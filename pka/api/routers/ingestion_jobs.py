"""``/ingestion`` — background jobs.

Per-source sync (full, metadata, ingest) with pause and cancel, and the
archive-wide enrich, re-chunk and vector rebuild passes.

Every job runs on a daemon thread. Per-source syncs are tracked in
``_workers`` so a forced restart can cancel and join the old one; the
archive-wide passes each hold a module-level running flag instead.
"""

import logging
import threading

from fastapi import APIRouter, HTTPException

from pka.api.ingestion_common import require_nothing_running, require_source, seed_baselines
from pka.constants import Source
from pka.ingestion import progress as sp
from pka.ingestion.progress.baselines import seed_progress_from_db
from pka.ingestion.registry import phase_spec, require_handlers

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


_enrich_lock = threading.Lock()


_enrich_running = False


@router.post("/enrich", status_code=202)
def enrich_endpoint(kind: str = "summary", source: str | None = None):
    """Re-run an enrichment pass over documents missing that artifact.

    The retrigger for ``purge summaries``: its skip gate is "has any chunk",
    which a summary purge leaves true, so re-syncing would not regenerate it.
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
        from pka.db.migrate import init_db
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


_rechunk_lock = threading.Lock()


_rechunk_running = False


@router.post("/rechunk", status_code=202)
def rechunk_endpoint(source: str | None = None, limit: int | None = None, dry_run: bool = False):
    """Re-chunk documents from their retained body text, with no re-fetch.

    What `document_texts` was retained for: applying a chunker or
    embedding-model change to documents already in the archive. It replaces body chunks and their vectors, so — like a purge —
    it refuses to start while a sync could be writing the same rows.
    """
    global _rechunk_running
    if source:
        require_source(source)

    from pka.db.migrate import init_db
    from pka.ingestion.rechunk import rechunk_documents

    if dry_run:
        init_db()
        return {
            "status": "counted",
            "source": source,
            "stats": rechunk_documents(source=source, dry_run=True, limit=limit),
        }

    require_nothing_running(source)
    with _rechunk_lock:
        if _rechunk_running:
            raise HTTPException(409, "A re-chunk pass is already in progress")
        _rechunk_running = True

    def _run() -> None:
        global _rechunk_running
        try:
            init_db()
            stats = rechunk_documents(source=source, limit=limit)
            log.info("Re-chunk pass finished: %s", stats)
        except Exception:
            log.exception("Re-chunk pass failed")
        finally:
            with _rechunk_lock:
                _rechunk_running = False

    threading.Thread(target=_run, daemon=True, name="alexandria-rechunk").start()
    return {"status": "queued", "source": source, "limit": limit}


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
        seed_baselines(src)
        return
    stopped = _extract_stopped(stats)
    if stopped:
        sp.finish(src, stopped=stopped)  # type: ignore[arg-type]
    else:
        sp.finish(src)
    seed_baselines(src)


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
    from pka.db.engine import get_engine
    from pka.db.migrate import init_db
    from pka.enrichment_runs import close_all

    init_db()
    seed_baselines(src)
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
