"""Single chunk → embed → persist path for text-bearing documents."""
from __future__ import annotations

import logging
import uuid

from pka.config import settings as cfg
from pka.constants import Source
from pka.db.queries import insert_chunks
from pka.ingestion.chunker import sentence_window_chunks
from pka.storage.vector_store import upsert_chunks

log = logging.getLogger(__name__)


def fetched_embed_text(title: str | None, card_summary: str | None, text: str) -> str:
    """Compose what gets embedded for a fetched page: title, card summary, body.

    Fetched body text often never names its own subject, so the title and the
    card summary are folded in (DESIGN.md §3.2) to make the document reachable
    by semantic search rather than only by the ``title`` fulltext branch.
    """
    parts = [(title or "").strip(), (card_summary or "").strip(), (text or "").strip()]
    return "\n\n".join(p for p in parts if p)


def ingest_text_block(
    doc_id: int,
    text: str,
    source: Source,
    extra_metadata: dict | None = None,
    chunk_offset: int = 0,
    dry_run: bool = False,
    min_chars: int | None = None,
    fallback_text: str | None = None,
) -> dict:
    """Chunk, embed, and persist a single block of text for a document.

    When ``text`` yields no chunks (empty, or too short to survive the
    ``min_chars`` filter) and ``fallback_text`` is given, the fallback is
    embedded as a single chunk regardless of length. This keeps documents with
    little or no body text — e.g. a Calibre book with only a title — findable by
    semantic search on that fallback (typically the title).

    Returns:
        ``{"chunks_added": int, "skipped": bool}``.
    """
    chunk_texts: list[str] = []
    if text and text.strip():
        chunk_texts = sentence_window_chunks(
            text,
            window    = cfg.chunk_sentences,
            overlap   = cfg.chunk_overlap,
            min_chars = min_chars if min_chars is not None else cfg.min_chunk_chars,
        )
    if not chunk_texts and fallback_text and fallback_text.strip():
        chunk_texts = [fallback_text.strip()]
    if not chunk_texts:
        return {"chunks_added": 0, "skipped": True}

    if dry_run:
        return {"chunks_added": len(chunk_texts), "skipped": False}

    vector_ids = [str(uuid.uuid4()) for _ in chunk_texts]

    base_meta = {
        "document_id": doc_id,
        "source": str(source),
        **(extra_metadata or {}),
    }

    upsert_chunks(
        ids       = vector_ids,
        texts     = chunk_texts,
        metadatas = [
            {**base_meta, "chunk_index": chunk_offset + i}
            for i in range(len(chunk_texts))
        ],
    )
    # Mirror the enrichment provenance Chroma already carries into SQLite, which
    # is what the API serves documents from (DESIGN.md §3.2). Absent keys stay
    # NULL; the Chroma payload above is untouched.
    meta = extra_metadata or {}
    provenance = {
        "chunk_pass":  meta.get("pass"),
        "resolved_by": meta.get("resolved_by"),
        "source_ref":  meta.get("isbn") or meta.get("work_key"),
        "ref_title":   meta.get("book_title"),
    }
    insert_chunks([
        {
            "document_id": doc_id,
            "chunk_index": chunk_offset + i,
            "text":        t,
            "token_count": len(t.split()),
            "vector_id":   vid,
            **provenance,
        }
        for i, (t, vid) in enumerate(zip(chunk_texts, vector_ids, strict=True))
    ])
    from pka.clustering.doc_embeddings import refresh_document_embedding
    refresh_document_embedding(doc_id)
    return {"chunks_added": len(chunk_texts), "skipped": False}


# Which setting gates a generated summary, per source. Sources absent from this
# map get no summary at all (Zotero has an abstract; YouTube has nothing to
# summarise) — see the DESIGN.md §3.2 table.
_SUMMARY_FLAGS = {
    str(Source.FIREFOX): "bookmark_summary_enabled",
    str(Source.REDDIT):  "bookmark_summary_enabled",
    str(Source.CALIBRE): "book_summary_enabled",
}


def attach_summary_chunk(
    doc_id: int,
    text: str,
    source: Source,
    *,
    title: str = "",
    dry_run: bool = False,
) -> int:
    """Add a generated-summary chunk for a long document (DESIGN.md §3.2).

    A body chunk answers "which passage matches"; nothing in a long article
    answers "what is this about". This adds that as its own ``pass="summary"``
    chunk rather than folding it into the body text, so it can be found, audited,
    and purged separately.

    The gate is checked here — one place for the whole mechanism, so runners need
    no flag of their own. Which flag depends on the source (:data:`_SUMMARY_FLAGS`):
    bookmarks and posts cost roughly one call, a book is map-reduced over its full
    text, so the cheap case must not silently enable the expensive one. The summary is cached in
    ``documents.generated_summary``: a purge-and-reingest replays it without
    paying for inference twice. Returns the number of chunks added, and never
    raises — enrichment must not cost a document its ordinary ingestion.
    """
    if dry_run or not (text or "").strip():
        return 0
    if not getattr(cfg, _SUMMARY_FLAGS.get(str(source), ""), False):
        return 0

    from pka.db.queries import (
        existing_chunk_count,
        get_generated_summary,
        set_generated_summary,
    )

    try:
        summary = get_generated_summary(doc_id)
        if not summary:
            from pka.ingestion.summarize import summarize_text

            summary = summarize_text(text)
            if not summary:
                return 0
            set_generated_summary(doc_id, summary)

        result = ingest_text_block(
            doc_id,
            summary,
            source,
            extra_metadata={"title": title, "pass": "summary"},
            chunk_offset=existing_chunk_count(doc_id),
            min_chars=1,
        )
        return 0 if result["skipped"] else result["chunks_added"]
    except Exception:
        log.exception("Summary chunk failed for doc_id=%d", doc_id)
        return 0
