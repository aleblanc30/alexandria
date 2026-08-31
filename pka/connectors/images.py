"""
Image folder connector.
Walks a directory recursively, collects image files, and extracts
EXIF / file metadata. Classification and text extraction happen in
the ingestion layer (image_pipeline.py).

Supported formats: JPEG, PNG, WEBP, TIFF, BMP, GIF (first frame only).
"""

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp", ".gif"}


@dataclass
class ImageFile:
    path: Path
    filename: str
    width: int | None
    height: int | None
    file_size: int  # bytes
    date_taken: int | None  # unix timestamp (EXIF DateTimeOriginal → mtime fallback)
    exif: dict  # raw EXIF key-value pairs (strings)


# ── EXIF extraction ───────────────────────────────────────────────────────────


def _read_exif(path: Path) -> tuple[dict, int | None, int | None, int | None]:
    """
    Returns (exif_dict, width, height, date_taken_ts).
    Uses Pillow's _getexif() if available; falls back to mtime for the date.
    """
    exif: dict = {}
    width = height = date_ts = None

    try:
        from PIL import ExifTags, Image

        with Image.open(path) as img:
            width, height = img.size
            raw = img._getexif() if hasattr(img, "_getexif") else None
            if raw:
                exif = {
                    ExifTags.TAGS.get(k, str(k)): str(v)
                    for k, v in raw.items()
                    if isinstance(v, (str, int, float, bytes))
                }
                # Parse DateTimeOriginal → unix ts
                dt_str = exif.get("DateTimeOriginal") or exif.get("DateTime")
                if dt_str:
                    try:
                        from datetime import datetime

                        dt = datetime.strptime(dt_str[:19], "%Y:%m:%d %H:%M:%S")
                        date_ts = int(dt.timestamp())
                    except Exception:
                        pass
    except Exception as exc:
        log.debug("EXIF read failed for %s: %s", path.name, exc)

    if date_ts is None:
        date_ts = int(path.stat().st_mtime)

    return exif, width, height, date_ts


# ── Directory walker ──────────────────────────────────────────────────────────


def scan_images(root: Path) -> list[ImageFile]:
    """
    Recursively walk `root` and return an ImageFile for every image found.
    Symlinks are followed once; cycles are detected via visited inode set.
    """
    if not root.exists():
        raise FileNotFoundError(f"Image folder not found: {root}")

    results: list[ImageFile] = []
    seen_inodes: set[int] = set()

    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        if not p.is_file():
            continue
        try:
            inode = p.stat().st_ino
            if inode in seen_inodes:
                continue
            seen_inodes.add(inode)

            size = p.stat().st_size
            exif, w, h, date_ts = _read_exif(p)

            results.append(
                ImageFile(
                    path=p,
                    filename=p.name,
                    width=w,
                    height=h,
                    file_size=size,
                    date_taken=date_ts,
                    exif=exif,
                )
            )
        except Exception as exc:
            log.warning("Skipping %s: %s", p, exc)

    log.info("Scanned %d images under %s", len(results), root)
    return results


def scan_image_dirs(roots: list[Path]) -> list[ImageFile]:
    """Scan several image folders, de-duplicating files shared between roots.

    Overlapping or nested roots (e.g. a folder and its subfolder both being
    configured) produce the same absolute path for a shared file, so a
    seen-path set keeps each image once. Missing roots are skipped with a
    warning rather than raising — one gone folder must not fail the whole scan.
    """
    results: list[ImageFile] = []
    seen_paths: set[str] = set()

    for root in roots:
        if not root.exists():
            log.warning("Image folder not found, skipping: %s", root)
            continue
        for img in scan_images(root):
            key = str(img.path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            results.append(img)

    log.info("Scanned %d images across %d folder(s)", len(results), len(roots))
    return results
