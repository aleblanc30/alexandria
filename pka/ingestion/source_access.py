"""Graceful access to optional source paths (Calibre library, image folder)."""
from __future__ import annotations

import logging

from pka.config import settings
from pka.connectors.calibre import CalibreBook, load_books
from pka.connectors.images import ImageFile, scan_image_dirs

log = logging.getLogger(__name__)


def calibre_available() -> tuple[bool, str | None]:
    """Check whether the Calibre library metadata.db exists."""
    path = settings.book_archive / "metadata.db"
    if path.exists():
        return True, None
    reason = f"Calibre metadata.db not found at {path}"
    return False, reason


def images_available() -> tuple[bool, str | None]:
    """Check whether at least one configured image folder exists."""
    roots = settings.image_dirs
    if not roots:
        return False, "No image folders configured"
    if any(root.exists() for root in roots):
        return True, None
    if len(roots) == 1:
        return False, f"Image folder not found: {roots[0]}"
    return False, f"None of the {len(roots)} configured image folders exist"


def try_load_calibre_books() -> tuple[list[CalibreBook], str | None]:
    """Load Calibre books, returning ``([], reason)`` when the library is missing."""
    try:
        return load_books(), None
    except FileNotFoundError as exc:
        log.warning("%s", exc)
        return [], str(exc)


def try_scan_images() -> tuple[list[ImageFile], str | None]:
    """Scan all configured image folders, returning ``([], reason)`` when none exist."""
    ok, reason = images_available()
    if not ok:
        log.warning("%s", reason)
        return [], reason
    return scan_image_dirs(settings.image_dirs), None
