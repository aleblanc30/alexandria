"""Graceful access to optional source paths (Calibre library, image folder)."""
from __future__ import annotations

import logging

from pka.config import settings
from pka.connectors.calibre import CalibreBook, load_books
from pka.connectors.images import ImageFile, scan_image_dirs
from pka.connectors.youtube import YouTubeVideo

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


def youtube_available() -> tuple[bool, str | None]:
    """Cheap, network-free check for whether YouTube credentials are configured."""
    from pka.connectors.youtube import youtube_credentials_available

    return youtube_credentials_available()


def try_load_youtube_videos() -> tuple[list[YouTubeVideo], str | None]:
    """Load saved videos, returning ``([], reason)`` when unavailable.

    Credential/import problems and transient API errors are swallowed into a
    reason string so an ingest job degrades gracefully instead of crashing.
    """
    from pka.connectors.youtube import YouTubeAuthError, load_saved_videos

    ok, reason = youtube_available()
    if not ok:
        return [], reason
    try:
        return load_saved_videos(), None
    except YouTubeAuthError as exc:
        log.warning("YouTube unavailable: %s", exc)
        return [], str(exc)
    except Exception as exc:  # network / quota / API errors must not crash the job
        log.warning("YouTube load failed: %s", exc)
        return [], f"YouTube load failed: {exc}"
