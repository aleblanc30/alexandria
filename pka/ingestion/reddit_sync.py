"""Reddit sync — metadata (persist rows) and ingest (embed inline + fetch links).

Unlike file-based sources, ``count_pending_metadata`` / ``source_corpus_size``
do not probe Reddit on status polls (that would hit the API). The metadata job
computes its own pending count from the freshly loaded saved list instead.
"""
import asyncio
import logging
from functools import partial

from pka.connectors.reddit import RedditSaved, load_saved, load_saved_from_archive
from pka.constants import Source
from pka.db.queries import all_reddit_items, document_index, init_db, source_ingest_queue
from pka.ingestion import progress as sp
from pka.ingestion.dev_limits import take
from pka.ingestion.fetcher import fetch_and_embed_pending
from pka.ingestion.pending_metadata import archive_document_count
from pka.ingestion.runners.reddit import (
    embed_fetched_text,
    ingest_reddit_embed,
    ingest_reddit_metadata,
)
from pka.ingestion.sync_shared import run_full_sync

log = logging.getLogger(__name__)

_EMPTY_FETCH = {"fetched": 0, "skipped": 0, "unfetchable": 0}


def _pending_count(saved: list[RedditSaved], known: set[str]) -> int:
    return sum(1 for s in saved if s.source_id not in known)


def _load_saved_from_db() -> list[RedditSaved]:
    """Rebuild the saved list from already-persisted documents + reddit_items.

    The ingest phase needs a body for every item still missing chunks, not just
    the newest saves, which used to mean a second full feed walk right after the
    metadata phase's own walk. The metadata phase already writes that same body
    (plus kind/subreddit/permalink/external_url) for every item it sees — new or
    already known — so reading it back costs nothing against Reddit's API.
    """
    return [
        RedditSaved(
            source_id=row["source_id"],
            kind=row["kind"],
            title=row["title"] or "",
            permalink=row["permalink"] or "",
            external_url=row["external_url"],
            subreddit=row["subreddit"] or "",
            body=row["body"],
            date_added=row["date_added"],
        )
        for row in all_reddit_items()
    ]


def sync_reddit_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
    backfill: bool = False,
    from_archive: bool = False,
) -> dict:
    """Persist saved items.

    Incremental by default: the feed is ordered by save time, so the walk stops
    at the first item already in the archive — normally one request, no throttle
    sleep. ``backfill=True`` walks the whole feed instead, for a first run or to
    fill gaps a failed run left behind.

    ``from_archive=True`` replays ``data/reddit/saved.jsonl`` instead of polling,
    which is how a rebuilt database is refilled once the feed is unreachable.
    """
    init_db()
    key = progress_key or "reddit"
    known = set(document_index(Source.REDDIT))
    saved = take(
        load_saved_from_archive() if from_archive
        else load_saved(known_ids=known, stop_on_known=not backfill),
        Source.REDDIT,
    )
    baseline = archive_document_count(Source.REDDIT)
    pending = _pending_count(saved, known)
    sp.begin_metadata_sync(key, pending, baseline)
    stats = ingest_reddit_metadata(saved, dry_run=dry_run, progress_key=key)
    log.info("Reddit metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_reddit_ingest(
    progress_key: str | None = None,
    dry_run: bool = False,
    skip_existing: bool = True,
    from_archive: bool = False,
) -> dict:
    key = progress_key or "reddit"
    # This phase needs bodies for every item still missing chunks, not just the
    # most recently saved — but that data is already in SQLite from the
    # metadata phase, so it reads it back instead of polling the live feed a
    # second time (from_archive replays the same jsonl the metadata phase would).
    saved = take(
        load_saved_from_archive() if from_archive else _load_saved_from_db(), Source.REDDIT,
    )
    sp.set_corpus_total(key, len(saved))

    stats: dict = {}

    # Phase 1: fetch external link posts, embedding each inline (Firefox pattern).
    fetch_stats, fetch_stopped = _fetch_link_posts(key, dry_run=dry_run)
    stats["fetch"] = fetch_stats

    # Phase 2: embed the inline body of self-posts and comments.
    inline = [s for s in saved if s.external_url is None]
    sp.set_phase(key, "embedding", len(inline))
    embed_stats = ingest_reddit_embed(
        inline, skip_existing=skip_existing, dry_run=dry_run, progress_key=key,
    )
    log.info("Reddit embed: %s", embed_stats)
    stats["embed"] = embed_stats

    if fetch_stopped:
        stats["stopped"] = fetch_stopped
    elif embed_stats.get("stopped"):
        stats["stopped"] = embed_stats["stopped"]
    return stats


def _fetch_link_posts(key: str, *, dry_run: bool) -> tuple[dict, str | None]:
    """Fetch + embed pending Reddit link posts. Returns (fetch_stats, stopped)."""
    work = source_ingest_queue(Source.REDDIT, None)
    if not work:
        sp.skip_phase(key, "fetching")
        return dict(_EMPTY_FETCH), None

    sp.set_phase(key, "fetching", len(work))
    embed_fn = None if dry_run else partial(
        embed_fetched_text, skip_existing=True, dry_run=dry_run,
    )
    result = asyncio.run(fetch_and_embed_pending(
        source=Source.REDDIT,
        limit=None,
        progress_key=key,
        embed_fn=embed_fn,
        dry_run=dry_run,
    ))
    fetch_stats = {k: v for k, v in result.items() if k not in ("embed", "stopped")}
    log.info("Reddit fetch: %s", fetch_stats)
    return fetch_stats, result.get("stopped")


def sync_reddit(
    progress_key: str | None = None,
    dry_run: bool = False,
    backfill: bool = False,
    from_archive: bool = False,
) -> dict:
    """Full pipeline (metadata then ingest). Kept for scripts/tests."""
    key = progress_key or "reddit"
    meta = sync_reddit_metadata(
        progress_key=key, dry_run=dry_run, backfill=backfill, from_archive=from_archive,
    )
    return run_full_sync(
        meta,
        lambda: sync_reddit_ingest(
            progress_key=key, dry_run=dry_run, from_archive=from_archive,
        ),
    )
