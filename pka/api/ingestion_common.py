"""Helpers shared by the ``/ingestion`` routers.

Source validation, the "no sync is running" guard that purges and re-chunks
share, and the progress re-seed every job and purge ends with.
"""

from fastapi import HTTPException

from pka.constants import ALL_SOURCES
from pka.ingestion import progress as sp
from pka.ingestion.progress.baselines import seed_progress_from_db


def require_source(source: str) -> str:
    """Validate a source name against the known sources, raising 400 otherwise."""
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    return source


def require_nothing_running(source: str | None) -> None:
    """Refuse a purge or re-chunk that could race a live worker.

    A source-scoped purge only has to wait for that source; an archive-wide one
    touches rows every sync writes, so it waits for all of them.
    """
    busy = [s for s in ([source] if source else ALL_SOURCES) if sp.is_running(s)]
    if busy:
        raise HTTPException(409, f"Stop the running sync for {', '.join(busy)} before purging")


def seed_baselines(src: str) -> None:
    """Recompute ``src``'s progress baseline from the archive after it changed."""
    from pka.db.engine import get_engine
    from pka.ingestion.pending_metadata import invalidate_source_probes

    # Source/archive state just changed (job start, finish, or purge); drop the
    # cached source-probe counts so the next status/progress poll recomputes.
    invalidate_source_probes(src)
    seed_progress_from_db(get_engine(), src)
