"""Shared helper to resolve the currently-accepted cluster run."""

import sqlalchemy as sa

from pka.db.schema import cluster_runs


def fetch_active_run_id(con) -> int | None:
    """Return the run_id of the latest accepted cluster run, or ``None``."""
    row = con.execute(
        sa.select(cluster_runs.c.run_id)
        .where(cluster_runs.c.accepted == True)  # noqa: E712 — SQLA expression
        .order_by(cluster_runs.c.run_id.desc())
        .limit(1)
    ).fetchone()
    return row[0] if row else None
