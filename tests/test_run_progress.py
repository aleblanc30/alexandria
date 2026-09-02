"""Tests for cluster run cancel coordination."""

import time

import pytest
import sqlalchemy as sa

from pka.clustering.run_progress import (
    INTERRUPTED_NOTE,
    ClusterRunCancelled,
    begin,
    check_cancel,
    finish,
    is_live,
    raise_if_cancelled,
    reconcile_interrupted_runs,
    reconcile_run,
    request_cancel,
)
from pka.db.queries import get_engine, init_db
from pka.db.schema import cluster_runs


class TestClusterRunProgress:
    def test_begin_clears_cancel_flag(self):
        request_cancel(42)
        begin(42)
        assert not check_cancel(42)

    def test_raise_if_cancelled(self):
        request_cancel(7)
        with pytest.raises(ClusterRunCancelled) as exc:
            raise_if_cancelled(7)
        assert exc.value.run_id == 7

    def test_finish_clears_cancel(self):
        request_cancel(9)
        finish(9)
        assert not check_cancel(9)

    def test_liveness_tracks_worker_lifetime(self):
        assert not is_live(11)
        begin(11)
        assert is_live(11)
        finish(11)
        assert not is_live(11)


# ── Startup reconciliation ────────────────────────────────────────────────────


def _insert_run(status: str, *, accepted: bool = False) -> int:
    with get_engine().begin() as con:
        res = con.execute(
            cluster_runs.insert().values(
                timestamp=int(time.time()),
                algorithm="HDBSCAN",
                parameters="{}",
                accepted=accepted,
                status=status,
            )
        )
    return res.inserted_primary_key[0]


def _status_of(run_id: int) -> tuple[str, str | None]:
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(cluster_runs.c.status, cluster_runs.c.notes).where(
                cluster_runs.c.run_id == run_id
            )
        ).fetchone()
    return row[0], row[1]


class TestReconcileInterruptedRuns:
    """A run whose worker died leaves a stale ``running`` row that would
    otherwise block deletion and every subsequent run."""

    @pytest.fixture(autouse=True)
    def fresh_db(self):
        init_db()

    def test_stale_running_row_marked_failed(self):
        run_id = _insert_run("running")

        assert reconcile_interrupted_runs() == [run_id]

        status, notes = _status_of(run_id)
        assert status == "failed"
        assert notes == INTERRUPTED_NOTE

    def test_finished_runs_untouched(self):
        finished = _insert_run("finished", accepted=True)
        stale = _insert_run("running")

        assert reconcile_interrupted_runs() == [stale]

        status, notes = _status_of(finished)
        assert status == "finished"
        assert notes is None

    def test_noop_when_nothing_running(self):
        _insert_run("finished")
        _insert_run("cancelled")

        assert reconcile_interrupted_runs() == []

    def test_clears_the_trigger_block(self):
        """The point of the sweep: a new run is no longer refused with a 409."""
        from pka.api.routers.runs import _running_run_id

        _insert_run("running")
        assert _running_run_id(get_engine()) is not None

        reconcile_interrupted_runs()
        assert _running_run_id(get_engine()) is None


class TestReconcileRun:
    @pytest.fixture(autouse=True)
    def fresh_db(self):
        init_db()

    def test_clears_a_single_running_row(self):
        run_id = _insert_run("running")
        other = _insert_run("running")

        assert reconcile_run(run_id) is True

        assert _status_of(run_id) == ("failed", INTERRUPTED_NOTE)
        assert _status_of(other)[0] == "running"

    def test_no_op_on_a_terminal_row(self):
        """Guarded on status, so it can never rewrite a finished run."""
        run_id = _insert_run("finished")

        assert reconcile_run(run_id) is False
        assert _status_of(run_id) == ("finished", None)
