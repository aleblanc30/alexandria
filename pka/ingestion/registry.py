"""Source → sync handler registry for ingestion jobs."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pka.constants import Source


@dataclass(frozen=True)
class SourceHandlers:
    sync_metadata: Callable[..., dict]
    sync_ingest: Callable[..., dict]
    sync_full: Callable[..., dict]


def get_source_handlers() -> dict[str, SourceHandlers]:
    from pka.ingestion.calibre_sync import (
        sync_calibre,
        sync_calibre_ingest,
        sync_calibre_metadata,
    )
    from pka.ingestion.dev_limits import effective_ingestion_limit
    from pka.ingestion.firefox_sync import (
        sync_firefox,
        sync_firefox_ingest,
        sync_firefox_metadata,
    )
    from pka.ingestion.image_sync import (
        sync_images,
        sync_images_ingest,
        sync_images_metadata,
    )
    from pka.ingestion.youtube_sync import (
        sync_youtube,
        sync_youtube_ingest,
        sync_youtube_metadata,
    )
    from pka.ingestion.zotero_sync import (
        sync_zotero,
        sync_zotero_ingest,
        sync_zotero_metadata,
    )

    return {
        Source.ZOTERO: SourceHandlers(
            sync_metadata=sync_zotero_metadata,
            sync_ingest=sync_zotero_ingest,
            sync_full=sync_zotero,
        ),
        Source.FIREFOX: SourceHandlers(
            sync_metadata=sync_firefox_metadata,
            sync_ingest=lambda *, progress_key=None, **__: sync_firefox_ingest(
                progress_key=progress_key,
                fetch_limit=effective_ingestion_limit(Source.FIREFOX),
            ),
            sync_full=lambda *, progress_key=None, **__: sync_firefox(
                progress_key=progress_key,
                fetch_limit=effective_ingestion_limit(Source.FIREFOX),
            ),
        ),
        Source.CALIBRE: SourceHandlers(
            sync_metadata=sync_calibre_metadata,
            sync_ingest=sync_calibre_ingest,
            sync_full=sync_calibre,
        ),
        Source.IMAGE: SourceHandlers(
            sync_metadata=sync_images_metadata,
            sync_ingest=sync_images_ingest,
            sync_full=sync_images,
        ),
        Source.YOUTUBE: SourceHandlers(
            sync_metadata=sync_youtube_metadata,
            sync_ingest=sync_youtube_ingest,
            sync_full=sync_youtube,
        ),
    }


def require_handlers(src: str) -> SourceHandlers:
    handlers = get_source_handlers().get(src)
    if handlers is None:
        raise ValueError(f"Unknown source: {src}")
    return handlers
