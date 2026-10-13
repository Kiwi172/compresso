"""Image compression backend (Pillow + pillow-heif).

Inputs: JPEG, PNG, WebP, HEIC/HEIF, TIFF, BMP; AVIF when Pillow supports it.
Outputs: JPEG, PNG, WebP, AVIF (when available). HEIC converts to JPEG.
Features: quality tiers or numeric quality, max-edge resize, EXIF stripping,
PNG palette quantization, mtime preservation, no-regression guard.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageOps

try:  # HEIC/HEIF support is optional at runtime
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False

QUALITY = {
    "low": 55,
    "medium": 72,
    "high": 85,
}

AUTO_MAP = {
    ".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp",
    ".heic": "jpeg", ".heif": "jpeg", ".avif": "avif",
    ".tif": "png", ".tiff": "png", ".bmp": "jpeg",
}


class ImageResult:
    def __init__(self, out_path: Path, in_size: int, out_size: int, note: str = ""):
        self.out_path = out_path
        self.in_size = in_size
        self.out_size = out_size
        self.note = note

    @property
    def saved(self) -> int:
        return self.in_size - self.out_size


def _quality(quality) -> int:
    if isinstance(quality, int):
        return max(1, min(100, quality))
    return QUALITY.get(str(quality), QUALITY["medium"])


def compress_image(
    src: Path,
    out_path: Path,
    fmt: str = "auto",
    quality="medium",  # str tier or int 1-100
    max_edge: int | None = None,
    force: bool = False,
    strip_exif: bool = True,
    png_mode: str = "auto",
    keep_mtime: bool = False,
) -> ImageResult:
    """Compress one image. fmt: auto|jpeg|png|webp|avif.

    'auto' keeps the source format (HEIC input -> JPEG output).
    png_mode: auto | palette (256-colour quantization, big PNG wins) | rgb.
    strip_exif: drop metadata (privacy + size). Default on, like the paid apps.
    """
    in_size = src.stat().st_size
    with Image.open(src) as im:
        info = dict(im.info)
        im = ImageOps.exif_transpose(im)  # honour orientation
        if strip_exif:
            info.pop("exif", None)
            info.pop("icc_profile", None)

        target = fmt.lower()
        if target == "auto":
            target = AUTO_MAP[src.suffix.lower()]

        if max_edge and max(im.size) > max_edge:
            scale = max_edge / max(im.size)
            new = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            im = im.resize(new, Image.LANCZOS)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        q = _quality(quality)

        if target == "jpeg":
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA", "PA") else None)
                im = bg
            im.save(out_path, "JPEG", quality=q, optimize=True, progressive=True, **({"icc_profile": info["icc_profile"]} if info.get("icc_profile") else {}))
        elif target == "webp":
            im.save(out_path, "WEBP", quality=q, method=6)
        elif target == "avif":
            im.save(out_path, "AVIF", quality=q)
        elif target == "png":
            if png_mode == "palette":
                im = im.convert("RGBA").quantize(colors=256, method=Image.Quantize.FASTOCTREE)
            elif im.mode in ("P", "LA", "PA") or im.mode == "RGBA":
                im = im.convert("RGBA")
            else:
                im = im.convert("RGB")
            im.save(out_path, "PNG", optimize=True)
        else:
            raise ValueError(f"unsupported output format: {target}")

    if keep_mtime:
        try:
            st = src.stat()
            os.utime(out_path, (st.st_atime, st.st_mtime))
        except OSError:
            pass

    out_size = out_path.stat().st_size
    note = ""
    if out_size >= in_size and not force:
        # No-regression guard: keep the original, drop the larger output.
        out_path.unlink(missing_ok=True)
        note = "skipped (compressed result was larger)"
        return ImageResult(None, in_size, in_size, note)
    return ImageResult(out_path, in_size, out_size)
