"""Shared job engine used by both the CLI and the UI.

Features implemented here: parallel jobs, duplicate detection, resume
manifest, JSON/CSV reports, per-extension stats, verification, presets,
keep-awake during long runs, completion notification, watch mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import MARKER, __version__
from .discovery import (walk_sources, output_path, parse_since,
                        IMAGE_EXTS, VIDEO_EXTS)

STATE_FILE = ".compresso-state.json"

PRESETS = {
    # name: option overrides
    "email":   {"quality": "low", "max_edge": 1280, "video_codec": "h264"},
    "web":     {"quality": "medium", "max_edge": 1920, "video_codec": "h264"},
    "archive": {"quality": "high", "max_edge": None, "video_codec": "av1"},
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class KeepAwake:
    """Prevent the machine from sleeping during a long run (best effort)."""

    def __init__(self):
        self._proc = None
        self._token = None

    def __enter__(self):
        if sys.platform == "win32":
            try:
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
                self._token = ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
            except Exception:
                pass
        else:
            from shutil import which
            if which("systemd-inhibit"):
                try:
                    self._proc = subprocess.Popen(
                        ["systemd-inhibit", "--what=idle", "--who=Compresso",
                         "--why=long compression run", "sleep", "infinity"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
        return self

    def __exit__(self, *a):
        if self._proc:
            self._proc.terminate()
        if self._token is not None and sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass


def notify(title: str, msg: str) -> None:
    """Best-effort desktop notification + terminal bell."""
    print("\a", end="", flush=True)
    try:
        if sys.platform == "linux" and _which("notify-send"):
            subprocess.run(["notify-send", title, msg], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            import ctypes
            threading.Thread(
                target=ctypes.windll.user32.MessageBoxW,
                args=(None, msg, title, 0x40), daemon=True).start()
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{msg}" with title "{title}"'],
                           timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _which(n):
    from shutil import which
    return which(n)


def file_hash(p: Path) -> str:
    """Fast content fingerprint: size + sha256 of first and last 1 MiB."""
    h = hashlib.sha256()
    size = p.stat().st_size
    h.update(str(size).encode())
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 1 << 21:
            f.seek(-1 << 20, os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def load_state(out_root: Path) -> dict:
    p = out_root / STATE_FILE
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_state(out_root: Path, state: dict) -> None:
    try:
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / STATE_FILE).write_text(json.dumps(state))
    except Exception:
        pass


def build_work(opts: dict):
    """Resolve inputs and filters into a work list [(root, kind, path)]."""
    include_images = opts.get("images", True)
    include_videos = opts.get("videos", True)
    inputs = []
    for p in opts.get("paths", []):
        path = Path(p).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"not found: {p}")
        inputs.append(path)

    since = opts.get("since")
    since_ts = parse_since(since) if isinstance(since, str) else since

    work = []
    for path in inputs:
        if path.is_dir():
            for kind, f in walk_sources(
                    path, include_images, include_videos,
                    marker=str(opts.get("marker") or MARKER),
                    include_hidden=bool(opts.get("include_hidden", False)),
                    include=opts.get("include"), exclude=opts.get("exclude"),
                    min_size=int(opts.get("min_size") or 0),
                    max_size=opts.get("max_size"),
                    since=since_ts, max_depth=opts.get("max_depth"),
                    follow_symlinks=bool(opts.get("follow_symlinks", False))):
                work.append((path, kind, f))
        else:
            ext = path.suffix.lower()
            if include_images and ext in IMAGE_EXTS:
                work.append((path.parent, "image", path))
            elif include_videos and ext in VIDEO_EXTS:
                work.append((path.parent, "video", path))
    return work


def resolve_out_root(opts: dict, work) -> Path:
    if opts.get("output"):
        return Path(opts["output"]).expanduser().resolve()
    first_dir = next((r for r, _, f in work if f.parent == r and r.is_dir()), None)
    if first_dir is None:
        return work[0][0]
    return first_dir.parent / (first_dir.name + "-sqz")


def image_out_ext(fmt: str, src: Path) -> str:
    fixed = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "avif": ".avif"}
    if fmt in fixed:
        return fixed[fmt]
    return {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp",
            ".heic": ".jpg", ".heif": ".jpg", ".avif": ".avif",
            ".tif": ".png", ".tiff": ".png", ".bmp": ".jpg"}.get(src.suffix.lower(), ".jpg")


def verify_output(path: Path, kind: str) -> bool:
    """Decode the produced file to make sure it is not corrupt."""
    try:
        if kind == "image":
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
            return True
        from .videos import ffmpeg_bin
        r = subprocess.run([ffmpeg_bin(), "-v", "error", "-xerror",
                            "-i", str(path), "-f", "null", "-"],
                           capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False


def run_job(opts: dict, progress=None, cancel=None) -> dict:
    """Run a compression job. `progress(snapshot)` called on updates;
    `cancel` is an optional threading.Event. Returns a summary dict."""
    preset = opts.get("preset")
    if preset in PRESETS:
        for k, v in PRESETS[preset].items():
            opts.setdefault(k, v)

    work = build_work(opts)
    out_root = resolve_out_root(opts, work)
    marker = opts.get("marker", MARKER)

    # Resume: skip sources recorded as done in a previous run.
    state = load_state(out_root) if opts.get("resume") else {}
    if opts.get("dedupe"):
        seen_hashes = {}
        deduped = []
        for root, kind, f in work:
            try:
                h = file_hash(f)
            except OSError:
                deduped.append((root, kind, f))
                continue
            if h in seen_hashes:
                continue
            seen_hashes[h] = str(f)
            deduped.append((root, kind, f))
        work = deduped

    summary = {"state": "running", "total": len(work), "done": 0,
               "compressed": 0, "skipped": 0, "failed": 0,
               "total_in": 0, "total_out": 0, "entries": [],
               "by_ext": {}, "out_root": str(out_root), "error": None}

    if not work:
        summary["state"] = "done"
        return summary

    encoders = None
    if any(k == "video" for _, k, _ in work):
        from .videos import available_encoders
        try:
            encoders = available_encoders()
        except Exception:
            encoders = set()

    quality = opts.get("quality", "medium")
    fmt = opts.get("image_format", "auto")
    vcodec = opts.get("video_codec", "h264")
    max_edge = opts.get("max_edge")
    keep_audio = not opts.get("no_audio", False)
    force = opts.get("force", False)
    jobs = max(1, int(opts.get("jobs", 1)))
    do_verify = opts.get("verify", False)
    keep_mtime = opts.get("keep_mtime", False)
    lock = threading.Lock()

    def process(i, root, kind, f):
        rel = str(f.relative_to(root)).replace(os.sep, "/") if f.is_relative_to(root) else f.name
        entry = {"n": i, "file": str(f), "rel": rel, "kind": kind, "status": "running"}
        key = f"{f}|{f.stat().st_mtime_ns}"
        if state.get(key) == "done":
            entry.update(status="skipped", note="resume: already done")
            return entry, 0, 0
        try:
            if kind == "image":
                from .images import compress_image
                out = output_path(f, root, out_root, image_out_ext(fmt, f), marker)
                r = compress_image(f, out, fmt, quality, max_edge, force,
                                   strip_exif=opts.get("strip_exif", True),
                                   png_mode=opts.get("png_mode", "auto"),
                                   keep_mtime=keep_mtime)
            else:
                from .videos import compress_video
                out = output_path(f, root, out_root, ".mp4", marker)
                r = compress_video(f, out, quality, vcodec, max_edge, keep_audio,
                                   force, encoders,
                                   bitrate=opts.get("bitrate"),
                                   trim=opts.get("trim"),
                                   audio_bitrate=opts.get("audio_bitrate"),
                                   keep_mtime=keep_mtime)
        except Exception as e:
            entry.update(status="error", note=str(e))
            return entry, f.stat().st_size, 0

        if r.out_path:
            if do_verify and not verify_output(r.out_path, kind):
                Path(r.out_path).unlink(missing_ok=True)
                entry.update(status="error", note="verification failed")
                return entry, r.in_size, 0
            if opts.get("resume"):
                with lock:
                    state[key] = "done"
            entry.update(status="ok", out=str(r.out_path), in_size=r.in_size,
                         out_size=r.out_size,
                         pct=round((1 - r.out_size / r.in_size) * 100) if r.in_size else 0,
                         encoder=getattr(r, "encoder", None))
            return entry, r.in_size, r.out_size
        entry.update(status="skipped", note=r.note)
        return entry, r.in_size, r.out_size

    def update(entry, in_s, out_s):
        with lock:
            summary["entries"].append(entry)
            summary["total_in"] += in_s
            summary["total_out"] += out_s
            st = entry["status"]
            summary["compressed" if st == "ok" else
                    "failed" if st == "error" else "skipped"] += 1
            ext = Path(entry["file"]).suffix.lower()
            b = summary["by_ext"].setdefault(ext, {"n": 0, "in": 0, "out": 0})
            b["n"] += 1
            b["in"] += in_s
            b["out"] += out_s
            summary["done"] += 1
        if progress:
            progress(summary)

    with KeepAwake():
        if jobs == 1:
            for i, (root, kind, f) in enumerate(work, 1):
                if cancel is not None and cancel.is_set():
                    summary["state"] = "cancelled"
                    break
                update(*process(i, root, kind, f))
        else:
            with ThreadPoolExecutor(max_workers=jobs) as ex:
                futs = [ex.submit(process, i, root, kind, f)
                        for i, (root, kind, f) in enumerate(work, 1)]
                for fut in futs:
                    if cancel is not None and cancel.is_set():
                        summary["state"] = "cancelled"
                        break
                    update(*fut.result())

    if opts.get("resume"):
        save_state(out_root, state)
    if summary["state"] == "running":
        summary["state"] = "done"
    if opts.get("report"):
        write_report(summary, Path(opts["report"]))
    if opts.get("csv"):
        write_report(summary, Path(opts["csv"]), csv=True)
    if opts.get("notify") and summary["compressed"]:
        saved = summary["total_in"] - summary["total_out"]
        notify("Compresso", f"{summary['compressed']} files compressed, "
                            f"{human(max(saved, 0))} saved")
    return summary


def write_report(summary: dict, path: Path, csv: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if csv:
        import csv as _csv
        with open(path, "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["file", "kind", "status", "in_bytes", "out_bytes",
                        "saved_bytes", "pct", "encoder", "note"])
            for e in summary["entries"]:
                ins, outs = e.get("in_size", 0), e.get("out_size", 0)
                w.writerow([e["file"], e["kind"], e["status"], ins, outs,
                            ins - outs, e.get("pct", ""), e.get("encoder", ""),
                            e.get("note", "")])
    else:
        path.write_text(json.dumps(summary, indent=2))
