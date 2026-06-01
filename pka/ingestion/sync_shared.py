"""Shared helpers for per-source full-sync orchestration."""
from __future__ import annotations

from collections.abc import Callable

from pka.ingestion import sync_progress as sp

EMPTY_STATS = {"processed": 0, "skipped": 0, "failed": 0}


def run_full_sync(meta: dict, ingest_fn: Callable[[], dict]) -> dict:
    """Compose metadata + ingest stats, short-circuiting on stop/unavailable."""
    if meta.get("stopped") or meta.get("unavailable"):
        return meta
    return {**meta, **ingest_fn()}


def unavailable_metadata(key: str, baseline: int, reason: str) -> dict:
    """Standard metadata result when a source connector is unavailable."""
    sp.begin_metadata_sync(key, 0, baseline)
    sp.skip_phase(key, "metadata")
    return {"metadata": dict(EMPTY_STATS), "unavailable": reason}
