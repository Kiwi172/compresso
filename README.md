# Compresso

Batch image & video compression for **Linux and Windows**.

Drop a whole folder (or single files), choose a quality level, and it compresses
everything locally. GPU-accelerated video encoding when your hardware supports it.

A full feature list (50 items) is in [FEATURES.md](FEATURES.md).

## What it does

- **Images** — JPEG, PNG, WebP, HEIC/HEIF in; JPEG, PNG, WebP out.
  HEIC converts to JPEG. Optional downscale (`--max-edge`).
- **Videos** — transcode to H.264 MP4 (or AV1 MP4) using NVIDIA NVENC,
  Intel QSV, AMD AMF, or VAAPI when available; falls back to x264 / SVT-AV1.
  HDR input is tone-mapped to SDR. Keep or drop audio.
- **Folders are first-class** — recurses all subfolders and mirrors the
  directory structure in the output tree.
- **Idempotent** — outputs carry a `.sqz` marker, so re-running over the same
  folder skips work already done.
- **No-regression guard** — if compression would make a file *larger*, the
  original is kept and no output is written. Videos already in an efficient
  codec (H.264/HEVC/AV1/VP9) are skipped.
- **Originals are never modified.**

## Install

Requires Python 3.9+ and, for video, [ffmpeg](https://ffmpeg.org) on PATH.

```bash
pipx install compresso        # or: pip install compresso
# ffmpeg:
#   Ubuntu/Debian: sudo apt install ffmpeg
#   Fedora:        sudo dnf install ffmpeg
#   Windows:       winget install Gyan.FFmpeg
#   macOS:         brew install ffmpeg
```

Check your environment (which GPU encoders are available, etc.):

```bash
compresso --doctor
```

## Graphical interface

```bash
compresso --ui
```

Opens the UI in a **separate window** (not a browser tab):

1. If [pywebview](https://pywebview.flowrl.com) is installed
   (`pipx install "compresso[ui]"`), you get a true native app window.
2. Otherwise it uses Edge/Chrome/Chromium in `--app` mode — a standalone
   window with no browser chrome. On Windows this works out of the box
   (Edge is always present).
3. Only if neither is available does it fall back to a browser tab.

Closing the window stops the server. Use `compresso --ui --browser` to
force a normal tab. The window has the same features as before: environment
pills, folder picker form, quality/format/codec options, live per-file
progress and savings table.

## Usage

```bash
# Compress every image under ./photos -> ./photos-sqz (same tree, .sqz names)
compresso ./photos

# Pick quality, resize long edge to 1920, convert everything to WebP
compresso ./photos -q high --max-edge 1920 --image-format webp

# Videos to AV1, drop audio, explicit output root
compresso ./clips -o ./clips-small --video-codec av1 --no-audio

# Everything, images and video, in one pass
compresso ./media -o ./media-small -q medium

# See what would happen without touching anything
compresso ./photos --dry-run
```

Output naming: `photo.jpg` → `photo.sqz.jpg`, `clip.mov` → `clip.sqz.mp4`,
always under a parallel output tree.

## Windows notes

Works on any Windows with Python 3.9+ and ffmpeg. Hardware encoding uses
NVENC/QSV/AMF. No Windows-11-only dependency.

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -e ".[dev]" || pip install -e .
python -m pytest
```

## License

MIT
