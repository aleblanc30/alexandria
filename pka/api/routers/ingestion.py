"""``/ingestion`` — status overview and per-source background sync triggers."""

import asyncio
import json
import logging
import threading
import time

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from pka.api import source_paths as spaths
from pka.api.dependencies import get_engine
from pka.api.schemas.ingestion import DomainTopLists, SourcePathUpdate
from pka.constants import ALL_SOURCES, FetchStatus, Source
from pka.db.schema import documents, fetch_log
from pka.domains import build_domain_top_lists
from pka.ingestion import progress as sp
from pka.ingestion.progress.baselines import (
    build_ingestion_status,
    display_snapshot,
    seed_progress_from_db,
    source_counts,
)
from pka.ingestion.registry import phase_spec, require_handlers

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
    return {src: display_snapshot(engine, src) for src in targets}


# ── Progress stream ─────────────────────────────────────────────────────────
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
    try:
        return spaths.get_source_path(source)
    except ValueError as exc:
        # Credential-based sources (e.g. Reddit) have no filesystem path.
        raise HTTPException(400, str(exc)) from exc


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
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    return {"path": chosen}


@router.post("/sources/{source}/purge")
def purge_source_endpoint(source: str, include_user_data: bool = False):
    """Delete every archived row (and vectors) for ``source``.

    By default, manually-applied/learned tags and reading-list entries survive
    (see ``PURGE_AND_PROVENANCE_PLAN.md`` §5.1); pass ``include_user_data=true``
    to remove those too. Refuses while a sync is running so a purge can't race
    a live worker.
    """
    require_source(source)
    if sp.is_running(source):
        raise HTTPException(409, f"Stop the running sync for {source} before purging")

    from pka.cli.purge_source import purge_source
    from pka.db.queries import init_db

    init_db()
    counts = purge_source(source, include_user_data=include_user_data)
    sp.reset(source)
    _seed_baselines(source)
    return {"status": "purged", "source": source, "counts": counts}


def _require_nothing_running(source: str | None) -> None:
    """Refuse a purge that could race a live worker.

    A source-scoped purge only has to wait for that source; an archive-wide one
    touches rows every sync writes, so it waits for all of them.
    """
    busy = [s for s in ([source] if source else ALL_SOURCES) if sp.is_running(s)]
    if busy:
        raise HTTPException(409, f"Stop the running sync for {', '.join(busy)} before purging")


@router.get("/purge-targets")
def purge_targets(source: str | None = None):
    """The purge registry with live dry-run counts — one row per button."""
    if source:
        require_source(source)
    from pka.db.queries import init_db
    from pka.purge import describe_targets

    init_db()
    return {"source": source, "targets": describe_targets(source)}


@router.get("/enrichment-runs")
def enrichment_runs_list(kind: str | None = None, limit: int = 100):
    """What ran, when, with which model, and at what cost in provider traffic.

    The provenance surface behind the purge filters below: a run listed here is
    a `run_id` a purge can target.
    """
    from pka.db.queries import init_db
    from pka.enrichment_runs import list_runs

    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    init_db()
    return {"runs": list_runs(kind=kind, limit=limit)}


