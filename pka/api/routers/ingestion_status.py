"""``/ingestion`` — status overview, live progress and its event stream.

Also the per-domain and unfetchable-URL reports behind the status page.
"""

import asyncio
import json
import time

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from pka.api.dependencies import get_engine
from pka.api.ingestion_common import require_source
from pka.api.schemas.ingestion import DomainTopLists
from pka.constants import ALL_SOURCES, FetchStatus
from pka.db.schema import documents, fetch_log
from pka.domains import build_domain_top_lists
from pka.ingestion.progress.baselines import (
    build_ingestion_status,
    display_snapshot,
    source_counts,
)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.get("/status")
async def ingestion_status(engine=Depends(get_engine)):
    return build_ingestion_status(engine)


@router.get("/sync/progress")
async def sync_progress(source: str | None = None, engine=Depends(get_engine)):
    """Return live progress for one or all ingestion sync jobs."""
    if source:
        require_source(source)
    targets = [source] if source else ALL_SOURCES
    return {src: display_snapshot(engine, src) for src in targets}


# One open connection replaces the 500 ms poll loop. Events carry the per-source
# slice of ``/ingestion/status`` too, so a running sync hits no other endpoint.

_EVENT_INTERVAL_SECONDS = 0.2  # coalesce to at most 5 events/sec


_IDLE_INTERVAL_SECONDS = 1.0


_HEARTBEAT_SECONDS = 15.0


# Progress is in memory; the counts alongside it cost queries, and they move far
# more slowly than a progress bar does.
_COUNTS_INTERVAL_SECONDS = 1.0


# A client opens the stream before the POST that starts the job has been picked
# up, so "not running yet" cannot mean "nothing to watch" right away.
_START_GRACE_SECONDS = 15.0


async def _progress_events(engine, src: str):
    started = time.monotonic()
    last_data: str | None = None
    last_emit = 0.0
    counts: dict | None = None
    counts_at = 0.0
    while True:
        if counts is None or time.monotonic() - counts_at >= _COUNTS_INTERVAL_SECONDS:
            counts = await run_in_threadpool(source_counts, engine, src)
            counts_at = time.monotonic()
        progress = await run_in_threadpool(display_snapshot, engine, src)
        payload = {"progress": progress, "counts": counts}
        running = progress["status"] == "running"
        data = json.dumps(payload)
        now = time.monotonic()
        if data != last_data:
            yield f"data: {data}\n\n"
            last_data, last_emit = data, now
        elif now - last_emit >= _HEARTBEAT_SECONDS:
            # Comment frame — keeps proxies from closing an idle connection.
            yield ": keep-alive\n\n"
            last_emit = now
        if not running and now - started >= _START_GRACE_SECONDS:
            return
        await asyncio.sleep(_EVENT_INTERVAL_SECONDS if running else _IDLE_INTERVAL_SECONDS)


@router.get("/sync/events")
async def sync_events(source: str, engine=Depends(get_engine)):
    """Stream one source's progress until its job reaches a terminal state."""
    require_source(source)
    return StreamingResponse(
        _progress_events(engine, source),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/domains", response_model=DomainTopLists)
def domain_top_lists(source: str | None = None, limit: int = 10):
    """Top domains by document count and by unfetchable count; ``limit=0`` returns all."""
    if source:
        require_source(source)
    if not 0 <= limit <= 100:
        raise HTTPException(400, "limit must be between 0 and 100")
    return build_domain_top_lists(source=source, limit=limit or None)


@router.get("/unfetchable")
async def unfetchable_urls(
    limit: int = 50,
    offset: int = 0,
    engine=Depends(get_engine),
):
    with engine.connect() as con:
        rows = con.execute(
            sa.select(
                documents.c.id,
                documents.c.title,
                documents.c.url_or_path,
                fetch_log.c.http_status,
                fetch_log.c.error_msg,
                fetch_log.c.timestamp,
            )
            .join(fetch_log, fetch_log.c.document_id == documents.c.id)
            .where(documents.c.fetch_status == str(FetchStatus.UNFETCHABLE))
            .order_by(fetch_log.c.timestamp.desc())
            .limit(limit)
            .offset(offset)
        ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "url": r[2],
            "http_status": r[3],
            "error": r[4],
            "timestamp": r[5],
        }
        for r in rows
    ]
