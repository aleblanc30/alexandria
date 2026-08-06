"""``/ingestion`` — status overview and per-source background sync triggers."""
import logging
import threading

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException

from pka.api import source_paths as spaths
from pka.api.dependencies import get_engine
from pka.api.schemas.ingestion import SourcePathUpdate
from pka.constants import ALL_SOURCES, FetchStatus, Source
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


def require_source(source: str) -> str:
    """Validate a source name against the known sources, raising 400 otherwise."""
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    return source


@router.get("/status")
async def ingestion_status(engine=Depends(get_engine)):
    return build_ingestion_status(engine)


@router.get("/sync/progress")
async def sync_progress(source: str | None = None, engine=Depends(get_engine)):
    """Return live progress for one or all ingestion sync jobs."""
    if source:
        require_source(source)
    targets = [source] if source else ALL_SOURCES
    for src in targets:
        apply_progress_baselines(engine, src)
    return sp.snapshot(source)


# ── Image folders (list-valued source) ──────────────────────────────────────
# Declared before the ``/sources/{source}/…`` routes so the static ``image``
# segment is matched by these handlers rather than the single-path fallbacks.

@router.get("/sources/image/dirs")
async def get_image_dirs():
    return {"dirs": spaths.get_image_dirs()}


@router.post("/sources/image/dirs", status_code=200)
def add_image_dir(body: SourcePathUpdate):
    if not body.path.strip():
        raise HTTPException(400, "Path must not be empty")
    try:
        return {"dirs": spaths.add_image_dir(body.path)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/sources/image/dirs", status_code=200)
def remove_image_dir(body: SourcePathUpdate):
    if not body.path.strip():
        raise HTTPException(400, "Path must not be empty")
    try:
        return {"dirs": spaths.remove_image_dir(body.path)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sources/image/dirs/browse", status_code=200)
def browse_image_dir():
    try:
        chosen = spaths.open_image_dir_picker()
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    return {"path": chosen}


def _reject_image_single_path(source: str) -> None:
    """The image source is list-valued; steer callers to the ``/dirs`` routes."""
    if source == Source.IMAGE:
        raise HTTPException(400, "Image source uses /sources/image/dirs")


@router.get("/sources/{source}/path")
async def get_path(source: str):
    require_source(source)
    _reject_image_single_path(source)
    return spaths.get_source_path(source)


@router.put("/sources/{source}/path")
def update_path(source: str, body: SourcePathUpdate):
    require_source(source)
    _reject_image_single_path(source)
    if not body.path.strip():
        raise HTTPException(400, "Path must not be empty")
    try:
        return spaths.set_source_path(source, body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sources/{source}/browse", status_code=200)
def browse_path(source: str):
    require_source(source)
    _reject_image_single_path(source)
    try:
        chosen = spaths.open_native_picker(source)
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    return {"path": chosen}


@router.post("/sources/{source}/purge")
def purge_source_endpoint(source: str):
    """Delete every archived row (and vectors) for ``source``.

    Refuses while a sync is running so a purge can't race a live worker.
    """
    require_source(source)
    if sp.is_running(source):
        raise HTTPException(409, f"Stop the running sync for {source} before purging")

    from pka.cli.purge_source import purge_source
    from pka.db.queries import init_db

    init_db()
    counts = purge_source(source)
    sp.reset(source)
    _seed_baselines(source)
    return {"status": "purged", "source": source, "counts": counts}


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


def _run_ingestion_job(src: str, *, begin_job: sp.JobKind, error_label: str, run, pre_begin=None) -> None:
    """Shared metadata/ingest/full job skeleton: init, begin, run handler, finish."""
    from pka.db.queries import get_engine, init_db

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, begin_job, phase="loading")
    if pre_begin is not None:
        pre_begin(src)
    try:
        _finish_job(src, run())
    except Exception as exc:
        log.exception("%s failed for %s", error_label, src)
        sp.finish(src, error=str(exc))
        seed_progress_from_db(get_engine(), src)


def _ingest_pre_begin(src: str) -> None:
    from pka.ingestion.pending_metadata import source_corpus_size

    if src == Source.FIREFOX:
        sp.clear_embed_progress(src)
    else:
        sp.begin_ingest(src, source_corpus_size(src))


def _sync_metadata(src: str) -> None:
    _run_ingestion_job(
        src, begin_job="metadata", error_label="Metadata sync",
        run=lambda: require_handlers(src).sync_metadata(progress_key=src),
    )


def _sync_ingest(src: str) -> None:
    _run_ingestion_job(
        src, begin_job="ingest", error_label="Ingest",
        run=lambda: require_handlers(src).sync_ingest(progress_key=src),
        pre_begin=_ingest_pre_begin,
    )


def _sync(src: str) -> None:
    """Background entry point for ``POST /ingestion/sync/{source}`` (full pipeline)."""
    _run_ingestion_job(
        src, begin_job="metadata", error_label="Ingestion sync",
        run=lambda: require_handlers(src).sync_full(progress_key=src),
    )


_JOB_TARGETS = {
    "metadata": lambda src: _sync_metadata(src),
    "ingest": lambda src: _sync_ingest(src),
    "full": lambda src: _sync(src),
}


_workers: dict[str, threading.Thread] = {}
_workers_lock = threading.Lock()

_FORCE_STOP_TIMEOUT = 30.0  # seconds to wait for a cancelled worker to exit


def _stop_running_job(src: str) -> None:
    """Cancel the active worker for ``src`` and wait until it exits."""
    sp.request_cancel(src)
    with _workers_lock:
        old = _workers.get(src)
    if old is not None and old.is_alive():
        old.join(timeout=_FORCE_STOP_TIMEOUT)
        if old.is_alive():
            raise HTTPException(
                409, f"Previous sync for {src} has not stopped yet; try again",
            )


def _queue_job(src: str, job: str, force: bool) -> dict:
    if sp.is_running(src):
        if not force:
            raise HTTPException(409, f"Sync already in progress for {src}")
        _stop_running_job(src)
    if force:
        sp.reset(src)
    thread = threading.Thread(
        target=_JOB_TARGETS[job],
        args=(src,),
        daemon=True,
        name=f"alexandria-sync-{src}-{job}",
    )
    with _workers_lock:
        _workers[src] = thread
    thread.start()
    return {"status": "queued", "source": src, "job": job}


# Plain ``def`` endpoints: _queue_job may block while joining a cancelled
# worker, so FastAPI must run these in its threadpool, not on the event loop.
@router.post("/sync/{source}", status_code=202)
def sync_source(source: str, force: bool = False):
    require_source(source)
    return _queue_job(source, "full", force)


@router.post("/sync/{source}/metadata", status_code=202)
def sync_metadata(source: str, force: bool = False):
    require_source(source)
    return _queue_job(source, "metadata", force)


@router.post("/sync/{source}/ingest", status_code=202)
def sync_ingest(source: str, force: bool = False):
    require_source(source)
    return _queue_job(source, "ingest", force)


@router.post("/sync/{source}/pause", status_code=202)
async def pause_sync(source: str):
    require_source(source)
    if not sp.request_pause(source):
        raise HTTPException(409, f"No active sync to pause for {source}")
    return {"status": "pause_requested", "source": source}


@router.post("/sync/{source}/cancel", status_code=202)
async def cancel_sync(source: str):
    require_source(source)
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
        name="alexandria-rebuild-vectors",
    ).start()
    return {"status": "queued"}
