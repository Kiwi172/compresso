"""File discovery and folder-tree walking with filters.

Compresso behaviour:
- recurses all subfolders and mirrors the tree in the output
- skips files already produced by Compresso (``.sqz`` marker names)
- optional filters: glob include/exclude, hidden files, size bounds,
  modification-time cutoff, recursion depth, symlink following
"""

from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path

from . import MARKER

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".tif", ".tiff", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".ts", ".h264", ".mpg", ".mpeg"}

# Containers/codecs that are already efficient for delivery: skip re-encoding
# unless the user forces it.
EFFICIENT_VIDEO_CODECS = {"h264", "hevc", "av1", "vp9"}
EFFICIENT_VIDEO_CONTAINERS = {".mp4", ".m4v", ".mkv", ".webm"}


def is_compresso_output(path: Path, marker: str = MARKER) -> bool:
    """True if this file was produced by a previous Compresso run."""
    return marker in path.name


def _match_any(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p)
               for p in patterns)


def walk_sources(root: Path, include_images: bool = True, include_videos: bool = True, *,
                 marker: str = MARKER, include_hidden: bool = False,
                 include=None, exclude=None, min_size: int = 0,
                 max_size: int | None = None, since: float | None = None,
                 max_depth: int | None = None, follow_symlinks: bool = False):
    """Yield (kind, path) for every supported, filter-passing file under root.

    since: epoch cutoff — only files modified at or after this time.
    max_depth: 0 = only top level, 1 = one level down, etc.
    """
    root = Path(root)
    root_depth = len(root.parts)
    visited = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        d = Path(dirpath)
        if follow_symlinks:
            key = os.path.realpath(d)
            if key in visited:
                continue
            visited.add(key)
        depth = len(d.parts) - root_depth
        dirnames[:] = sorted(x for x in dirnames
                             if (include_hidden or not x.startswith("."))
                             and (max_depth is None or depth + 1 <= max_depth))
        for name in sorted(filenames):
            if not include_hidden and name.startswith("."):
                continue
            p = d / name
            rel = str(p.relative_to(root)).replace(os.sep, "/")
            ext = p.suffix.lower()
            if is_compresso_output(p, marker):
                continue
            kind = None
            if include_images and ext in IMAGE_EXTS:
                kind = "image"
            elif include_videos and ext in VIDEO_EXTS:
                kind = "video"
            if kind is None:
                continue
            if include and not _match_any(rel, include):
                continue
            if exclude and _match_any(rel, exclude):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < min_size:
                continue
            if max_size is not None and st.st_size > max_size:
                continue
            if since is not None and st.st_mtime < since:
                continue
            yield kind, p


def output_path(src: Path, root: Path, out_root: Path, new_ext: str,
                marker: str = MARKER) -> Path:
    """Map a source path to its output path, preserving relative structure.

    Output names get the ``marker`` so re-running over the same tree skips
    already-produced files and originals are never overwritten.
    """
    rel = src.relative_to(root)
    return out_root / rel.parent / f"{rel.stem}{marker}{new_ext}"


def parse_since(value: str) -> float:
    """Parse --since: ISO date (2026-01-01) or relative (7d, 24h, 30m)."""
    v = value.strip().lower()
    if v.endswith(("d", "h", "m")) and v[:-1].isdigit():
        n = int(v[:-1])
        mult = {"d": 86400, "h": 3600, "m": 60}[v[-1]]
        return time.time() - n * mult
    import datetime
    return datetime.datetime.fromisoformat(value).timestamp()
