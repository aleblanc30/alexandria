"""Dev-mode ingestion caps — limit corpus size per source when ALEXANDRIA_DEV=1."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TypeVar

from pka.config import settings
from pka.constants import Source

log = logging.getLogger(__name__)

T = TypeVar("T")

# Per-source dev cap → the settings attribute holding its limit.
_LIMIT_ATTR: dict[str, str] = {
    Source.FIREFOX: "dev_ingestion_limit_firefox",
    Source.ZOTERO: "dev_ingestion_limit_zotero",
    Source.CALIBRE: "dev_ingestion_limit_calibre",
    Source.IMAGE: "dev_ingestion_limit_image",
    Source.YOUTUBE: "dev_ingestion_limit_youtube",
}


def effective_ingestion_limit(source: Source | str) -> int | None:
    """Return the doc cap for *source* when dev mode is on, else ``None``."""
    if not settings.dev:
        return None
    try:
        attr = _LIMIT_ATTR[str(source)]
    except KeyError:
        raise ValueError(f"Unknown source: {source}") from None
    return getattr(settings, attr)


def take(items: Sequence[T], source: Source | str) -> list[T]:
    """Return *items*, truncated to *source*'s dev ingestion limit when active."""
    limit = effective_ingestion_limit(source)
    if limit is None or len(items) <= limit:
        return list(items)
    log.info(
        "Dev ingestion limit (%s): using %d of %d items",
        source,
        limit,
        len(items),
    )
    return list(items[:limit])
