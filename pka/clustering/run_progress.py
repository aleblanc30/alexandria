"""Liveness and cancel coordination for background clustering runs.

Run liveness is process-local while ``cluster_runs.status`` is persistent, so the
two can disagree whenever a worker dies without running its ``finally``. This
module owns both halves: the in-memory cancel/live sets, and the reconciliation
that settles a ``running`` row left behind by a worker that no longer exists.
"""

from __future__ import annotations

import logging
import threading

import sqlalchemy as sa

from pka.db.queries import get_engine
from pka.db.schema import cluster_runs

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cancel_requested: set[int] = set()

# Runs with a worker alive in *this* process. ``create_run_placeholder`` is the
# only writer of status="running" and its sole caller brackets the worker with
# begin()/finish(), so a "running" row absent from this set has no worker left.
_live: set[int] = set()

INTERRUPTED_NOTE = "Interrupted before completion (server stopped mid-run)."


class ClusterRunCancelled(Exception):
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        super().__init__(f"Cluster run #{run_id} cancelled")


def begin(run_id: int) -> None:
    with _lock:
        _cancel_requested.discard(run_id)
        _live.add(run_id)


def is_live(run_id: int) -> bool:
    """True while a worker for ``run_id`` is running in this process."""
    with _lock:
        return run_id in _live


def request_cancel(run_id: int) -> bool:
    with _lock:
        _cancel_requested.add(run_id)
    return True


def check_cancel(run_id: int) -> bool:
    with _lock:
        return run_id in _cancel_requested


def raise_if_cancelled(run_id: int) -> None:
    if check_cancel(run_id):
        raise ClusterRunCancelled(run_id)


def finish(run_id: int) -> None:
    with _lock:
        _cancel_requested.discard(run_id)
        _live.discard(run_id)


def reconcile_interrupted_runs() -> list[int]:
    """Mark rows still flagged ``running`` as ``failed``; return the run ids.

    ``cluster_runs.status`` is persistent but the thread that clears it is not:
    ``create_run_placeholder`` writes ``running`` before the worker starts, and
    only that worker's ``finally`` resets it. A run interrupted mid-flight —
    crash, Ctrl-C, restart — therefore leaves a row claiming to run forever,
    which refuses deletion *and* blocks every subsequent run with a 409.

    Call this at API startup, where no clustering thread can be alive yet, so
    any surviving ``running`` row is stale by construction.
    """
    eng = get_engine()
    with eng.begin() as con:
        stale = [
            r[0]
            for r in con.execute(
                sa.select(cluster_runs.c.run_id).where(cluster_runs.c.status == "running")
            ).fetchall()
        ]
        if stale:
            con.execute(
                cluster_runs.update()
                .where(cluster_runs.c.status == "running")
                .values(status="failed", notes=INTERRUPTED_NOTE)
            )
    if stale:
        log.warning(
            "Cleared %d interrupted clustering run(s): %s",
            len(stale),
            ", ".join(f"#{r}" for r in stale),
        )
    return stale


def reconcile_run(run_id: int) -> bool:
    """Mark one stale ``running`` row as failed; ``True`` when it changed.

    The per-request counterpart to :func:`reconcile_interrupted_runs`, for a run
    whose worker is gone while the server stayed up. Deliberately lands on the
    same terminal state as the startup sweep: one physical situation — a run
    with no worker — should not leave two different-looking rows depending on
    whether a restart or a cancel cleared it.
    """
    with get_engine().begin() as con:
        result = con.execute(
            cluster_runs.update()
            .where((cluster_runs.c.run_id == run_id) & (cluster_runs.c.status == "running"))
            .values(status="failed", notes=INTERRUPTED_NOTE)
        )
    changed = bool(result.rowcount)
    if changed:
        log.warning("Cleared interrupted clustering run #%d", run_id)
    return changed
