"""Image sync — register (metadata) and ingest (OCR/CLIP/embed) as separate jobs."""
import logging

from pka.constants import Source
from pka.ingestion import sync_progress as sp
from pka.ingestion.dev_limits import take
from pka.ingestion.image_pipeline import ingest_images, register_images
from pka.ingestion.pending_metadata import archive_document_count, count_pending_metadata
from pka.ingestion.source_access import try_scan_images

log = logging.getLogger(__name__)

_EMPTY_STATS = {"processed": 0, "skipped": 0, "failed": 0}


def _plan_counts(key: str, total: int) -> None:
    sp.set_corpus_total(key, total)


def _unavailable_metadata(key: str, baseline: int, reason: str) -> dict:
    sp.begin_metadata_sync(key, 0, baseline)
    sp.skip_phase(key, "metadata")
    return {"metadata": dict(_EMPTY_STATS), "unavailable": reason}


def _unavailable_ingest(key: str, reason: str) -> dict:
    _plan_counts(key, 0)
    sp.skip_phase(key, "fetching")
    sp.skip_phase(key, "embedding")
    return {"ingest": dict(_EMPTY_STATS), "unavailable": reason}


def sync_images_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    from pka.db.queries import init_db

    init_db()
    key = progress_key or "image"
    baseline = archive_document_count(Source.IMAGE)
    images, unavailable = try_scan_images()
    if unavailable:
        return _unavailable_metadata(key, baseline, unavailable)
    pending = count_pending_metadata(Source.IMAGE)
    sp.begin_metadata_sync(key, pending, baseline)
    images = take(images)
    stats = register_images(images, dry_run=dry_run, progress_key=key)
    log.info("Image metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_images_ingest(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    key = progress_key or "image"
    images, unavailable = try_scan_images()
    if unavailable:
        return _unavailable_ingest(key, unavailable)
    images = take(images)
    n = len(images)
    _plan_counts(key, n)
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
    if meta.get("stopped") or meta.get("unavailable"):
        return meta
    return {**meta, **sync_images_ingest(progress_key=progress_key, dry_run=dry_run)}
