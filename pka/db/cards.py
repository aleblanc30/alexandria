"""Card text for a document: its title and a short description.

The description is the stored ``card_summary`` when there is one, else a
snippet of the document's first chunk.
"""

import sqlalchemy as sa

from pka.card_summary import truncate_summary
from pka.db.schema import chunks, documents


def resolve_description(card_summary: str | None, chunk_text: str | None) -> str:
    """Prefer stored card summary; fall back to first-chunk snippet.

    Whatever was stored is shown, junk included. A card that quietly goes blank
    for a page whose scrape returned a consent wall hides the one thing worth
    knowing — that the URL needs a handler — while the meaningless text stays
    chunked and embedded regardless. ``ingestion/content_gate.py`` keeps such a
    page out of the archive in the first place.
    """
    if card_summary and card_summary.strip():
        return truncate_summary(card_summary)
    return truncate_summary(chunk_text)


def first_chunk_map(con: sa.Connection, doc_ids: list[int]) -> dict[int, str]:
    """Map document id → text of its lowest-index chunk, in one query."""
    if not doc_ids:
        return {}
    min_idx = (
        sa.select(
            chunks.c.document_id,
            sa.func.min(chunks.c.chunk_index).label("min_idx"),
        )
        .where(chunks.c.document_id.in_(doc_ids))
        .group_by(chunks.c.document_id)
        .subquery()
    )
    chunk_rows = con.execute(
        sa.select(chunks.c.document_id, chunks.c.text).select_from(
            chunks.join(
                min_idx,
                (chunks.c.document_id == min_idx.c.document_id)
                & (chunks.c.chunk_index == min_idx.c.min_idx),
            )
        )
    ).fetchall()
    return {r[0]: r[1] for r in chunk_rows}


def document_description(con: sa.Connection, doc_id: int) -> str:
    """Card description for a single document (summary or first chunk)."""
    row = con.execute(
        sa.select(documents.c.card_summary).where(documents.c.id == doc_id)
    ).fetchone()
    card_summary = row[0] if row else None
    chunk_map = first_chunk_map(con, [doc_id])
    return resolve_description(card_summary, chunk_map.get(doc_id))


def doc_title_excerpts(
    con: sa.Connection,
    ids: list[int],
) -> dict[int, tuple[str, str]]:
    """Map doc id -> (title, excerpt), preserving DB row order for ``ids``."""
    rows = con.execute(
        sa.select(documents.c.id, documents.c.title, documents.c.card_summary).where(
            documents.c.id.in_(ids)
        )
    ).fetchall()
    chunk_map = first_chunk_map(con, ids)
    return {
        doc_id: (
            (title or "").strip() or "Untitled",
            resolve_description(card_summary, chunk_map.get(doc_id)),
        )
        for doc_id, title, card_summary in rows
    }
