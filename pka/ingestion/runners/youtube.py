"""YouTube saved-video ingestion (metadata + embed).

Metadata-only content: each document embeds ``title + channel + description +
tags``. There is no separate fetch phase — the Data API returns the content
alongside the metadata — so ``fetch_status`` is set to ``FETCHED`` on insert.
Transcript-based Phase-2 enrichment is deferred (see ``BACKLOG.md``).
"""
from __future__ import annotations

import logging

from pka.classification import classify_document, sync_classification_tags
from pka.connectors.youtube import (
    YouTubeVideo,
    youtube_card_summary,
    youtube_embed_text,
)
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_index,
    insert_document_if_new,
    insert_source_collections,
    insert_source_tags,
    source_ids_with_chunks,
    update_card_summary,
    upsert_document,
)
from pka.ingestion.core import ingest_text_block
from pka.ingestion.loops import MetadataOutcome, run_embed_loop, run_metadata_loop

log = logging.getLogger(__name__)


def _sync_youtube_classification(doc_id: int) -> None:
    sync_classification_tags(doc_id, classify_document(Source.YOUTUBE))


def _document_kwargs(video: YouTubeVideo) -> dict:
    return dict(
        source=Source.YOUTUBE,
        source_id=video.source_id,
        title=video.title,
        url_or_path=video.url,
        date_added=video.date_added,
        fetch_status=FetchStatus.FETCHED,
    )


def _persist_side_data(doc_id: int, video: YouTubeVideo, *, dry_run: bool) -> None:
    insert_source_tags(doc_id, video.tags, source=Source.YOUTUBE)
    insert_source_collections(doc_id, video.playlists, source=Source.YOUTUBE)
    _sync_youtube_classification(doc_id)
    if not dry_run:
        update_card_summary(doc_id, youtube_card_summary(video))


def ingest_youtube_metadata(
    videos: list[YouTubeVideo],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Persist new saved-video rows (documents, tags, playlists) without embedding."""
    known = document_index(Source.YOUTUBE)

    def _persist(video: YouTubeVideo) -> MetadataOutcome:
        if dry_run:
            return "dry_run"
        doc_id = insert_document_if_new(**_document_kwargs(video))
        if doc_id is None:
            return "skipped"
        _persist_side_data(doc_id, video, dry_run=dry_run)
        known[video.source_id] = doc_id
        return "processed"

    return run_metadata_loop(
        videos,
        known=known,
        get_source_id=lambda v: v.source_id,
        persist=_persist,
        progress_key=progress_key,
    )


def ingest_youtube_embed(
    videos: list[YouTubeVideo],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Embed title + channel + description + tags for saved videos in the DB."""
    doc_ids = document_index(Source.YOUTUBE) if skip_existing else {}
    embedded = source_ids_with_chunks(Source.YOUTUBE) if skip_existing else set()

    def _should_skip(video: YouTubeVideo) -> bool:
        return False

    def _process(video: YouTubeVideo) -> tuple[bool, int]:
        doc_id = doc_ids.get(video.source_id)
        if doc_id is None:
            doc_id = upsert_document(**_document_kwargs(video))
            doc_ids[video.source_id] = doc_id
        _persist_side_data(doc_id, video, dry_run=dry_run)
        if skip_existing and video.source_id in embedded:
            return False, 0
        result = ingest_text_block(
            doc_id,
            youtube_embed_text(video),
            Source.YOUTUBE,
            extra_metadata={"title": video.title},
            min_chars=1,
            fallback_text=video.title,
            dry_run=dry_run,
        )
        if result["skipped"]:
            return False, 0
        embedded.add(video.source_id)
        return True, result["chunks_added"]

    return run_embed_loop(
        videos,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda video, exc: log.exception(
            "YouTube embed %s failed: %s", video.source_id, exc,
        ),
    )
