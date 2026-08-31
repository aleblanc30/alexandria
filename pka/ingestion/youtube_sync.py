"""YouTube sync — metadata and embed as separate jobs.

Mirrors the Zotero two-phase flow (no async fetch phase): metadata import then
embed. ``pending`` and corpus totals are computed from the videos loaded for the
job, so status polling never touches the YouTube Data API (see
``pending_metadata`` where YouTube returns 0 for the network-free counts).
"""

import logging

from pka.constants import Source
from pka.db.queries import document_index
from pka.ingestion import progress as sp
from pka.ingestion.dev_limits import take
from pka.ingestion.pending_metadata import archive_document_count
from pka.ingestion.runners.youtube import ingest_youtube_embed, ingest_youtube_metadata
from pka.ingestion.source_access import try_load_youtube_videos
from pka.ingestion.sync_shared import run_full_sync, unavailable_metadata

log = logging.getLogger(__name__)

_EMPTY_EMBED = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}


def sync_youtube_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    from pka.db.queries import init_db

    init_db()
    key = progress_key or "youtube"
    baseline = archive_document_count(Source.YOUTUBE)

    videos, unavailable = try_load_youtube_videos()
    if unavailable:
        log.info("YouTube metadata skipped: %s", unavailable)
        return unavailable_metadata(key, baseline, unavailable)

    videos = take(videos, Source.YOUTUBE)
    known = set(document_index(Source.YOUTUBE))
    pending = sum(1 for v in videos if v.source_id not in known)
    sp.begin_metadata_sync(key, pending, baseline)

    stats = ingest_youtube_metadata(videos, dry_run=dry_run, progress_key=key)
    log.info("YouTube metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_youtube_ingest(
    progress_key: str | None = None,
    dry_run: bool = False,
    skip_existing: bool = True,
) -> dict:
    key = progress_key or "youtube"

    videos, unavailable = try_load_youtube_videos()
    if unavailable:
        sp.skip_phase(key, "fetching")
        sp.skip_phase(key, "embedding")
        log.info("YouTube embed skipped: %s", unavailable)
        return {"embed": dict(_EMPTY_EMBED), "unavailable": unavailable}

    videos = take(videos, Source.YOUTUBE)
    n = len(videos)
    sp.set_corpus_total(key, n)
    sp.skip_phase(key, "fetching")
    sp.set_phase(key, "embedding", n)

    if not videos:
        log.info("YouTube embed: nothing to do")
        return {"embed": dict(_EMPTY_EMBED)}

    stats = ingest_youtube_embed(
        videos,
        skip_existing=skip_existing,
        dry_run=dry_run,
        progress_key=key,
    )
    log.info("YouTube embed: %s", stats)
    return {"embed": stats, "stopped": stats.get("stopped")}


def sync_youtube(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Full sync (metadata + embed). Kept for scripts/tests."""
    key = progress_key or "youtube"
    meta = sync_youtube_metadata(progress_key=key, dry_run=dry_run)
    return run_full_sync(
        meta,
        lambda: sync_youtube_ingest(progress_key=key, dry_run=dry_run),
    )


__all__ = [
    "sync_youtube",
    "sync_youtube_ingest",
    "sync_youtube_metadata",
]
