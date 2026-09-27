"""Re-chunk documents from their retained text, without going back to the source.

The pass that ``document_texts`` exists for (``planning/FULL_TEXT_RETENTION.md``
§6.2). Changing ``chunk_sentences``, ``chunk_overlap``, ``min_chunk_chars`` or
the splitter itself used to be unappliable to an archive already ingested: the
only copy of the body was ``chunks.text``, so re-cutting it meant re-fetching
every URL and re-extracting every book. With the text retained, it is a local
read.

Only **body** chunks are replaced. A summary, an external synopsis and Calibre's
pass-1 metadata chunk are not what changed when the chunker changed, and the
summary in particular costs inference to remake — ``purge.body_chunk_predicate``
is the shared definition of which chunks are body, so this pass and the
``fetched_text`` purge target cannot disagree about it.

**New chunks are written before the old ones are deleted.** An interruption then
leaves a document with duplicated body chunks, which a re-run cleans up, rather
than with none at all — the opposite order would blind search for that document
until someone noticed.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from pka.constants import Source
from pka.db.queries import get_engine
from pka.db.schema import chunks, documents
from pka.ingestion.core import fetched_embed_text, ingest_text_block
from pka.ingestion.text_store import document_text_meta, load_document_text
from pka.purge import body_chunk_predicate
from pka.storage import vector_store

log = logging.getLogger(__name__)

# SQLite binds one variable per id in an ``IN (...)`` list; one document can
# carry tens of thousands of chunks (a single Firefox fetch has produced 14584),
# so the delete of the superseded chunks batches like every other id list.
_ID_BATCH_SIZE = 5_000


def _batches(ids: list) -> list[list]:
    return [ids[i : i + _ID_BATCH_SIZE] for i in range(0, len(ids), _ID_BATCH_SIZE)]


def _candidates(con, source: str | None, limit: int | None) -> list[sa.Row]:
    """Documents whose body text is retained — the only ones this can act on."""
    from pka.db.schema import document_texts

    q = (
        sa.select(documents.c.id, documents.c.source, documents.c.title, documents.c.card_summary)
        .select_from(documents.join(document_texts, document_texts.c.document_id == documents.c.id))
        .order_by(documents.c.id)
    )
    if source is not None:
        q = q.where(documents.c.source == str(source))
    if limit is not None:
        q = q.limit(limit)
    return list(con.execute(q).fetchall())


def _body_chunk_rows(con, doc_id: int) -> list[sa.Row]:
    return list(
        con.execute(
            sa.select(chunks.c.id, chunks.c.vector_id)
            .where(chunks.c.document_id == doc_id)
            .where(body_chunk_predicate())
        ).fetchall()
    )


def _next_chunk_index(con, doc_id: int) -> int:
    """One past the highest index in use.

    Not ``existing_chunk_count``: the runners can use a count because they only
    ever append to a contiguous run, whereas here the surviving summary chunk
    keeps whatever high index it was given when the body still sat underneath
    it. Counting would hand new chunks indices that collide with it.
    """
    highest = con.execute(
        sa.select(sa.func.max(chunks.c.chunk_index)).where(chunks.c.document_id == doc_id)
    ).scalar()
    return 0 if highest is None else int(highest) + 1


def _rechunk_blocks(row: sa.Row, text: str, blocks: list[dict], offset: int) -> int:
    """Replay a paginated extraction section by section (Calibre pass 2).

    The block map is what makes this a re-chunk rather than a downgrade: without
    it the section title, section index and page range the old chunks carried
    could not be reproduced.
    """
    added = 0
    for block in blocks:
        start = block.get("offset", 0)
        section = text[start : start + block.get("length", 0)]
        if not section.strip():
            continue
        meta = {
            "title": row.title or "",
            "pass": "fulltext",
            "section_title": block.get("title", ""),
            "section_index": block.get("index", 0),
        }
        for key in ("page_start", "page_end"):
            # Chroma metadata values must be scalars, so a None is omitted
            # rather than passed — same rule as runners/calibre.py::_page_range.
            if block.get(key) is not None:
                meta[key] = block[key]
        result = ingest_text_block(
            row.id,
            section,
            Source(row.source),
            extra_metadata=meta,
            chunk_offset=offset + added,
        )
        if not result["skipped"]:
            added += result["chunks_added"]
    return added


def _rechunk_body(row: sa.Row, text: str, offset: int) -> int:
    """Replay a fetched body the way ``embed_fetched_text`` composed it.

    Title and card summary are folded back in from ``documents`` (DESIGN.md
    §3.2) rather than from the stored text, which deliberately holds the body
    alone — that is the whole reason the composite is not what gets retained.
    """
    embed_text = fetched_embed_text(row.title, row.card_summary, text)
    result = ingest_text_block(
        row.id,
        embed_text,
        Source(row.source),
        extra_metadata={"title": row.title or ""},
        fallback_text=embed_text,
        chunk_offset=offset,
    )
    return 0 if result["skipped"] else result["chunks_added"]


def rechunk_documents(
    *,
    source: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Re-cut every retained document's body with the current chunker settings.

    Returns ``{"candidates", "rechunked", "skipped", "chunks_added",
    "chunks_removed", "vectors_purged"}``. ``skipped`` counts documents whose
    stored text produced no chunks at all — an empty or unreadable row — which
    are left exactly as they were rather than stripped of the chunks they have.
    """
    eng = get_engine()
    with eng.connect() as con:
        candidates = _candidates(con, source, limit)

    stats = {
        "candidates": len(candidates),
        "rechunked": 0,
        "skipped": 0,
        "chunks_added": 0,
        "chunks_removed": 0,
        "vectors_purged": 0,
    }
    if dry_run:
        return stats

    for row in candidates:
        try:
            text = load_document_text(row.id)
            if not text or not text.strip():
                stats["skipped"] += 1
                continue
            meta = document_text_meta(row.id) or {}
            blocks = meta.get("blocks")

            with eng.connect() as con:
                superseded = _body_chunk_rows(con, row.id)
                offset = _next_chunk_index(con, row.id)

            added = (
                _rechunk_blocks(row, text, blocks, offset)
                if blocks
                else _rechunk_body(row, text, offset)
            )
            if added == 0:
                # Nothing replaced the old chunks, so nothing is dropped: a
                # document that survives this pass with its previous chunks is a
                # better outcome than one silently emptied.
                stats["skipped"] += 1
                continue

            vector_ids = [r.vector_id for r in superseded if r.vector_id]
            if vector_ids:
                stats["vectors_purged"] += vector_store.purge_vectors(vector_ids)
            # `purge_vectors` already drops the rows carrying those vector ids;
            # this deletes by id so a body chunk that never got a vector (an
            # interrupted embed) goes too, rather than surviving as a duplicate.
            chunk_ids = [r.id for r in superseded]
            with eng.begin() as con:
                for batch in _batches(chunk_ids):
                    con.execute(chunks.delete().where(chunks.c.id.in_(batch)))
            stats["chunks_removed"] += len(chunk_ids)
            stats["chunks_added"] += added
            stats["rechunked"] += 1

            # The mean pool ran per block, over a document that still held the
            # chunks just deleted. Refresh it once the picture is final.
            from pka.clustering.doc_embeddings import refresh_document_embedding

            refresh_document_embedding(row.id)
        except Exception:
            log.exception("Re-chunk failed for doc_id=%d", row.id)
            stats["skipped"] += 1

    log.info(
        "Re-chunk finished: %d/%d documents, +%d chunks, -%d chunks",
        stats["rechunked"],
        stats["candidates"],
        stats["chunks_added"],
        stats["chunks_removed"],
    )
    return stats