@router.post("/purge/{key}")
def purge_target_endpoint(
    key: str,
    source: str | None = None,
    dry_run: bool = False,
    run_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    unknown: bool = False,
):
    """Purge one registered target, optionally scoped by source and provenance.

    ``run_id`` / ``provider`` / ``model`` narrow to what a particular backend
    produced; ``unknown=true`` selects the pre-provenance backlog. Targets whose
    artifact carries no run stamp reject those filters with a 400 rather than
    silently widening the purge.
    """
    if source:
        require_source(source)
    from pka.db.queries import init_db
    from pka.purge import purge_target

    if not dry_run:
        _require_nothing_running(source)

    init_db()
    try:
        counts = purge_target(
            key,
            source=source,
            dry_run=dry_run,
            run_id=run_id,
            provider=provider,
            model=model,
            unknown=unknown,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not dry_run:
        # Counts the status view reports just changed under it.
        for src in [source] if source else ALL_SOURCES:
            _seed_baselines(src)
    return {
        "status": "counted" if dry_run else "purged",
        "target": key,
        "source": source,
        "counts": counts,
    }


_enrich_lock = threading.Lock()
_enrich_running = False


@router.post("/enrich", status_code=202)
def enrich_endpoint(kind: str = "summary", source: str | None = None):
    """Re-run an enrichment pass over documents missing that artifact.

    The retrigger for ``purge summaries``: its skip gate is "has any chunk",
    which a summary purge leaves true, so re-syncing would not regenerate it
    (PURGE_AND_PROVENANCE_PLAN.md §5.2.1).
    """
    global _enrich_running
    if source:
        require_source(source)
    from pka.ingestion.enrich import KINDS

    if kind not in KINDS:
        raise HTTPException(400, f"Unknown enrichment kind: {kind}")

    with _enrich_lock:
        if _enrich_running:
            raise HTTPException(409, "An enrichment pass is already in progress")
        _enrich_running = True

    def _run() -> None:
        global _enrich_running
        from pka.db.queries import init_db
        from pka.ingestion.enrich import enrich

        try:
            init_db()
            stats = enrich(kind, source=source)
            log.info("Enrichment pass %s finished: %s", kind, stats)
        except Exception:
            log.exception("Enrichment pass %s failed", kind)
        finally:
            with _enrich_lock:
                _enrich_running = False

    threading.Thread(target=_run, daemon=True, name=f"alexandria-enrich-{kind}").start()
    return {"status": "queued", "kind": kind, "source": source}


@router.get("/domains", response_model=DomainTopLists)
def domain_top_lists(source: str | None = None, limit: int = 10):
    """Top domains by document count and by unfetchable count."""
    if source:
        require_source(source)
    if not 1 <= limit <= 100:
        raise HTTPException(400, "limit must be between 1 and 100")
    return build_domain_top_lists(source=source, limit=limit)


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
    from pka.ingestion.pending_metadata import invalidate_source_probes

    # Source/archive state just changed (job start, finish, or purge); drop the
    # cached source-probe counts so the next status/progress poll recomputes.
    invalidate_source_probes(src)
    seed_progress_from_db(get_engine(), src)


def _assign_new_documents(src: str) -> None:
    """File freshly ingested documents into the active cluster run.

    Deliberately calls ``assign_new_docs`` rather than
    ``run_incremental_clustering``: the latter starts a *full* clustering run
    when no run is accepted, and an ingest finishing must never kick off a
    minutes-long re-cluster on its own. Best-effort — clustering is a view over
    the archive, so a failure here must not make a completed sync look failed.
    """
    from pka.clustering.lifecycle import assign_new_docs, get_active_run_id

    try:
        if get_active_run_id() is None:
            log.debug("No accepted cluster run — nothing to assign after %s ingest", src)
            return
        assigned = assign_new_docs().get("assigned", 0)
        if assigned:
            log.info("Assigned %d newly ingested doc(s) to the active cluster run", assigned)
    except Exception:
        log.exception("Assigning new documents after %s ingest failed", src)


def _run_ingestion_job(
    src: str,
    *,
    begin_job: sp.JobKind,
    error_label: str,
    run,
    pre_begin=None,
    assign_after: bool = False,
) -> None:
    """Shared metadata/ingest/full job skeleton: init, begin, run handler, finish."""
    from pka.db.queries import get_engine, init_db
    from pka.enrichment_runs import close_all

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, begin_job, phase="loading")
    if pre_begin is not None:
        pre_begin(src)
    try:
        stats = run()
        _finish_job(src, stats)
    except Exception as exc:
        log.exception("%s failed for %s", error_label, src)
        sp.finish(src, error=str(exc))
        seed_progress_from_db(get_engine(), src)
        # The enrichment run this job opened (if any) died with it — close it
        # as failed rather than leaving a `running` row for the reaper.
        close_all(status="failed")
        return
    finally:
        close_all()
    # Only after a clean finish: a cancelled or paused job leaves the archive
    # mid-update, and its new documents can wait for the next complete run.
    if assign_after and not _extract_stopped(stats):
        _assign_new_documents(src)


