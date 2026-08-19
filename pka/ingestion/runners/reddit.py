"""Reddit saved-post ingestion.

Phase 1 (metadata) persists every saved item as a document row. Phase 2 (ingest)
embeds the inline body of self-posts and comments — the "cheap text immediately
available" per DESIGN.md §3 — and fetches external link posts through the shared
fetcher. This mirrors the Zotero metadata/embed split; link-post fetching reuses
the Firefox machinery.
"""
from __future__ import annotations

import logging

from pka.card_summary import body_excerpt
from pka.connectors.reddit import RedditSaved
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_ids_with_chunks,
    document_index,
    document_titles,
    insert_document_if_new,
    insert_source_collections,
    source_ids_with_chunks,
    update_card_summary,
    upsert_document,
    upsert_reddit_item,
)
from pka.ingestion.core import (
    attach_summary_chunk,
    fetched_embed_text,
    ingest_text_block,
)
from pka.ingestion.fetcher import bookmark_url_unfetchable_reason
from pka.ingestion.loops import MetadataOutcome, run_embed_loop, run_metadata_loop

log = logging.getLogger(__name__)


def _fetch_status(saved: RedditSaved) -> FetchStatus:
    if saved.external_url is None:
        # Self-post or comment: content is inline; nothing to fetch.
        return FetchStatus.AVAILABLE
    if bookmark_url_unfetchable_reason(saved.external_url):
        return FetchStatus.UNFETCHABLE
    return FetchStatus.PENDING


def _document_kwargs(saved: RedditSaved) -> dict:
    return dict(
        source=Source.REDDIT,
        source_id=saved.source_id,
        title=saved.title,
        url_or_path=saved.url_or_path,
        date_added=saved.date_added,
        fetch_status=_fetch_status(saved),
        item_type=saved.kind,
    )


def _persist_reddit_fields(doc_id: int, saved: RedditSaved) -> None:
    """Mirror the Reddit-only fields into ``reddit_items`` for the detail panel.

    ``documents`` cannot carry these: ``card_summary`` is a 280-char card
    excerpt, and a link post's ``url_or_path`` is the external target rather than
    the thread the user saved.
    """
    upsert_reddit_item(
        doc_id,
        kind=saved.kind,
        subreddit=saved.subreddit,
        permalink=saved.permalink,
        external_url=saved.external_url,
        body=saved.body,
    )


# Framing passed to the summariser. A saved comment is one turn lifted out of a
# thread, not a self-contained document, and summarising it as though it were
# produces "the author argues that…" noise instead of its subject.
_MATERIAL_BY_KIND = {
    "post": "reddit_post",
    "comment": "reddit_comment",
}


def _summary_context(saved: RedditSaved) -> str | None:
    """Thread and subreddit line given to the summariser as context.

    A comment body often names none of its own subject ("this is exactly
    backwards, and the second point is worse"). The thread title is the only
    thing that says what it is backwards *about*, so it is handed over
    separately rather than concatenated into the text being summarised.
    """
    parts = []
    title = (saved.title or "").strip()
    if title:
        parts.append(f"Thread: {title}")
    if saved.subreddit:
        parts.append(f"Subreddit: r/{saved.subreddit}")
    return " · ".join(parts) or None


def ingest_reddit_metadata(
    items: list[RedditSaved],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 1: persist new saved items (documents + subreddit collections)."""
    known = document_index(Source.REDDIT)

    def _persist(saved: RedditSaved) -> MetadataOutcome:
        existing_id = known.get(saved.source_id)
        if dry_run:
            return "skipped" if existing_id is not None else "dry_run"
        doc_id = insert_document_if_new(**_document_kwargs(saved))
        if doc_id is None:
            # Already archived. Refresh the Reddit fields anyway: a library
            # ingested before ``reddit_items`` existed fills itself in on the
            # next metadata run, and an edited body stays current.
            if existing_id is not None:
                _persist_reddit_fields(existing_id, saved)
            return "skipped"
        insert_source_collections(doc_id, [saved.collection], source=Source.REDDIT)
        _persist_reddit_fields(doc_id, saved)
        known[saved.source_id] = doc_id
        return "processed"

    # Every item reaches ``_persist``, including ones already archived — that is
    # what lets the Reddit fields backfill. ``_persist`` still reports an
    # already-known item as skipped, so the counts are unchanged.
    return run_metadata_loop(
        items,
        known=known,
        get_source_id=lambda s: s.source_id,
        persist=_persist,
        progress_key=progress_key,
        skip_when_in_known=False,
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
    """Chunk + embed one fetched link-post document (phase-2 interleaved worker).

    ``title`` defaults to a lookup of the persisted ``documents.title`` (a fetch
    handler may have overridden it before this runs); pass it explicitly — ``""``
    when the document has none — to reuse an already batched lookup.
    """
    if skip_existing:
        if chunked is not None and doc_id in chunked:
            return {"processed": False, "chunks": 0, "skipped": True, "failed": False}
        if chunked is None and doc_id in document_ids_with_chunks(Source.REDDIT):
            return {"processed": False, "chunks": 0, "skipped": True, "failed": False}
    try:
        if title is None:
            title = document_titles([doc_id]).get(doc_id, "")
        summary = card_summary or body_excerpt(text)
        embed_text = fetched_embed_text(title, summary, text)
        result = ingest_text_block(
            doc_id,
            embed_text,
            Source.REDDIT,
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
            doc_id, text, Source.REDDIT, title=title or "", dry_run=dry_run,
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
        log.exception("Failed embedding reddit doc_id=%d", doc_id)
        return {"processed": False, "chunks": 0, "skipped": False, "failed": True}


def ingest_reddit_embed(
    items: list[RedditSaved],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Embed the inline body of self-posts and comments already in the database.

    Link posts (``external_url`` set) are skipped here — they are handled by the
    phase-2 fetcher.
    """
    doc_ids = document_index(Source.REDDIT) if skip_existing else {}
    embedded = source_ids_with_chunks(Source.REDDIT) if skip_existing else set()

    def _should_skip(saved: RedditSaved) -> bool:
        return saved.external_url is not None

    def _process(saved: RedditSaved) -> tuple[bool, int]:
        doc_id = doc_ids.get(saved.source_id)
        if doc_id is None:
            doc_id = upsert_document(**_document_kwargs(saved))
            doc_ids[saved.source_id] = doc_id
        if not dry_run:
            _persist_reddit_fields(doc_id, saved)
        if skip_existing and saved.source_id in embedded:
            return False, 0
        if not dry_run and saved.body:
            update_card_summary(doc_id, body_excerpt(saved.body))
        result = ingest_text_block(
            doc_id,
            saved.body or "",
            Source.REDDIT,
            extra_metadata={"title": saved.title},
            fallback_text=saved.title,
            min_chars=1,
            dry_run=dry_run,
        )
        if result["skipped"]:
            return False, 0
        # Generated summary as its own chunk (DESIGN.md §3.2; default off).
        # The inline path owes this as much as the fetched one does: a long
        # self-post or comment is exactly the case where the body chunks answer
        # "which passage matches" and nothing answers "what is this about".
        summary_chunks = attach_summary_chunk(
            doc_id,
            saved.body or "",
            Source.REDDIT,
            title=saved.title,
            material=_MATERIAL_BY_KIND.get(saved.kind),
            context=_summary_context(saved),
            dry_run=dry_run,
        )
        embedded.add(saved.source_id)
        return True, result["chunks_added"] + summary_chunks

    return run_embed_loop(
        items,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda saved, exc: log.exception(
            "Reddit embed %s failed: %s", saved.source_id, exc,
        ),
    )
