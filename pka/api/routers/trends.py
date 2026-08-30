"""``/trends`` — timelines aggregated by cluster label or source."""
import datetime
from collections import defaultdict

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query

from pka.api.active_run import fetch_active_run_id
from pka.api.dependencies import get_engine
from pka.db.schema import cluster_assignments, clusters, documents
from pka.trends.kernel import build_kernel_timeline

router = APIRouter(prefix="/trends", tags=["trends"])


@router.get("/timeline")
def timeline(engine=Depends(get_engine)):
    """Return kernel-smoothed level-1 cluster timelines for the interest chart.

    Each bookmark contributes a finite-support Gaussian-like kernel (one quarter
    wide) centered on ``date_added``. Values are sampled at month centers.
    """
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        if not run_id:
            return {"timeline": {}, "sizes": {}}

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
                (cluster_assignments.c.level == 1) &
                (clusters.c.level == 1) &
                (documents.c.date_added.is_not(None))
            )
        ).fetchall()

    timeline_data, sizes = build_kernel_timeline(rows)
    return {"timeline": timeline_data, "sizes": sizes}


@router.get("/sources")
def sources_over_time(
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
