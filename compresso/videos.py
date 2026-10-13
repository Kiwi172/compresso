"""Video compression backend (ffmpeg).

Compresso behaviour:
- hardware encoding when available (NVIDIA NVENC / Intel QSV / AMD AMF /
  VAAPI on Linux), falling back to software x264
- optional AV1 output when the GPU encoder supports it
- skip videos already in an efficient codec (unless --force)
- no-regression guard: if the result is larger, keep the original
- HDR input is tone-mapped to SDR for sharing
- keep or drop audio; output keeps the first audio track only
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .discovery import EFFICIENT_VIDEO_CODECS

# Encoder candidates in preference order. Each entry: (encoder, rate-control args
# template where {q} is a quality number 18-30-ish for x264-style CRF).
HW_H264 = [
    ("h264_nvenc", ["-preset", "slow", "-rc", "vbr", "-cq", "{q}", "-b", "0"]),
    ("h264_qsv", ["-global_quality", "{q}"]),
    ("h264_amf", ["-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"]),
    ("h264_vaapi", ["-rc", "low_power", "-global_quality", "{q}"]),
]
HW_AV1 = [
    ("av1_nvenc", ["-preset", "slow", "-rc", "vbr", "-cq", "{q}", "-b", "0"]),
    ("av1_qsv", ["-global_quality", "{q}"]),
    ("av1_amf", ["-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"]),
    ("av1_vaapi", ["-rc", "low_power", "-global_quality", "{q}"]),
]
SW_H264 = ("libx264", ["-preset", "slow", "-crf", "{q}"])
SW_AV1 = ("libsvtav1", ["-preset", "6", "-crf", "{q}"])
HW_HEVC = [
    ("hevc_nvenc", ["-preset", "slow", "-rc", "vbr", "-cq", "{q}", "-b", "0"]),
    ("hevc_qsv", ["-global_quality", "{q}"]),
    ("hevc_amf", ["-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"]),
    ("hevc_vaapi", ["-rc", "low_power", "-global_quality", "{q}"]),
]
SW_HEVC = ("libx265", ["-preset", "slow", "-crf", "{q}"])

QUALITY_TO_CRF = {"low": 30, "medium": 24, "high": 20}

# NVENC/AMF need format=nv16/tunnel etc.; we insert a scale/format filter for
# vaapi only. Keep the filtergraph simple and portable.


class VideoError(RuntimeError):
    pass


class VideoResult:
    def __init__(self, out_path, in_size, out_size, note="", encoder=None):
        self.out_path = out_path
        self.in_size = in_size
        self.out_size = out_size
        self.note = note
        self.encoder = encoder

    @property
    def saved(self) -> int:
        return self.in_size - self.out_size


def ffmpeg_bin() -> str:
    f = shutil.which("ffmpeg")
    if not f:
        raise VideoError("ffmpeg not found on PATH. Install ffmpeg to compress videos.")
    return f


def ffprobe_bin() -> str:
    f = shutil.which("ffprobe")
    if not f:
        raise VideoError("ffprobe not found on PATH (ships with ffmpeg).")
    return f


def probe(src: Path) -> dict:
    cmd = [
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(src),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise VideoError(f"ffprobe failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def available_encoders() -> set:
    out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"],
                        capture_output=True, text=True)
    return {line.split()[1] for line in out.stdout.splitlines()
            if len(line.split()) >= 2 and line.strip().startswith("V")}


def encoder_candidates(kind: str, encoders: set) -> list:
    """All viable encoders in preference order (hardware first, software last)."""
    if kind == "av1":
        table, sw = HW_AV1, SW_AV1
    elif kind == "hevc":
        table, sw = HW_HEVC, SW_HEVC
    else:
        table, sw = HW_H264, SW_H264
    cands = [(n, a) for n, a in table if n in encoders]
    cands.append(sw)
    return cands


def is_already_efficient(src: Path, info: dict) -> bool:
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return False
    codec = (v.get("codec_name") or "").lower()
    return codec in EFFICIENT_VIDEO_CODECS


def is_hdr(info: dict) -> bool:
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return False
    return (v.get("color_transfer") or "").lower() in ("smpte2084", "arib-std-b67")


def compress_video(
    src: Path,
    out_path: Path,
    quality: str = "medium",
    kind: str = "h264",
    max_edge: int | None = None,
    keep_audio: bool = True,
    force: bool = False,
    encoders: set | None = None,
    bitrate: str | None = None,
    trim: str | None = None,
    audio_bitrate: str | None = None,
    keep_mtime: bool = False,
) -> VideoResult:
    in_size = src.stat().st_size
    info = probe(src)
    # Trimming or an explicit bitrate is an intentional edit: bypass the
    # already-efficient skip.
    force = force or bool(trim)

    if is_already_efficient(src, info) and not force:
        return VideoResult(None, in_size, in_size, "skipped (already efficient codec)", None)

    encoders = encoders or available_encoders()
    q = QUALITY_TO_CRF.get(quality, QUALITY_TO_CRF["medium"]) if not bitrate else None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / (out_path.name + ".part.mp4")

    last_err = ""
    for encoder, args_tpl in encoder_candidates(kind, encoders):
        # Filter chain: HDR->SDR tone-map, optional scale.
        filters = []
        if is_hdr(info):
            filters.append("zscale=t=linear:npl=100,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=bt709,format=nv12")
        if max_edge:
            filters.append(
                f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':'if(gt(iw,ih),-2,min({max_edge},ih))'"
            )
        if encoder.endswith("_vaapi"):
            filters.append("format=nv12")

        cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
        if encoder.endswith("_vaapi"):
            cmd += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
        if trim:  # --trim 30s, 1:02-1:30, or START-DURATION
            start, dur = _parse_trim(trim)
            if start:
                cmd += ["-ss", start]
            if dur:
                cmd += ["-t", dur]
        cmd += ["-i", str(src)]
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-c:v", encoder]
        if bitrate:
            cmd += ["-b:v", bitrate]
        else:
            cmd += [a.replace("{q}", str(q)) for a in args_tpl]
        cmd += ["-pix_fmt", "nv12"] if encoder.endswith("_vaapi") else ["-pix_fmt", "yuv420p"]
        if keep_audio:
            cmd += ["-c:a", "aac"] if audio_bitrate else ["-c:a", "copy"]
            if audio_bitrate:
                cmd += ["-b:a", audio_bitrate]
        else:
            cmd += ["-an"]
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"] if keep_audio else ["-map", "0:v:0"]
        cmd += ["-movflags", "+faststart", str(tmp)]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            break
        tmp.unlink(missing_ok=True)
        last_err = proc.stderr.strip()[-300:]
        # Hardware encoder present but not actually usable (no GPU/driver):
        # fall through to the next candidate.
    else:
        raise VideoError(f"all {kind} encoders failed; last error: {last_err}")

    out_size = tmp.stat().st_size
    if out_size >= in_size and not force:
        tmp.unlink(missing_ok=True)
        return VideoResult(None, in_size, in_size, "skipped (result would be larger)", encoder)
    if keep_mtime:
        try:
            import os
            st = src.stat()
            os.utime(tmp, (st.st_atime, st.st_mtime))
        except OSError:
            pass
    tmp.rename(out_path)
    return VideoResult(out_path, in_size, out_size, "", encoder)


def _parse_trim(spec: str) -> tuple:
    """Parse 'START', 'START-END' (times as SSs, M:SS, or seconds)."""
    def to_sec(v):
        v = v.strip().lower()
        if ":" in v:
            parts = [float(x) for x in v.split(":")]
            s = 0.0
            for p in parts:
                s = s * 60 + p
            return s
        if v.endswith("s"):
            v = v[:-1]
        return float(v)
    if "-" in spec and not spec.strip().endswith("-"):
        a, b = spec.split("-", 1)
        start = to_sec(a)
        end = to_sec(b)
        return str(start), str(max(0.0, end - start))
    return str(to_sec(spec)), None