def _ingest_pre_begin(src: str) -> None:
    from pka.ingestion.pending_metadata import source_corpus_size

    if phase_spec(src).plans_own_phases:
        return  # its ingest sets the totals once it knows the work
    sp.begin_ingest(src, source_corpus_size(src))


# Sources whose metadata sync understands ``backfill``. Reddit's feed is walked
# incrementally by default (it stops at the first already-saved item), so a full
# re-walk has to be asked for; every other connector reads a local database or
# folder in full every time and has no such distinction.
BACKFILL_SOURCES = frozenset({str(Source.REDDIT)})


def _backfill_kwargs(src: str, backfill: bool) -> dict:
    """Pass ``backfill`` only where a handler accepts it."""
    return {"backfill": True} if (backfill and src in BACKFILL_SOURCES) else {}


def _sync_metadata(src: str, backfill: bool = False) -> None:
    _run_ingestion_job(
        src,
        begin_job="metadata",
        error_label="Metadata sync",
        run=lambda: require_handlers(src).sync_metadata(
            progress_key=src,
            **_backfill_kwargs(src, backfill),
        ),
    )


def _sync_ingest(src: str) -> None:
    _run_ingestion_job(
        src,
        begin_job="ingest",
        error_label="Ingest",
        run=lambda: require_handlers(src).sync_ingest(progress_key=src),
        pre_begin=_ingest_pre_begin,
        assign_after=True,
    )


def _sync(src: str, backfill: bool = False) -> None:
    """Background entry point for ``POST /ingestion/sync/{source}`` (full pipeline)."""
    _run_ingestion_job(
        src,
        begin_job="metadata",
        error_label="Ingestion sync",
        run=lambda: require_handlers(src).sync_full(
            progress_key=src,
            **_backfill_kwargs(src, backfill),
        ),
        assign_after=True,
    )


_JOB_TARGETS = {
    "metadata": lambda src, backfill=False: _sync_metadata(src, backfill),
    "ingest": lambda src, backfill=False: _sync_ingest(src),
    "full": lambda src, backfill=False: _sync(src, backfill),
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
                409,
                f"Previous sync for {src} has not stopped yet; try again",
            )


def _queue_job(src: str, job: str, force: bool, backfill: bool = False) -> dict:
    if backfill and src not in BACKFILL_SOURCES:
        raise HTTPException(400, f"{src} has no backfill mode")
    if sp.is_running(src):
        if not force:
            raise HTTPException(409, f"Sync already in progress for {src}")
        _stop_running_job(src)
    if force:
        sp.reset(src)
    thread = threading.Thread(
        target=_JOB_TARGETS[job],
        args=(src, backfill),
        daemon=True,
        name=f"alexandria-sync-{src}-{job}",
    )
    with _workers_lock:
        _workers[src] = thread
    thread.start()
    return {"status": "queued", "source": src, "job": job, "backfill": backfill}


# Plain ``def`` endpoints: _queue_job may block while joining a cancelled
# worker, so FastAPI must run these in its threadpool, not on the event loop.
@router.post("/sync/{source}", status_code=202)
def sync_source(source: str, force: bool = False, backfill: bool = False):
    require_source(source)
    return _queue_job(source, "full", force, backfill)


@router.post("/sync/{source}/metadata", status_code=202)
def sync_metadata(source: str, force: bool = False, backfill: bool = False):
    require_source(source)
    return _queue_job(source, "metadata", force, backfill)


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
