"""Calibre sync — metadata and ingest (embed + fulltext) as separate jobs."""
import logging

from pka.connectors.calibre import load_books
from pka.constants import Source
from pka.ingestion import sync_progress as sp
from pka.ingestion.pending_metadata import archive_document_count, count_pending_metadata
from pka.ingestion.sync_helpers import should_stop
from pka.ingestion.runners.calibre import (
    ingest_calibre_books,
    ingest_calibre_fulltext,
    ingest_calibre_metadata,
)

log = logging.getLogger(__name__)


def sync_calibre_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    from pka.db.queries import init_db

    init_db()
    key = progress_key or "calibre"
    baseline = archive_document_count(Source.CALIBRE)
    pending = count_pending_metadata(Source.CALIBRE)
    sp.begin_metadata_sync(key, pending, baseline)
    books = load_books()
    stats = ingest_calibre_metadata(books, dry_run=dry_run, progress_key=key)
    log.info("Calibre metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_calibre_ingest(
    progress_key: str | None = None,
    dry_run: bool = False,
    max_pages: int | None = None,
) -> dict:
    key = progress_key or "calibre"
    books = load_books()
    n = len(books)
    n_files = sum(
        1 for b in books if b.preferred_path and b.preferred_path.exists()
    )
    sp.set_corpus_total(key, n_files or n)
    sp.skip_phase(key, "fetching")

    stats: dict = {}
    sp.set_phase(key, "embedding", n)
    stats["metadata_embed"] = ingest_calibre_books(
        books, dry_run=dry_run, progress_key=key,
    )
    log.info("Calibre metadata embed: %s", stats["metadata_embed"])
    if stats["metadata_embed"].get("stopped"):
        stats["stopped"] = stats["metadata_embed"]["stopped"]
        return stats

    if n_files == 0:
        stats["fulltext"] = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}
        return stats

    file_books = [
        b for b in books if b.preferred_path and b.preferred_path.exists()
    ]
    sp.set_phase(key, "embedding", n_files)
    stats["fulltext"] = ingest_calibre_fulltext(
        file_books,
        dry_run=dry_run,
        max_pages=max_pages,
        progress_key=key,
    )
    log.info("Calibre fulltext: %s", stats["fulltext"])
    if stats["fulltext"].get("stopped"):
        stats["stopped"] = stats["fulltext"]["stopped"]
    elif should_stop(key):
        stats["stopped"] = should_stop(key)
    return stats


def sync_calibre(
    progress_key: str | None = None,
    dry_run: bool = False,
    max_pages: int | None = None,
) -> dict:
    """Full pipeline. Kept for scripts/tests."""
    meta = sync_calibre_metadata(progress_key=progress_key, dry_run=dry_run)
    if meta.get("stopped"):
        return meta
    return {**meta, **sync_calibre_ingest(
        progress_key=progress_key, dry_run=dry_run, max_pages=max_pages,
    )}
