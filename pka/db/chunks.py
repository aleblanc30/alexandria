"""Writes and reads on ``chunks`` rows, including enrichment provenance."""

from typing import Any

import sqlalchemy as sa

from pka.constants import Source
from pka.db import engine
from pka.db.schema import chunks, documents

# Enrichment provenance columns, optional per row (see :func:`document_enrichment`).
_CHUNK_PROVENANCE_KEYS = ("chunk_pass", "resolved_by", "source_ref", "ref_title")


# Source-file location, optional per row: PDF page range behind the chunk.
_CHUNK_LOCATION_KEYS = ("page_start", "page_end")


def insert_chunks(rows: list[dict[str, Any]]) -> None:
    """rows: dicts with keys document_id, chunk_index, text, token_count, vector_id.

    Optionally also chunk_pass, resolved_by, source_ref, ref_title (enrichment
    provenance — see :func:`document_enrichment`) and page_start, page_end (the
    pages the chunk was read from, for PDF-backed sources).
    """
    if not rows:
        return
    # ``executemany`` binds one compiled statement across the batch, so every
    # dict must carry the same keys. Without this, a batch mixing an enriched
    # row with a plain one raises "A value is required for bind parameter" —
    # which would make the "optional" above a lie for any multi-row caller.
    defaults = dict.fromkeys(_CHUNK_PROVENANCE_KEYS + _CHUNK_LOCATION_KEYS)
    rows = [{**defaults, **row} for row in rows]
    eng = engine.get_engine()
    with eng.begin() as con:
        con.execute(chunks.insert(), rows)


# Chunk passes that represent retrieval enrichment rather than an ordinary body
# pass (``metadata``/``fulltext`` are Calibre's two normal passes).
ENRICHMENT_PASSES = ("summary", "external_synopsis")


def document_enrichment(doc_ids: list[int]) -> dict[int, list[dict]]:
    """Map document id → its enrichment chunks, in one query (DESIGN.md §3.2).

    Only ``summary`` and ``external_synopsis`` chunks are returned; the ordinary
    body passes are not provenance. Documents with no enrichment are absent from
    the mapping.
    """
    if not doc_ids:
        return {}
    with engine.get_engine().connect() as con:
        rows = con.execute(
            sa.select(
                chunks.c.document_id,
                chunks.c.chunk_pass,
                chunks.c.resolved_by,
                chunks.c.source_ref,
                chunks.c.ref_title,
                chunks.c.text,
            )
            .where(chunks.c.document_id.in_(doc_ids) & chunks.c.chunk_pass.in_(ENRICHMENT_PASSES))
            .order_by(chunks.c.document_id, chunks.c.chunk_index)
        ).fetchall()
    out: dict[int, list[dict]] = {}
    for doc_id, chunk_pass, resolved_by, source_ref, ref_title, text in rows:
        out.setdefault(doc_id, []).append(
            {
                "chunk_pass": chunk_pass,
                "resolved_by": resolved_by,
                "source_ref": source_ref,
                "ref_title": ref_title,
                "text": text,
            }
        )
    return out


def document_has_chunks(document_id: int) -> bool:
    with engine.get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count()).where(chunks.c.document_id == document_id)
        ).scalar()
    return (n or 0) > 0


def source_ids_with_chunks(source: Source | str) -> set[str]:
    """Return source ids whose documents already have at least one chunk."""
    with engine.get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.source_id)
            .select_from(documents.join(chunks, chunks.c.document_id == documents.c.id))
            .where(documents.c.source == str(source))
            .distinct()
        ).fetchall()
    return {row[0] for row in rows}


def source_ids_with_chunk_pass(source: Source | str, chunk_pass: str) -> set[str]:
    """Source ids whose documents have at least one chunk from ``chunk_pass``."""
    with engine.get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.source_id)
            .select_from(documents.join(chunks, chunks.c.document_id == documents.c.id))
            .where((documents.c.source == str(source)) & (chunks.c.chunk_pass == chunk_pass))
            .distinct()
        ).fetchall()
    return {row[0] for row in rows}


def document_ids_with_chunks(source: Source | str | None = None) -> set[int]:
    """Return document ids that already have at least one chunk."""
    with engine.get_engine().connect() as con:
        q = sa.select(chunks.c.document_id).distinct()
        if source is not None:
            q = q.select_from(chunks.join(documents, chunks.c.document_id == documents.c.id)).where(
                documents.c.source == str(source)
            )
        rows = con.execute(q).fetchall()
    return {row[0] for row in rows}


def existing_chunk_count(document_id: int) -> int:
    """Number of chunks already stored for ``document_id`` (used by two-phase ingestion)."""
    with engine.get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count())
            .select_from(chunks)
            .where(chunks.c.document_id == document_id)
        ).scalar()
    return n or 0
