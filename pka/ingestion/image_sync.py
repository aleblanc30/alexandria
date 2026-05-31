"""Image sync — register (metadata) and ingest (OCR/CLIP/embed) as separate jobs."""
import logging

from pka.config import settings
from pka.connectors.images import scan_images
from pka.constants import Source
from pka.ingestion import sync_progress as sp
from pka.ingestion.image_pipeline import ingest_images, register_images
from pka.ingestion.pending_metadata import archive_document_count, count_pending_metadata

log = logging.getLogger(__name__)


def sync_images_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    from pka.db.queries import init_db

    init_db()
    key = progress_key or "image"
    baseline = archive_document_count(Source.IMAGE)
    pending = count_pending_metadata(Source.IMAGE)
    sp.begin_metadata_sync(key, pending, baseline)
    images = scan_images(settings.images_dir)
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
    sp.set_corpus_total(key, n)
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
