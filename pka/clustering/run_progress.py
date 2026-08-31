"""In-memory cancel coordination for background clustering runs."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_cancel_requested: set[int] = set()


class ClusterRunCancelled(Exception):
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        super().__init__(f"Cluster run #{run_id} cancelled")


def begin(run_id: int) -> None:
    with _lock:
        _cancel_requested.discard(run_id)


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
