"""Source → sync handler registry for ingestion jobs."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pka.constants import Source


@dataclass(frozen=True)
class PhaseSpec:
    """How one source's ingest maps onto the standard progress phases.

    The defaults describe a local library: everything is already on disk, so the
    ingest pins the corpus up front and reports embedding as its own phase.
    """

    plans_own_phases: bool = False   # ingest sets phase totals as it discovers work
    tracks_embedding: bool = True    # False when fetch and embed run interleaved


# Firefox fetches each bookmark over the network and embeds it in the same pass,
# so its work is only known once the fetch queue is built, and there is no
# separate embedding progress to report.
PHASE_SPECS: dict[str, PhaseSpec] = {
    Source.FIREFOX: PhaseSpec(plans_own_phases=True, tracks_embedding=False),
}

_DEFAULT_PHASE_SPEC = PhaseSpec()


def phase_spec(source: str) -> PhaseSpec:
    return PHASE_SPECS.get(source, _DEFAULT_PHASE_SPEC)


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
    from pka.ingestion.reddit_sync import (
        sync_reddit,
        sync_reddit_ingest,
        sync_reddit_metadata,
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
        Source.REDDIT: SourceHandlers(
            sync_metadata=sync_reddit_metadata,
            sync_ingest=sync_reddit_ingest,
            sync_full=sync_reddit,
        ),
    }


def require_handlers(src: str) -> SourceHandlers:
    handlers = get_source_handlers().get(src)
    if handlers is None:
        raise ValueError(f"Unknown source: {src}")
    return handlers
