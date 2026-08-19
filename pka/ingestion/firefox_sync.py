"""Firefox sync — metadata and ingest (fetch + embed) as separate jobs."""
import asyncio
import logging
from functools import partial

from pka.connectors.firefox import load_bookmarks
from pka.constants import Source
from pka.db.queries import firefox_ingest_queue
from pka.ingestion import progress as sp
from pka.ingestion.dev_limits import take
from pka.ingestion.fetcher import fetch_and_embed_pending, reset_unfetchable_for_fetch
from pka.ingestion.pending_metadata import archive_document_count, count_pending_metadata
from pka.ingestion.progress import should_stop
from pka.ingestion.runners.firefox import embed_fetched_text, ingest_firefox_bookmarks
from pka.ingestion.sync_shared import run_full_sync

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
    bookmarks = take(load_bookmarks(), Source.FIREFOX)
    baseline = archive_document_count(Source.FIREFOX)
    pending = count_pending_metadata(Source.FIREFOX)
    sp.begin_metadata_sync(key, pending, baseline)
    stats = ingest_firefox_bookmarks(
        bookmarks, dry_run=dry_run, progress_key=key,
    )
    log.info("Firefox metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_firefox_ingest(
    progress_key: str | None = None,
    fetch_limit: int | None = None,
    fetch_concurrency: int | None = None,
    dry_run: bool = False,
) -> dict:
    key = progress_key or "firefox"
    stats: dict = {}

    reset_unfetchable_for_fetch()

    work = firefox_ingest_queue(fetch_limit)
    n_work = len(work)
    n_bm = len(take(load_bookmarks(), Source.FIREFOX))
    _plan_counts(n_bm)

    if n_work == 0:
        sp.skip_phase(key, "fetching")
        stats["fetch"] = {"fetched": 0, "skipped": 0, "unfetchable": 0}
        stats["embed"] = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}
        return stats

    sp.set_phase(key, "fetching", n_work)

    embed_fn = None if dry_run else partial(
        embed_fetched_text,
        skip_existing=True,
        dry_run=dry_run,
    )
    result = asyncio.run(fetch_and_embed_pending(
        limit=fetch_limit,
        concurrency=fetch_concurrency,
        progress_key=key,
        embed_fn=embed_fn,
        dry_run=dry_run,
    ))

    stats["fetch"] = {
        k: v for k, v in result.items() if k not in ("embed", "stopped")
    }
    stats["embed"] = result.get("embed") or {
        "processed": 0, "skipped": 0, "failed": 0, "chunks": 0,
    }
    log.info("Firefox fetch: %s", stats["fetch"])
    log.info("Firefox embed: %s", stats["embed"])

    if stop := result.get("stopped"):
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
    return run_full_sync(meta, lambda: sync_firefox_ingest(
        progress_key=key,
        fetch_limit=fetch_limit,
        fetch_concurrency=fetch_concurrency,
        dry_run=dry_run,
    ))
