"""``/ingestion`` — status overview and per-source background sync triggers."""
import logging
import threading

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException

from pka.api.dependencies import get_engine
from pka.constants import ALL_SOURCES, FetchStatus
from pka.db.schema import documents, fetch_log
from pka.ingestion import sync_progress as sp
from pka.ingestion.progress_baselines import (
    apply_progress_baselines,
    build_ingestion_status,
    seed_progress_from_db,
)
from pka.ingestion.registry import require_handlers

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.get("/status")
async def ingestion_status(engine=Depends(get_engine)):
    return build_ingestion_status(engine)


@router.get("/sync/progress")
async def sync_progress(source: str | None = None, engine=Depends(get_engine)):
    """Return live progress for one or all ingestion sync jobs."""
    if source and source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    targets = [source] if source else ALL_SOURCES
    for src in targets:
        apply_progress_baselines(engine, src)
    return sp.snapshot(source)


@router.get("/unfetchable")
async def unfetchable_urls(
    limit: int = 50, offset: int = 0,
    engine=Depends(get_engine),
):
    with engine.connect() as con:
        rows = con.execute(
            sa.select(
                documents.c.id, documents.c.title,
                documents.c.url_or_path, fetch_log.c.http_status,
                fetch_log.c.error_msg, fetch_log.c.timestamp,
            )
            .join(fetch_log, fetch_log.c.document_id == documents.c.id)
            .where(documents.c.fetch_status == str(FetchStatus.UNFETCHABLE))
            .order_by(fetch_log.c.timestamp.desc())
            .limit(limit).offset(offset)
        ).fetchall()
    return [
        {"id": r[0], "title": r[1], "url": r[2],
         "http_status": r[3], "error": r[4], "timestamp": r[5]}
        for r in rows
    ]


def _extract_stopped(stats: dict | None) -> str | None:
    if not stats:
        return None
    if isinstance(stats.get("stopped"), str):
        return stats["stopped"]
    for value in stats.values():
        if isinstance(value, dict) and value.get("stopped"):
            return value["stopped"]
    return None


def _finish_job(src: str, stats: dict | None, *, error: str | None = None) -> None:
    if stats:
        sp.set_job_result(src, stats)
    if error:
        sp.finish(src, error=error)
        _seed_baselines(src)
        return
    stopped = _extract_stopped(stats)
    if stopped:
        sp.finish(src, stopped=stopped)  # type: ignore[arg-type]
    else:
        sp.finish(src)
    _seed_baselines(src)


def _seed_baselines(src: str) -> None:
    from pka.db.queries import get_engine

    seed_progress_from_db(get_engine(), src)


def _sync_metadata(src: str) -> None:
    from pka.db.queries import get_engine, init_db

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, "metadata", phase="loading")
    try:
        stats = require_handlers(src).sync_metadata(progress_key=src)
        _finish_job(src, stats)
    except Exception as exc:
        log.exception("Metadata sync failed for %s", src)
        sp.finish(src, error=str(exc))
        seed_progress_from_db(get_engine(), src)


def _sync_ingest(src: str) -> None:
    from pka.db.queries import get_engine, init_db

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, "ingest", phase="loading")
    try:
        stats = require_handlers(src).sync_ingest(progress_key=src)
        _finish_job(src, stats)
    except Exception as exc:
        log.exception("Ingest failed for %s", src)
        sp.finish(src, error=str(exc))
        seed_progress_from_db(get_engine(), src)


def _sync(src: str) -> None:
    """Background entry point for ``POST /ingestion/sync/{source}`` (full pipeline)."""
    from pka.db.queries import get_engine, init_db

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, "metadata", phase="loading")
    try:
        stats = require_handlers(src).sync_full(progress_key=src)
        _finish_job(src, stats)
    except Exception as exc:
        log.exception("Ingestion sync failed for %s", src)
        sp.finish(src, error=str(exc))
        seed_progress_from_db(get_engine(), src)


def _queue_job(src: str, job: sp.JobKind, force: bool) -> dict:
    if sp.is_running(src) and not force:
        raise HTTPException(409, f"Sync already in progress for {src}")
    if force:
        sp.reset(src)
    target = _sync_metadata if job == "metadata" else _sync_ingest
    threading.Thread(
        target=target,
        args=(src,),
        daemon=True,
        name=f"pka-sync-{src}-{job}",
    ).start()
    return {"status": "queued", "source": src, "job": job}


async def _run_sync_threaded(src: str) -> None:
    """Deprecated wrapper — kept for tests that patch ``_sync``."""
    _sync(src)


@router.post("/sync/{source}", status_code=202)
async def sync_source(source: str, force: bool = False):
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    if sp.is_running(source) and not force:
        raise HTTPException(409, f"Sync already in progress for {source}")
    if force:
        sp.reset(source)
    sp.begin_job(source, "metadata", phase="starting")
    threading.Thread(
        target=_sync,
        args=(source,),
        daemon=True,
        name=f"pka-sync-{source}",
    ).start()
    return {"status": "queued", "source": source}


@router.post("/sync/{source}/metadata", status_code=202)
async def sync_metadata(source: str, force: bool = False):
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    return _queue_job(source, "metadata", force)


@router.post("/sync/{source}/ingest", status_code=202)
async def sync_ingest(source: str, force: bool = False):
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    return _queue_job(source, "ingest", force)


@router.post("/sync/{source}/pause", status_code=202)
async def pause_sync(source: str):
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    if not sp.request_pause(source):
        raise HTTPException(409, f"No active sync to pause for {source}")
    return {"status": "pause_requested", "source": source}


@router.post("/sync/{source}/cancel", status_code=202)
async def cancel_sync(source: str):
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    if not sp.request_cancel(source):
        raise HTTPException(409, f"No active sync to cancel for {source}")
    return {"status": "cancel_requested", "source": source}


_rebuild_lock = threading.Lock()
_rebuild_running = False


@router.post("/rebuild-vectors", status_code=202)
async def rebuild_vectors():
    """Rebuild the Chroma chunk index from SQLite chunk text."""
    global _rebuild_running
    with _rebuild_lock:
        if _rebuild_running:
            raise HTTPException(409, "Vector rebuild already in progress")
        _rebuild_running = True

    def _run() -> None:
        global _rebuild_running
        from pka.storage.vector_store import rebuild_from_chunks

        try:
            stats = rebuild_from_chunks()
            log.info("Vector rebuild finished: %s", stats)
        except Exception:
            log.exception("Vector rebuild failed")
        finally:
            with _rebuild_lock:
                _rebuild_running = False

    threading.Thread(
        target=_run,
        daemon=True,
        name="pka-rebuild-vectors",
    ).start()
    return {"status": "queued"}
