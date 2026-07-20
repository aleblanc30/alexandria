"""Remove clustering run rows and related cluster data from archive.db.

Usage::

    alexandria purge-cluster-runs --all
    alexandria purge-cluster-runs 42
    alexandria purge-cluster-runs --all --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys

import sqlalchemy as sa

from pka.cli._logging import setup_logging
from pka.db.queries import get_engine
from pka.db.schema import cluster_assignments, cluster_runs, clusters

log = logging.getLogger("purge_cluster_runs")


def _run_ids(engine, *, run_id: int | None, all_runs: bool) -> list[dict]:
    with engine.connect() as con:
        q = sa.select(cluster_runs.c.run_id, cluster_runs.c.status, cluster_runs.c.accepted)
        if run_id is not None:
            q = q.where(cluster_runs.c.run_id == run_id)
        q = q.order_by(cluster_runs.c.run_id)
        return [dict(r._mapping) for r in con.execute(q).fetchall()]


def purge_cluster_run(run_id: int, *, dry_run: bool = False, force: bool = False) -> dict[str, int]:
    """Delete one run and its clusters / assignments."""
    eng = get_engine()
    rows = _run_ids(eng, run_id=run_id, all_runs=False)
    if not rows:
        raise ValueError(f"Run #{run_id} not found")

    row = rows[0]
    if row["status"] == "running":
        raise ValueError(f"Run #{run_id} is still running")
    if row["accepted"] and not force:
        raise ValueError(
            f"Run #{run_id} is accepted; pass --force to delete the active run"
        )

    counts = _count_run_rows(eng, run_id)
    if dry_run:
        return counts

    with eng.begin() as con:
        result = con.execute(
            cluster_assignments.delete().where(cluster_assignments.c.run_id == run_id)
        )
        counts["cluster_assignments"] = result.rowcount
        result = con.execute(
            clusters.delete().where(
                (clusters.c.run_id == run_id) & (clusters.c.level == 2)
            )
        )
        counts["clusters_l2"] = result.rowcount
        result = con.execute(
            clusters.delete().where(
                (clusters.c.run_id == run_id) & (clusters.c.level == 1)
            )
        )
        counts["clusters_l1"] = result.rowcount
        result = con.execute(
            cluster_runs.delete().where(cluster_runs.c.run_id == run_id)
        )
        counts["cluster_runs"] = result.rowcount

    return counts


def purge_all_cluster_runs(*, dry_run: bool = False, force: bool = False) -> dict[str, int]:
    """Delete every non-running clustering run."""
    eng = get_engine()
    rows = _run_ids(eng, run_id=None, all_runs=True)
    totals: dict[str, int] = {
        "runs": 0,
        "cluster_assignments": 0,
        "clusters_l2": 0,
        "clusters_l1": 0,
        "cluster_runs": 0,
        "skipped_running": 0,
        "skipped_accepted": 0,
    }

    for row in rows:
        run_id = row["run_id"]
        if row["status"] == "running":
            totals["skipped_running"] += 1
            continue
        if row["accepted"] and not force:
            totals["skipped_accepted"] += 1
            continue

        counts = purge_cluster_run(run_id, dry_run=dry_run, force=force)
        totals["runs"] += 1
        for key in ("cluster_assignments", "clusters_l2", "clusters_l1", "cluster_runs"):
            totals[key] += counts.get(key, 0)

    return totals


def _count_run_rows(engine, run_id: int) -> dict[str, int]:
    with engine.connect() as con:
        n_assign = con.execute(
            sa.select(sa.func.count())
            .select_from(cluster_assignments)
            .where(cluster_assignments.c.run_id == run_id)
        ).scalar() or 0
        n_l2 = con.execute(
            sa.select(sa.func.count())
            .select_from(clusters)
            .where((clusters.c.run_id == run_id) & (clusters.c.level == 2))
        ).scalar() or 0
        n_l1 = con.execute(
            sa.select(sa.func.count())
            .select_from(clusters)
            .where((clusters.c.run_id == run_id) & (clusters.c.level == 1))
        ).scalar() or 0
    return {
        "cluster_assignments": n_assign,
        "clusters_l2": n_l2,
        "clusters_l1": n_l1,
        "cluster_runs": 1,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alexandria purge-cluster-runs",
        description="Remove clustering runs from archive.db.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("run_id", nargs="?", type=int, help="Specific run ID to delete")
    group.add_argument(
        "--all",
        action="store_true",
        help="Delete every non-running run (skips accepted unless --force)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report row counts without deleting",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow deleting accepted (active) runs",
    )
    args = parser.parse_args(argv)

    setup_logging()

    try:
        if args.all:
            counts = purge_all_cluster_runs(dry_run=args.dry_run, force=args.force)
        else:
            counts = purge_cluster_run(args.run_id, dry_run=args.dry_run, force=args.force)
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)

    prefix = "Would delete" if args.dry_run else "Deleted"
    for name, count in counts.items():
        if count:
            log.info("%s %s: %d", prefix, name, count)
    return 0


if __name__ == "__main__":
    main()
