"""Firefox bookmark ingestion."""
from __future__ import annotations

import logging

from pka.card_summary import body_excerpt
from pka.classification import classify_document, sync_classification_tags
from pka.connectors.firefox import FirefoxBookmark
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_ids_with_chunks,
    document_index,
    document_titles,
    insert_document_if_new,
    insert_source_collections,
    insert_source_tags,
    update_card_summary,
)
from pka.ingestion.core import (
    attach_summary_chunk,
    fetched_embed_text,
    ingest_text_block,
)
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


def embed_fetched_text(
    doc_id: int,
    text: str,
    card_summary: str | None = None,
    *,
    title: str | None = None,
    skip_existing: bool = True,
    dry_run: bool = False,
    chunked: set[int] | None = None,
) -> dict:
    """Chunk + embed one fetched document. Used by the interleaved fetch worker.

    ``title`` defaults to a lookup of the persisted ``documents.title`` (a fetch
    handler may have overridden it before this runs); pass it explicitly — ``""``
    when the document has none — to reuse an already batched lookup.
    """
    if skip_existing:
        if chunked is not None and doc_id in chunked:
            return {"processed": False, "chunks": 0, "skipped": True, "failed": False}
        if chunked is None and doc_id in document_ids_with_chunks(Source.FIREFOX):
            return {"processed": False, "chunks": 0, "skipped": True, "failed": False}
    try:
        if title is None:
            title = document_titles([doc_id]).get(doc_id, "")
        summary = card_summary or body_excerpt(text)
        embed_text = fetched_embed_text(title, summary, text)
        result = ingest_text_block(
            doc_id,
            embed_text,
            Source.FIREFOX,
            extra_metadata={"title": title},
            fallback_text=embed_text,
            dry_run=dry_run,
        )
        if result["skipped"]:
            return {"processed": False, "chunks": 0, "skipped": True, "failed": False}
        # Generated summary as its own chunk (DESIGN.md §3.2; default off).
        # Summarise the *body*, not the composed blob — the title and card
        # summary are already their own signal.
        summary_chunks = attach_summary_chunk(
            doc_id, text, Source.FIREFOX, title=title or "", dry_run=dry_run,
        )
        if not dry_run and card_summary is None:
            update_card_summary(doc_id, summary)
        if chunked is not None:
            chunked.add(doc_id)
        return {
            "processed": True,
            "chunks": result["chunks_added"] + summary_chunks,
            "skipped": False,
            "failed": False,
        }
    except Exception:
        log.exception("Failed embedding doc_id=%d", doc_id)
        return {"processed": False, "chunks": 0, "skipped": False, "failed": True}


def ingest_fetched_texts(
    fetched_texts: dict[int, str],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 2 batch: chunk + embed a mapping of fetched texts."""
    chunked = document_ids_with_chunks(Source.FIREFOX) if skip_existing else set()
    pairs = list(fetched_texts.items())
    titles = document_titles([doc_id for doc_id, _ in pairs])

    def _should_skip(pair: tuple[int, str]) -> bool:
        doc_id, _ = pair
        return skip_existing and doc_id in chunked

    def _process(pair: tuple[int, str]) -> tuple[bool, int]:
        doc_id, text = pair
        outcome = embed_fetched_text(
            doc_id,
            text,
            title=titles.get(doc_id, ""),
            skip_existing=skip_existing,
            dry_run=dry_run,
            chunked=chunked,
        )
        if outcome["failed"]:
            raise RuntimeError(f"embed failed for doc_id={doc_id}")
        if outcome["skipped"]:
            return False, 0
        return True, outcome["chunks"]

    return run_embed_loop(
        pairs,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda pair, exc: log.exception(
            "Failed embedding doc_id=%d: %s", pair[0], exc,
        ),
    )
