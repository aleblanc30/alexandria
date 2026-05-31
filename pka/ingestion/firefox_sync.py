"""Firefox sync — metadata and ingest (fetch + embed) as separate jobs."""
import asyncio
import logging

from pka.connectors.firefox import load_bookmarks
from pka.constants import Source
from pka.ingestion.fetcher import fetch_pending, _get_pending
from pka.ingestion import sync_progress as sp
from pka.ingestion.pending_metadata import archive_document_count, count_pending_metadata
from pka.ingestion.sync_helpers import should_stop
from pka.pipeline import ingest_fetched_texts, ingest_firefox_bookmarks

log = logging.getLogger(__name__)


def _stopped(stats: dict) -> str | None:
    return stats.get("stopped")


def _plan_counts(n_bm: int) -> None:
    sp.set_corpus_total("firefox", n_bm)


def sync_firefox_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    from pka.db.queries import init_db

    init_db()
    key = progress_key or "firefox"
    bookmarks = load_bookmarks()
    baseline = archive_document_count(Source.FIREFOX)
    pending = count_pending_metadata(Source.FIREFOX)
    sp.begin_metadata_sync(key, pending, baseline)
    stats = ingest_firefox_bookmarks(
        bookmarks, dry_run=dry_run, progress_key=key,
    )
    log.info("Firefox metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def _run_embed_phase(
    key: str,
    texts: dict[int, str],
    *,
    dry_run: bool,
    stats: dict,
) -> None:
    if texts and not dry_run:
        sp.set_phase(key, "embedding", len(texts))
        stats["embed"] = ingest_fetched_texts(
            texts, dry_run=dry_run, progress_key=key,
        )
        log.info("Firefox embed: %s", stats["embed"])
    else:
        sp.skip_phase(key, "embedding")
        stats["embed"] = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}


def sync_firefox_ingest(
    progress_key: str | None = None,
    fetch_limit: int | None = None,
    fetch_concurrency: int | None = None,
    dry_run: bool = False,
) -> dict:
    key = progress_key or "firefox"
    stats: dict = {}
    pending = _get_pending(fetch_limit)
    n_pending = len(pending)
    n_bm = len(load_bookmarks())
    _plan_counts(n_bm)

    if n_pending == 0:
        sp.skip_phase(key, "fetching")
        sp.skip_phase(key, "embedding")
        stats["fetch"] = {"fetched": 0, "skipped": 0, "unfetchable": 0, "texts": {}}
        stats["embed"] = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}
        return stats

    sp.set_phase(key, "fetching", n_pending)
    fetch_stats = asyncio.run(fetch_pending(
        limit=fetch_limit,
        concurrency=fetch_concurrency,
        progress_key=key,
    ))
    stats["fetch"] = {k: v for k, v in fetch_stats.items() if k != "texts"}
    log.info("Firefox fetch: %s", stats["fetch"])

    texts = fetch_stats.get("texts") or {}
    _run_embed_phase(key, texts, dry_run=dry_run, stats=stats)

    if stop := fetch_stats.get("stopped"):
        stats["stopped"] = stop
    elif should_stop(key):
        stats["stopped"] = should_stop(key)
    elif stop := _stopped(stats.get("embed")):
        stats["stopped"] = stop

    return stats


def sync_firefox(
    progress_key: str | None = None,
    fetch_limit: int | None = None,
    fetch_concurrency: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Full pipeline (metadata then ingest). Kept for scripts/tests."""
    key = progress_key or "firefox"
    meta = sync_firefox_metadata(progress_key=key, dry_run=dry_run)
    if meta.get("stopped"):
        return meta
    ingest = sync_firefox_ingest(
        progress_key=key,
        fetch_limit=fetch_limit,
        fetch_concurrency=fetch_concurrency,
        dry_run=dry_run,
    )
    return {**meta, **ingest}
