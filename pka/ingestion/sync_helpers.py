"""Shared helpers for cooperative sync stop checks."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pka.ingestion.sync_progress import StopReason


def should_stop(progress_key: str | None) -> StopReason | None:
    if not progress_key:
        return None
    from pka.ingestion import sync_progress as sp
    return sp.check_stop(progress_key)
