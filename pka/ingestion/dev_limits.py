"""Dev-mode ingestion caps — limit corpus size per source when ALEXANDRIA_DEV=1."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TypeVar

from pka.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T")


def effective_ingestion_limit() -> int | None:
    """Return the per-source doc cap when dev mode is on, else ``None``."""
    if settings.dev:
        return settings.dev_ingestion_limit
    return None


def take(items: Sequence[T]) -> list[T]:
    """Return *items*, truncated to the dev ingestion limit when active."""
    limit = effective_ingestion_limit()
    if limit is None or len(items) <= limit:
        return list(items)
    log.info(
        "Dev ingestion limit: using %d of %d items",
        limit,
        len(items),
    )
    return list(items[:limit])
