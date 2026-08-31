"""Tests for cluster run cancel coordination."""

import pytest

from pka.clustering.run_progress import (
    ClusterRunCancelled,
    begin,
    check_cancel,
    finish,
    raise_if_cancelled,
    request_cancel,
)


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
