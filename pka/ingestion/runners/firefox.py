"""Firefox bookmark ingestion."""
from __future__ import annotations

import logging

from pka.classification import classify_document, sync_classification_tags
from pka.connectors.firefox import FirefoxBookmark
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_ids_with_chunks,
    document_index,
    insert_document_if_new,
    insert_source_collections,
    insert_source_tags,
)
from pka.ingestion.core import ingest_text_block
from pka.ingestion.fetcher import bookmark_url_unfetchable_reason
from pka.ingestion.loops import MetadataOutcome, run_embed_loop, run_metadata_loop

log = logging.getLogger(__name__)


def _sync_firefox_classification(doc_id: int, bm: FirefoxBookmark) -> None:
    tags = classify_document(Source.FIREFOX, url_or_path=bm.url)
    sync_classification_tags(doc_id, tags)


def ingest_firefox_bookmarks(
    bookmarks: list[FirefoxBookmark],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 1: persist new bookmark metadata. Fetching happens out-of-band."""
    known = document_index(Source.FIREFOX) if skip_existing else {}

    def _persist(bm: FirefoxBookmark) -> MetadataOutcome:
        if dry_run:
            return "dry_run"
        fetch_status = (
            FetchStatus.UNFETCHABLE
            if bookmark_url_unfetchable_reason(bm.url)
            else FetchStatus.PENDING
        )
        doc_id = insert_document_if_new(
            source       = Source.FIREFOX,
            source_id    = bm.source_id,
            title        = bm.title,
            url_or_path  = bm.url,
            date_added   = bm.date_added,
            fetch_status = fetch_status,
        )
        if doc_id is None:
            return "skipped"
        insert_source_tags(doc_id, bm.tags, source=Source.FIREFOX)
        if bm.folder_path:
            insert_source_collections(doc_id, [bm.folder_path], source=Source.FIREFOX)
        _sync_firefox_classification(doc_id, bm)
        known[bm.source_id] = doc_id
        return "processed"

    return run_metadata_loop(
        bookmarks,
        known=known,
        get_source_id=lambda bm: bm.source_id,
        persist=_persist,
        progress_key=progress_key,
        skip_when_in_known=skip_existing,
    )


def ingest_fetched_texts(
    fetched_texts: dict[int, str],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 2: chunk + embed text returned by :mod:`pka.ingestion.fetcher`."""
    chunked = document_ids_with_chunks(Source.FIREFOX) if skip_existing else set()
    pairs = list(fetched_texts.items())

    def _should_skip(pair: tuple[int, str]) -> bool:
        doc_id, _ = pair
        return skip_existing and doc_id in chunked

    def _process(pair: tuple[int, str]) -> tuple[bool, int]:
        doc_id, text = pair
        result = ingest_text_block(doc_id, text, Source.FIREFOX, dry_run=dry_run)
        if result["skipped"]:
            return False, 0
        chunked.add(doc_id)
        return True, result["chunks_added"]

    return run_embed_loop(
        pairs,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda pair, exc: log.exception(
            "Failed embedding doc_id=%d: %s", pair[0], exc,
        ),
    )
