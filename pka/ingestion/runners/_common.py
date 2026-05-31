"""Shared helpers for per-source ingestion runners."""
from __future__ import annotations


def progress_tick(key: str | None, *, failed: bool = False) -> None:
    if key:
        from pka.ingestion.sync_progress import advance
        advance(key, failed=failed)


def stop_requested(key: str | None) -> str | None:
    if not key:
        return None
    from pka.ingestion.sync_helpers import should_stop
    return should_stop(key)
