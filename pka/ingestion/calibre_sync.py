"""Calibre sync — metadata and ingest (embed + fulltext) as separate jobs."""

import logging

from pka.constants import FetchStatus, Source
from pka.db.chunks import source_ids_with_chunk_pass
from pka.db.documents import source_ids_with_fetch_status
from pka.ingestion import progress as sp
from pka.ingestion.dev_limits import take
from pka.ingestion.pending_metadata import (
    archive_document_count,
    count_pending_metadata,
    load_calibre_books,
)
from pka.ingestion.progress import should_stop
from pka.ingestion.runners.calibre import (
    ingest_calibre_books,
    ingest_calibre_fulltext,
    ingest_calibre_metadata,
)
from pka.ingestion.sync_shared import EMPTY_STATS, run_full_sync, unavailable_metadata

log = logging.getLogger(__name__)

_EMPTY_EMBED = {**EMPTY_STATS, "chunks": 0}


def _plan_counts(key: str, total: int) -> None:
    sp.set_corpus_total(key, total)


def _unavailable_ingest(key: str, reason: str) -> dict:
    _plan_counts(key, 0)
    sp.skip_phase(key, "fetching")
    sp.skip_phase(key, "embedding")
    return {
        "metadata_embed": dict(_EMPTY_EMBED),
        "fulltext": dict(_EMPTY_EMBED),
        "unavailable": reason,
    }


def _needs_fulltext(books: list) -> list:
    """Books whose file is on disk and whose full text is not archived yet.

    Done means ``fulltext`` chunks already exist, or the file was found to be a
    scan (``no_text_layer``). Without this every ingest re-extracted every book
    and appended another full copy of its chunks, since the pass itself never
    checks. To redo a book's full text (a different ``--max-pages``, say), purge
    its ``fetched_text`` first.
    """
    done = source_ids_with_chunk_pass(Source.CALIBRE, "fulltext") | source_ids_with_fetch_status(
        Source.CALIBRE, FetchStatus.NO_TEXT_LAYER
    )
    return [
        b
        for b in books
        if b.preferred_path and b.preferred_path.exists() and b.source_id not in done
    ]


def sync_calibre_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    from pka.db.migrate import init_db

    init_db()
    key = progress_key or "calibre"
    baseline = archive_document_count(Source.CALIBRE)
    books, unavailable = load_calibre_books()
    if unavailable:
        return unavailable_metadata(key, baseline, unavailable)
    pending = count_pending_metadata(Source.CALIBRE)
    sp.begin_metadata_sync(key, pending, baseline)
    books = take(books, Source.CALIBRE)
    stats = ingest_calibre_metadata(books, dry_run=dry_run, progress_key=key)
    log.info("Calibre metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_calibre_ingest(
    progress_key: str | None = None,
    dry_run: bool = False,
    max_pages: int | None = None,
) -> dict:
    key = progress_key or "calibre"
    books, unavailable = load_calibre_books()
    if unavailable:
        return _unavailable_ingest(key, unavailable)
    books = take(books, Source.CALIBRE)
    n = len(books)
    n_files = sum(1 for b in books if b.preferred_path and b.preferred_path.exists())
    _plan_counts(key, n_files or n)
    sp.skip_phase(key, "fetching")

    stats: dict = {}
    sp.set_phase(key, "embedding", n)
    stats["metadata_embed"] = ingest_calibre_books(
        books,
        dry_run=dry_run,
        progress_key=key,
    )
    log.info("Calibre metadata embed: %s", stats["metadata_embed"])
    if stats["metadata_embed"].get("stopped"):
        stats["stopped"] = stats["metadata_embed"]["stopped"]
        return stats

    file_books = _needs_fulltext(books)
    if not file_books:
        stats["fulltext"] = dict(_EMPTY_EMBED)
        return stats

    sp.set_phase(key, "embedding", len(file_books))
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
    return run_full_sync(
        meta,
        lambda: sync_calibre_ingest(
            progress_key=progress_key,
            dry_run=dry_run,
            max_pages=max_pages,
        ),
    )
