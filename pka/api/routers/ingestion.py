"""``/ingestion`` — status overview and per-source background sync triggers."""
import logging
import threading

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException

from pka.api.dependencies import get_engine
from pka.constants import ALL_SOURCES, FetchStatus, Source
from pka.db.schema import chunks, documents, fetch_log, images
from pka.ingestion import sync_progress as sp

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


def _phase_baselines(engine, src: str) -> tuple[dict[str, int], dict[str, int]]:
    """Return (totals, processed) for idle progress bars from DB counts.

    Every item traverses metadata → fetching → embedding, so all phases share
    the same corpus total. Processed counts decrease downstream.
    """
    with engine.connect() as con:
        if src == Source.IMAGE:
            total = con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar() or 0
            embedded = con.execute(
                sa.select(sa.func.count()).select_from(images).where(
                    images.c.clip_vector_id.isnot(None)
                    | images.c.text_vector_id.isnot(None)
                )
            ).scalar() or 0
            return (
                {"metadata": total, "fetching": total, "embedding": total},
                {"metadata": total, "fetching": total, "embedding": embedded},
            )

        doc_count = con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.source == src)
        ).scalar() or 0

        embedded = con.execute(
            sa.select(sa.func.count(sa.distinct(chunks.c.document_id)))
            .select_from(
                chunks.join(documents, chunks.c.document_id == documents.c.id)
            )
            .where(documents.c.source == src)
        ).scalar() or 0

        fetching_done = doc_count
        if src == Source.FIREFOX:
            fetching_done = con.execute(
                sa.select(sa.func.count()).select_from(documents).where(
                    documents.c.source == src,
                    documents.c.fetch_status.in_([
                        str(FetchStatus.FETCHED),
                        str(FetchStatus.UNFETCHABLE),
                        str(FetchStatus.SKIPPED),
                    ]),
                )
            ).scalar() or 0

        return (
            {"metadata": doc_count, "fetching": doc_count, "embedding": doc_count},
            {"metadata": doc_count, "fetching": fetching_done, "embedding": embedded},
        )


@router.get("/status")
async def ingestion_status(engine=Depends(get_engine)):
    with engine.connect() as con:
        total = con.execute(
            sa.select(sa.func.count()).select_from(documents)
        ).scalar()
        by_source = {}
        fetch_by_source: dict[str, dict[str, int]] = {}
        for src in ALL_SOURCES:
            n = con.execute(
                sa.select(sa.func.count()).select_from(documents)
                .where(documents.c.source == src)
            ).scalar()
            by_source[src] = n
            if src == Source.FIREFOX:
                rows = con.execute(
                    sa.select(documents.c.fetch_status, sa.func.count())
                    .where(documents.c.source == src)
                    .group_by(documents.c.fetch_status)
                ).fetchall()
                fetch_by_source[src] = {status: count for status, count in rows}
                embedded = con.execute(
                    sa.select(sa.func.count(sa.distinct(chunks.c.document_id)))
                    .select_from(
                        chunks.join(documents, chunks.c.document_id == documents.c.id)
                    )
                    .where(documents.c.source == src)
                ).scalar() or 0
                fetch_by_source[src]["embedded"] = embedded
        unfetchable = con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.fetch_status == str(FetchStatus.UNFETCHABLE))
        ).scalar()
        pending = con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.fetch_status == str(FetchStatus.PENDING))
        ).scalar()
    return {
        "total": total,
        "by_source": by_source,
        "fetch_by_source": fetch_by_source,
        "unfetchable": unfetchable,
        "pending": pending,
    }


@router.get("/sync/progress")
async def sync_progress(source: str | None = None, engine=Depends(get_engine)):
    """Return live progress for one or all ingestion sync jobs."""
    if source and source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source: {source}")
    targets = [source] if source else ALL_SOURCES
    for src in targets:
        if not sp.is_running(src):
            totals, processed = _phase_baselines(engine, src)
            sp.hydrate(src, totals, processed)
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
    """Load DB counts into progress state so new jobs never regress bars."""
    from pka.db.queries import get_engine

    totals, processed = _phase_baselines(get_engine(), src)
    sp.hydrate(src, totals, processed)


def _sync_metadata(src: str) -> None:
    from pka.db.queries import init_db

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, "metadata", phase="loading")
    try:
        if src == Source.ZOTERO:
            from pka.ingestion.zotero_sync import sync_zotero_metadata
            stats = sync_zotero_metadata(progress_key=src)
        elif src == Source.FIREFOX:
            from pka.ingestion.firefox_sync import sync_firefox_metadata
            stats = sync_firefox_metadata(progress_key=src)
        elif src == Source.CALIBRE:
            from pka.ingestion.calibre_sync import sync_calibre_metadata
            stats = sync_calibre_metadata(progress_key=src)
        elif src == Source.IMAGE:
            from pka.ingestion.image_sync import sync_images_metadata
            stats = sync_images_metadata(progress_key=src)
        else:
            raise ValueError(f"Unknown source: {src}")
        _finish_job(src, stats)
    except Exception as exc:
        log.exception("Metadata sync failed for %s", src)
        sp.finish(src, error=str(exc))


def _sync_ingest(src: str) -> None:
    from pka.db.queries import init_db

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, "ingest", phase="loading")
    try:
        if src == Source.ZOTERO:
            from pka.ingestion.zotero_sync import sync_zotero_ingest
            stats = sync_zotero_ingest(progress_key=src)
        elif src == Source.FIREFOX:
            from pka.ingestion.firefox_sync import sync_firefox_ingest
            stats = sync_firefox_ingest(progress_key=src, fetch_limit=None)
        elif src == Source.CALIBRE:
            from pka.ingestion.calibre_sync import sync_calibre_ingest
            stats = sync_calibre_ingest(progress_key=src)
        elif src == Source.IMAGE:
            from pka.ingestion.image_sync import sync_images_ingest
            stats = sync_images_ingest(progress_key=src)
        else:
            raise ValueError(f"Unknown source: {src}")
        _finish_job(src, stats)
    except Exception as exc:
        log.exception("Ingest failed for %s", src)
        sp.finish(src, error=str(exc))


def _sync(src: str) -> None:
    """Background entry point for ``POST /ingestion/sync/{source}`` (full pipeline)."""
    from pka.db.queries import init_db

    init_db()
    _seed_baselines(src)
    sp.begin_job(src, "metadata", phase="loading")
    try:
        if src == Source.ZOTERO:
            from pka.ingestion.zotero_sync import sync_zotero
            stats = sync_zotero(progress_key=src)
        elif src == Source.FIREFOX:
            from pka.ingestion.firefox_sync import sync_firefox
            stats = sync_firefox(progress_key=src, fetch_limit=None)
        elif src == Source.CALIBRE:
            from pka.ingestion.calibre_sync import sync_calibre
            stats = sync_calibre(progress_key=src)
        elif src == Source.IMAGE:
            from pka.ingestion.image_sync import sync_images
            stats = sync_images(progress_key=src)
        else:
            raise ValueError(f"Unknown source: {src}")
        _finish_job(src, stats)
    except Exception as exc:
        log.exception("Ingestion sync failed for %s", src)
        sp.finish(src, error=str(exc))


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
