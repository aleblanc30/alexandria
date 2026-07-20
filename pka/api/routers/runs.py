"""``/runs`` — list, diagnostics, accept/reject, trigger new run."""
import asyncio
import json
import logging

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from pka.api.db_rows import fetchall_mappings
from pka.api.dependencies import get_engine
from pka.api.schemas.clusters import DiagnosticsOut, RunOut
from pka.db.schema import chunks, cluster_assignments, cluster_runs, clusters

log = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])


def _clustering_preflight() -> None:
    """Reject trigger requests that cannot produce a run."""
    from pka.storage.vector_store import vector_count

    n = vector_count()
    if n == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "No embeddings in the vector store. Sync and embed documents first."
            ),
        )
    if n < 5:
        raise HTTPException(
            status_code=400,
            detail=f"Need at least 5 embedded document chunks to cluster; found {n}.",
        )


def _require_run(con, run_id: int, *, require_running: bool = False) -> str:
    """Return a run's status, or raise 404 (missing) / 409 (wrong state)."""
    row = con.execute(
        sa.select(cluster_runs.c.status).where(cluster_runs.c.run_id == run_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    status = row[0] or "finished"
    if require_running and status != "running":
        raise HTTPException(409, "Run is not running")
    if not require_running and status == "running":
        raise HTTPException(409, "Run still in progress")
    return status


def _bg_thread(bg: BackgroundTasks, fn) -> None:
    """Schedule a blocking callable on a background worker thread."""
    async def _run_threaded() -> None:
        await asyncio.to_thread(fn)

    bg.add_task(_run_threaded)


def _running_run_id(engine) -> int | None:
    with engine.connect() as con:
        row = con.execute(
            sa.select(cluster_runs.c.run_id)
            .where(cluster_runs.c.status == "running")
            .order_by(cluster_runs.c.run_id.desc())
            .limit(1)
        ).fetchone()
    return row[0] if row else None


def _n_noise(con, run_id: int, *, total_chunked: int | None = None) -> int:
    """Chunked documents without a level-1 assignment in this run."""
    if total_chunked is None:
        total_chunked = con.execute(
            sa.select(sa.func.count(sa.distinct(chunks.c.document_id)))
        ).scalar() or 0
    assigned = con.execute(
        sa.select(sa.func.count(sa.distinct(cluster_assignments.c.document_id)))
        .where(
            (cluster_assignments.c.run_id == run_id)
            & (cluster_assignments.c.level == 1)
        )
    ).scalar() or 0
    return max(0, total_chunked - assigned)


def _run_out(con, row, *, total_chunked: int | None = None) -> RunOut:
    n_cl = con.execute(
        sa.select(sa.func.count()).select_from(clusters)
        .where(clusters.c.run_id == row["run_id"])
    ).scalar() or 0
    return RunOut(
        run_id=row["run_id"],
        timestamp=row["timestamp"],
        algorithm=row["algorithm"] or "HDBSCAN",
        parameters=json.loads(row["parameters"] or "{}"),
        accepted=bool(row["accepted"]),
        status=row.get("status") or "finished",
        n_clusters=n_cl,
        n_noise=_n_noise(con, row["run_id"], total_chunked=total_chunked),
        notes=row["notes"],
    )


@router.get("", response_model=list[RunOut])
async def list_runs(engine=Depends(get_engine)):
    with engine.connect() as con:
        rows = fetchall_mappings(con.execute(
            sa.select(cluster_runs).order_by(cluster_runs.c.run_id.desc())
        ))
        total_chunked = con.execute(
            sa.select(sa.func.count(sa.distinct(chunks.c.document_id)))
        ).scalar() or 0
        return [_run_out(con, r, total_chunked=total_chunked) for r in rows]


@router.get("/{run_id}/diagnostics", response_model=DiagnosticsOut)
async def run_diagnostics(run_id: int, engine=Depends(get_engine)):
    from pka.clustering.lifecycle import compute_drift, compute_merge_suggestions
    with engine.connect() as con:
        _require_run(con, run_id)
        cluster_rows = con.execute(
            sa.select(clusters.c.cluster_id,
                      sa.func.count(cluster_assignments.c.id).label("n"))
            .join(cluster_assignments,
                  cluster_assignments.c.cluster_id == clusters.c.cluster_id)
            .where(clusters.c.run_id == run_id)
            .group_by(clusters.c.cluster_id)
        ).fetchall()
        sizes = {str(r[0]): r[1] for r in cluster_rows}
        n_noise = _n_noise(con, run_id)
    return DiagnosticsOut(
        run_id=run_id,
        n_clusters=len(sizes),
        n_noise=n_noise,
        cluster_sizes=sizes,
        drift_flags=compute_drift(run_id),
        merge_suggestions=compute_merge_suggestions(run_id),
    )


@router.post("/{run_id}/accept", status_code=204)
async def accept_run(run_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        status = _require_run(con, run_id)
    if status != "finished":
        raise HTTPException(409, f"Cannot accept a {status} run")
    from pka.clustering.lifecycle import accept_run as _accept
    _accept(run_id)


@router.post("/{run_id}/reject", status_code=204)
async def reject_run(run_id: int, notes: str = "", engine=Depends(get_engine)):
    with engine.connect() as con:
        status = _require_run(con, run_id)
    if status != "finished":
        raise HTTPException(409, f"Cannot reject a {status} run")
    from pka.clustering.lifecycle import reject_run as _reject
    _reject(run_id, notes=notes)


@router.post("/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        _require_run(con, run_id, require_running=True)
    from pka.clustering.run_progress import request_cancel
    request_cancel(run_id)
    return {"status": "cancel_requested", "run_id": run_id}


@router.post("/trigger", status_code=202)
async def trigger_run(
    bg: BackgroundTasks,
    engine=Depends(get_engine),
    skip_labelling: bool = False,
    async_labelling: bool = False,
    cluster_space: str | None = None,
):
    """Kick off a new clustering run in the background."""
    _clustering_preflight()
    if _running_run_id(engine) is not None:
        raise HTTPException(409, "A clustering run is already in progress")

    from pka.clustering.engine import create_run_placeholder, run_clustering, set_run_status
    from pka.clustering.run_progress import ClusterRunCancelled, begin, finish

    run_id = create_run_placeholder()
    begin(run_id)

    def _run() -> None:
        try:
            result = run_clustering(
                run_id=run_id,
                skip_labelling=skip_labelling,
                async_labelling=async_labelling or None,
                cluster_space=cluster_space,
            )
            log.info(
                "Clustering run #%d finished (%d clusters, %d noise)",
                result.run_id, result.n_clusters, result.n_noise,
            )
        except ClusterRunCancelled:
            set_run_status(run_id, "cancelled")
            log.info("Clustering run #%d cancelled", run_id)
        except Exception as exc:
            set_run_status(run_id, "failed", notes=str(exc))
            log.exception("Clustering run #%d failed", run_id)
        finally:
            finish(run_id)

    _bg_thread(bg, _run)
    return {"status": "queued", "run_id": run_id}


@router.post("/incremental", status_code=202)
async def trigger_incremental(bg: BackgroundTasks, engine=Depends(get_engine)):
    """Assign new docs to active run, or full re-cluster when drift is flagged."""
    _clustering_preflight()
    if _running_run_id(engine) is not None:
        raise HTTPException(409, "A clustering run is already in progress")

    from pka.clustering.lifecycle import run_incremental_clustering

    _bg_thread(bg, run_incremental_clustering)
    return {"status": "queued", "mode": "incremental"}
