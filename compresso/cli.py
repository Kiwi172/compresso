"""Compresso command-line interface.

Usage examples:
    compresso ./photos                        # -> ./photos-sqz parallel tree
    compresso ./media -o out -q high --max-edge 1920 --jobs 4
    compresso ./clips --video-codec hevc --bitrate 2M --trim 5s-60s
    compresso --watch ./inbox --preset web
    compresso --doctor
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import load_config
from .discovery import output_path, IMAGE_EXTS, VIDEO_EXTS
from .engine import (run_job, human, resolve_out_root, build_work,
                     image_out_ext, PRESETS)


def doctor() -> int:
    from .images import HEIF_OK
    print(f"Compresso {__version__} — environment check")
    print(f"  Pillow: installed, HEIC/HEIF: {'yes' if HEIF_OK else 'NO (pip install pillow-heif)'}")
    try:
        from .videos import ffmpeg_bin, ffprobe_bin, available_encoders
        import subprocess
        v = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True)
        print(f"  ffmpeg: {v.stdout.splitlines()[0]}")
        print(f"  ffprobe: {ffprobe_bin()}")
        encs = available_encoders()
        hw = sorted(e for e in encs if any(t in e for t in ("nvenc", "qsv", "amf", "vaapi", "vulkan")))
        sw = sorted(e for e in encs if e in ("libx264", "libsvtav1", "libx265"))
        print(f"  hardware video encoders: {', '.join(hw) or 'none (software fallback will be used)'}")
        print(f"  software video encoders: {', '.join(sw) or 'none'}")
    except Exception as e:
        print(f"  ffmpeg: NOT AVAILABLE ({e})")
        print("    Install it: Ubuntu/Debian `sudo apt install ffmpeg`, "
              "Windows `winget install Gyan.FFmpeg`, or any OS via `pipx install ffmpeg-binaries`.")
    return 0


def _size(v: str) -> int:
    v = v.strip().lower()
    mult = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if v and v[-1] in mult:
        return int(float(v[:-1]) * mult[v[-1]])
    return int(v)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="compresso",
        description="Batch-compress images and videos. Originals are never modified; "
                    "results are written to a parallel output tree with '.sqz' in the name. "
                    "Re-running over the same folders skips work already done.",
    )
    p.add_argument("paths", nargs="*", help="Files and/or folders to compress")
    p.add_argument("-o", "--output", help="Output root (default: <first-input>-sqz)")
    p.add_argument("-q", "--quality", default="medium",
                   help="low|medium|high or a number 1-100")
    p.add_argument("--preset", choices=sorted(PRESETS),
                   help="Preset: email (small), web (balanced), archive (best)")
    p.add_argument("--images", action="store_true", help="Only process images")
    p.add_argument("--videos", action="store_true", help="Only process videos")
    p.add_argument("--image-format", choices=["auto", "jpeg", "png", "webp", "avif"],
                   default="auto", help="Image output format (auto = keep source)")
    p.add_argument("--video-codec", choices=["h264", "hevc", "av1"], default="h264",
                   help="Video output codec (hardware preferred, software fallback)")
    p.add_argument("--bitrate", help="Video target bitrate, e.g. 2M or 800k "
                                     "(overrides quality tiers)")
    p.add_argument("--audio-bitrate", help="Re-encode audio at this bitrate, e.g. 128k")
    p.add_argument("--trim", metavar="SPEC",
                   help="Trim video: '5s' from start, or '1:02-1:30' range")
    p.add_argument("--max-edge", type=int, help="Downscale longest edge to N pixels")
    p.add_argument("--png-mode", choices=["auto", "palette", "rgb"], default="auto",
                   help="PNG strategy; 'palette' quantizes to 256 colours for big wins")
    p.add_argument("--keep-exif", action="store_true",
                   help="Keep EXIF/ICC metadata in images (stripped by default)")
    p.add_argument("--keep-mtime", action="store_true",
                   help="Copy the source modification time to outputs")
    p.add_argument("--no-audio", action="store_true", help="Drop audio from videos")
    p.add_argument("--force", action="store_true",
                   help="Disable skip rules (already-efficient codecs, larger-result guard)")
    p.add_argument("--include", action="append", metavar="GLOB",
                   help="Only files matching this glob (repeatable)")
    p.add_argument("--exclude", action="append", metavar="GLOB",
                   help="Skip files matching this glob (repeatable)")
    p.add_argument("--min-size", type=_size, default=0,
                   help="Skip files smaller than this (e.g. 10k, 1m)")
    p.add_argument("--max-size", type=_size, help="Skip files larger than this")
    p.add_argument("--since", metavar="DATE",
                   help="Only files modified after DATE (ISO date or 7d/24h/30m)")
    p.add_argument("--max-depth", type=int, metavar="N",
                   help="Limit folder recursion depth (0 = top level only)")
    p.add_argument("--include-hidden", action="store_true", help="Include dot-files/dirs")
    p.add_argument("--follow-symlinks", action="store_true", help="Follow symlinked dirs")
    p.add_argument("--marker", default=".sqz",
                   help="Output-name marker used for skip-on-rerun (default .sqz)")
    p.add_argument("--jobs", "-j", type=int, default=1,
                   help="Parallel files to process (default 1)")
    p.add_argument("--resume", action="store_true",
                   help="Record progress in the output tree and skip done files on re-run")
    p.add_argument("--dedupe", action="store_true",
                   help="Compress only the first of any identical-content files")
    p.add_argument("--verify", action="store_true",
                   help="Decode each output to confirm it is not corrupt")
    p.add_argument("--report", metavar="FILE", help="Write a JSON report")
    p.add_argument("--csv", metavar="FILE", help="Write a CSV report")
    p.add_argument("--config", metavar="FILE", help="Config file (TOML/JSON) for defaults")
    p.add_argument("--notify", action="store_true", help="Desktop notification when done")
    p.add_argument("--watch", action="store_true",
                   help="Keep running: process new files as they appear")
    p.add_argument("--interval", type=int, default=10, help="Watch interval seconds")
    p.add_argument("--dry-run", action="store_true", help="List what would be processed")
    p.add_argument("--doctor", action="store_true", help="Check ffmpeg/encoders and exit")
    p.add_argument("--ui", action="store_true",
                   help="Open the graphical interface in a separate window")
    p.add_argument("--browser", action="store_true", help="With --ui: force a browser tab")
    p.add_argument("--port", type=int, default=0, help="Port for --ui (default: auto)")
    p.add_argument("--version", action="version", version=f"compresso {__version__}")
    return p


def opts_from_args(args) -> dict:
    cfg = load_config(args.config)
    o = dict(cfg)
    for k in ("paths", "output", "quality", "preset", "image_format", "video_codec",
              "bitrate", "audio_bitrate", "trim", "max_edge", "png_mode", "marker",
              "include", "exclude", "min_size", "max_size", "since", "max_depth",
              "jobs", "report", "csv", "interval"):
        v = getattr(args, k, None)
        if v is not None:
            o[k] = v
    if getattr(args, "images"):
        o["images"], o["videos"] = True, False
    if getattr(args, "videos"):
        o["videos"], o["images"] = True, False
    for flag, key in (("no_audio", "no_audio"), ("force", "force"),
                      ("resume", "resume"), ("dedupe", "dedupe"),
                      ("verify", "verify"), ("notify", "notify"),
                      ("keep_mtime", "keep_mtime"), ("include_hidden", "include_hidden"),
                      ("follow_symlinks", "follow_symlinks")):
        if getattr(args, flag):
            o[key] = True
    if getattr(args, "keep_exif", False):
        o["strip_exif"] = False
    o.setdefault("images", True)
    o.setdefault("videos", True)
    return o


def print_summary(s: dict) -> None:
    saved = s["total_in"] - s["total_out"]
    print(f"\nDone: {s['compressed']} compressed, {s['skipped']} skipped, "
          f"{s['failed']} failed. Saved {human(max(saved, 0))} of {human(s['total_in'])}.")
    if s["compressed"]:
        print(f"Output tree: {s['out_root']}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.doctor:
        return doctor()
    if args.ui:
        from .webui import launch
        launch(port=args.port, mode="browser" if args.browser else "window")
        return 0
    if not args.paths:
        build_parser().print_help()
        return 2

    opts = opts_from_args(args)
    quality = opts.get("quality", "medium")
    if str(quality).isdigit():
        opts["quality"] = int(quality)

    # Dry run: list planned work only.
    if args.dry_run:
        try:
            work = build_work(opts)
            out_root = resolve_out_root(opts, work)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for root, kind, f in work:
            new_ext = image_out_ext(opts.get("image_format", "auto"), f) if kind == "image" else ".mp4"
            print(f"[{kind}] {f} -> {output_path(f, root, out_root, new_ext, opts.get('marker', '.sqz'))}")
        print(f"{len(work)} file(s).")
        return 0

    def show(s):
        i = s["done"]
        for e in s["entries"][-1:]:
            if e["status"] == "ok":
                extra = f" via {e['encoder']}" if e.get("encoder") else ""
                print(f"[{i}/{s['total']}] {Path(e['file']).name}: "
                      f"{human(e['in_size'])} -> {human(e['out_size'])} "
                      f"({e['pct']:+d}%){extra}")
            elif e["status"] == "skipped":
                print(f"[{i}/{s['total']}] {Path(e['file']).name}: {e.get('note', 'skipped')}")
            elif e["status"] == "error":
                print(f"[{i}/{s['total']}] FAIL {Path(e['file']).name}: {e.get('note')}",
                      file=sys.stderr)

    if args.watch:
        print(f"Watching {', '.join(opts['paths'])} every {opts['interval']}s (Ctrl+C to stop)")
        try:
            while True:
                s = run_job(opts, progress=show)
                if s["compressed"]:
                    print_summary(s)
                time.sleep(opts.get("interval", 10))
        except KeyboardInterrupt:
            return 0

    try:
        s = run_job(opts, progress=show)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_summary(s)
    if opts.get("report"):
        print(f"JSON report: {opts['report']}")
    if opts.get("csv"):
        print(f"CSV report: {opts['csv']}")
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
