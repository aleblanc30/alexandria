"""Image sync — register (metadata) and ingest (OCR/CLIP/embed) as separate jobs."""
import logging

from pka.config import settings
from pka.connectors.images import scan_images
from pka.ingestion import sync_progress as sp
from pka.ingestion.image_pipeline import ingest_images, register_images

log = logging.getLogger(__name__)


def sync_images_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    key = progress_key or "image"
    images = scan_images(settings.images_dir)
    n = len(images)
    sp.plan_pipeline(key, [("metadata", n), ("fetching", 0), ("embedding", n)])
    sp.set_phase(key, "metadata", n)
    stats = register_images(images, dry_run=dry_run, progress_key=key)
    log.info("Image metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_images_ingest(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    key = progress_key or "image"
    images = scan_images(settings.images_dir)
    n = len(images)
    sp.plan_pipeline(key, [("metadata", n), ("fetching", 0), ("embedding", n)])
    sp.skip_phase(key, "fetching")
    sp.set_phase(key, "embedding", n)
    stats = ingest_images(images, dry_run=dry_run, progress_key=key)
    log.info("Image ingest: %s", stats)
    return {"ingest": stats, "stopped": stats.get("stopped")}


def sync_images(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Full pipeline. Kept for scripts/tests."""
    meta = sync_images_metadata(progress_key=progress_key, dry_run=dry_run)
    if meta.get("stopped"):
        return meta
    return {**meta, **sync_images_ingest(progress_key=progress_key, dry_run=dry_run)}
