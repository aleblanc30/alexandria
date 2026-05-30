"""``/trends`` — timelines aggregated by cluster label or source."""
import datetime
from collections import defaultdict

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query

from pka.api.dependencies import get_engine
from pka.db.schema import cluster_assignments, cluster_runs, clusters, documents

router = APIRouter(prefix="/trends", tags=["trends"])


@router.get("/timeline")
async def timeline(
    granularity: str = Query("month", description="month | year"),
    engine=Depends(get_engine),
):
    """Return ``{cluster_label: {period: count}}`` for the interest timeline chart."""
    with engine.connect() as con:
        run = con.execute(
            sa.select(cluster_runs.c.run_id)
            .where(cluster_runs.c.accepted == True)  # noqa: E712
            .order_by(cluster_runs.c.run_id.desc()).limit(1)
        ).fetchone()
        if not run:
            return {}
        run_id = run[0]

        rows = con.execute(
            sa.select(
                clusters.c.label,
                documents.c.date_added,
            )
            .join(cluster_assignments,
                  cluster_assignments.c.document_id == documents.c.id)
            .join(clusters,
                  clusters.c.cluster_id == cluster_assignments.c.cluster_id)
            .where(
                (cluster_assignments.c.run_id == run_id) &
                (documents.c.date_added.is_not(None))
            )
        ).fetchall()

    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for label, ts in rows:
        if not ts:
            continue
        dt = datetime.datetime.utcfromtimestamp(ts)
        period = dt.strftime("%Y-%m") if granularity == "month" else str(dt.year)
        result[label or "Unlabelled"][period] += 1

    return {k: dict(v) for k, v in result.items()}


@router.get("/sources")
async def sources_over_time(
    granularity: str = Query("month"),
    engine=Depends(get_engine),
):
    """Return ``{source: {period: count}}`` for the sources-over-time chart."""
    with engine.connect() as con:
        rows = con.execute(
            sa.select(documents.c.source, documents.c.date_added)
            .where(documents.c.date_added.is_not(None))
        ).fetchall()
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for src, ts in rows:
        dt = datetime.datetime.utcfromtimestamp(ts)
        period = dt.strftime("%Y-%m") if granularity == "month" else str(dt.year)
        result[src][period] += 1
    return {k: dict(v) for k, v in result.items()}
