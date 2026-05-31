"""
Reusable insert / select helpers for ``archive.db``.

Engine setup, table initialisation, document/tag/collection/chunk upserts.
Higher-level orchestration lives in :mod:`pka.ingestion.runners` and :mod:`pka.ingestion.core`.
"""
import time
from typing import Any

import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import FetchStatus, Source
from pka.db.schema import (
    chunks,
    documents,
    meta,
    overlay_tags,
    source_collections,
    source_tags,
)

_engine: sa.Engine | None = None


def get_engine() -> sa.Engine:
    global _engine
    if _engine is None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = sa.create_engine(
            f"sqlite:///{cfg.archive_db}",
            connect_args={"check_same_thread": False},
        )
        with _engine.connect() as con:
            con.execute(sa.text("PRAGMA journal_mode=WAL"))
            con.execute(sa.text("PRAGMA synchronous=NORMAL"))
            con.commit()
    return _engine


def reset_engine() -> None:
    """Drop the cached engine — used by the test suite when ``data_dir`` changes."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> None:
    """Create tables and run in-place migrations for backwards compatibility."""
    eng = get_engine()
    meta.create_all(eng)

    with eng.begin() as con:
        # Migration: add ingested_at to existing DBs that predate the column
        cols = [r[1] for r in con.execute(
            sa.text("PRAGMA table_info(documents)")
        ).fetchall()]
        if "ingested_at" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN ingested_at INTEGER"
            ))
            con.execute(sa.text(
                "UPDATE documents SET ingested_at = date_added "
                "WHERE ingested_at IS NULL"
            ))

        # Migration: add umap_points to cluster_runs
        cr_cols = [r[1] for r in con.execute(
            sa.text("PRAGMA table_info(cluster_runs)")
        ).fetchall()]
        if cr_cols and "umap_points" not in cr_cols:
            con.execute(sa.text(
                "ALTER TABLE cluster_runs ADD COLUMN umap_points TEXT"
            ))
        if cr_cols and "status" not in cr_cols:
            con.execute(sa.text(
                "ALTER TABLE cluster_runs ADD COLUMN status TEXT DEFAULT 'finished'"
            ))
            con.execute(sa.text(
                "UPDATE cluster_runs SET status = 'finished' WHERE status IS NULL"
            ))


# ── Documents ────────────────────────────────────────────────────────────────

def insert_document_if_new(
    source: Source | str,
    source_id: str,
    title: str,
    url_or_path: str | None,
    date_added: int | None,
    fetch_status: FetchStatus | str = FetchStatus.PENDING,
) -> int | None:
    """Insert a document when ``(source, source_id)`` is not already archived."""
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        existing = con.execute(
            sa.select(documents.c.id).where(
                (documents.c.source == str(source)) &
                (documents.c.source_id == source_id)
            )
        ).fetchone()
        if existing:
            return None
        con.execute(
            sa.text("""
                INSERT INTO documents
                    (source, source_id, title, url_or_path,
                     date_added, ingested_at, fetch_status)
                VALUES
                    (:source, :sid, :title, :url, :da, :now, :fs)
            """),
            {
                "source": str(source), "sid": source_id,
                "title": title, "url": url_or_path,
                "da": date_added, "now": now, "fs": str(fetch_status),
            },
        )
        row = con.execute(
            sa.select(documents.c.id).where(
                (documents.c.source == str(source)) &
                (documents.c.source_id == source_id)
            )
        ).fetchone()
    return row[0]


def upsert_document(
    source: Source | str,
    source_id: str,
    title: str,
    url_or_path: str | None,
    date_added: int | None,
    fetch_status: FetchStatus | str = FetchStatus.PENDING,
) -> int:
    """Insert a document or update its mutable fields. Returns the document id.

    ``ingested_at`` is set on first insert only — ``COALESCE`` preserves the
    original value on subsequent upserts.
    """
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        stmt = sa.text("""
            INSERT INTO documents
                (source, source_id, title, url_or_path,
                 date_added, ingested_at, fetch_status)
            VALUES
                (:source, :sid, :title, :url, :da, :now, :fs)
            ON CONFLICT(source, source_id) DO UPDATE SET
                title        = excluded.title,
                url_or_path  = excluded.url_or_path,
                fetch_status = excluded.fetch_status,
                ingested_at  = COALESCE(documents.ingested_at, excluded.ingested_at)
        """)
        con.execute(stmt, {
            "source": str(source), "sid": source_id,
            "title": title, "url": url_or_path,
            "da": date_added, "now": now, "fs": str(fetch_status),
        })
        row = con.execute(
            sa.select(documents.c.id).where(
                (documents.c.source == str(source)) &
                (documents.c.source_id == source_id)
            )
        ).fetchone()
    return row[0]


def insert_source_tags(document_id: int, tags: list[str], source: Source | str) -> None:
    if not tags:
        return
    eng = get_engine()
    with eng.begin() as con:
        # Replace existing tags for this (document, source) pair
        con.execute(
            source_tags.delete().where(
                (source_tags.c.document_id == document_id) &
                (source_tags.c.source == str(source))
            )
        )
        con.execute(source_tags.insert(), [
            {"document_id": document_id, "tag_string": t, "source": str(source)}
            for t in tags
        ])


def insert_source_collections(
    document_id: int,
    cols: list[str],
    source: Source | str,
) -> None:
    if not cols:
        return
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            source_collections.delete().where(
                (source_collections.c.document_id == document_id) &
                (source_collections.c.source == str(source))
            )
        )
        con.execute(source_collections.insert(), [
            {"document_id": document_id, "collection": c, "source": str(source)}
            for c in cols
        ])


def insert_chunks(rows: list[dict[str, Any]]) -> None:
    """rows: dicts with keys document_id, chunk_index, text, token_count, vector_id."""
    if not rows:
        return
    eng = get_engine()
    with eng.begin() as con:
        con.execute(chunks.insert(), rows)


def document_has_chunks(document_id: int) -> bool:
    with get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count()).where(chunks.c.document_id == document_id)
        ).scalar()
    return (n or 0) > 0


def document_index(source: Source | str) -> dict[str, int]:
    """Map ``source_id`` → ``documents.id`` for one connector."""
    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.source_id, documents.c.id).where(
                documents.c.source == str(source)
            )
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def source_ids_with_chunks(source: Source | str) -> set[str]:
    """Return source ids whose documents already have at least one chunk."""
    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.source_id)
            .select_from(
                documents.join(chunks, chunks.c.document_id == documents.c.id)
            )
            .where(documents.c.source == str(source))
            .distinct()
        ).fetchall()
    return {row[0] for row in rows}


def document_ids_with_chunks(source: Source | str | None = None) -> set[int]:
    """Return document ids that already have at least one chunk."""
    with get_engine().connect() as con:
        q = sa.select(chunks.c.document_id).distinct()
        if source is not None:
            q = q.select_from(
                chunks.join(documents, chunks.c.document_id == documents.c.id)
            ).where(documents.c.source == str(source))
        rows = con.execute(q).fetchall()
    return {row[0] for row in rows}


def existing_chunk_count(document_id: int) -> int:
    """Number of chunks already stored for ``document_id`` (used by two-phase ingestion)."""
    with get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count())
            .select_from(chunks)
            .where(chunks.c.document_id == document_id)
        ).scalar()
    return n or 0


_SNIPPET_MAX = 160


def _truncate_snippet(text: str | None, max_len: int = _SNIPPET_MAX) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[:max_len].rstrip() + "…"


def _apply_document_browse_filters(
    q: sa.Select,
    *,
    source_filter: list[str] | None,
    source_tag_filter: list[str] | None,
    overlay_tag_filter: list[str] | None,
) -> sa.Select:
    if source_filter:
        q = q.where(documents.c.source.in_(source_filter))
    if source_tag_filter:
        for tag in source_tag_filter:
            q = q.where(
                sa.exists(
                    sa.select(source_tags.c.id).where(
                        (source_tags.c.document_id == documents.c.id)
                        & (source_tags.c.tag_string == tag)
                    )
                )
            )
    if overlay_tag_filter:
        for tag in overlay_tag_filter:
            q = q.where(
                sa.exists(
                    sa.select(overlay_tags.c.id).where(
                        (overlay_tags.c.document_id == documents.c.id)
                        & (overlay_tags.c.tag == tag)
                    )
                )
            )
    return q


def list_documents(
    sources: list[str] | None = None,
    source_tags: list[str] | None = None,
    overlay_tags: list[str] | None = None,
    limit: int = 48,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Paginated document browse list with first-chunk snippet as description."""
    source_filter = [str(s) for s in sources] if sources else None
    source_tag_filter = [str(t) for t in source_tags] if source_tags else None
    overlay_tag_filter = [str(t) for t in overlay_tags] if overlay_tags else None
    filter_kwargs = {
        "source_filter": source_filter,
        "source_tag_filter": source_tag_filter,
        "overlay_tag_filter": overlay_tag_filter,
    }

    with get_engine().connect() as con:
        count_q = _apply_document_browse_filters(
            sa.select(sa.func.count()).select_from(documents),
            **filter_kwargs,
        )
        total = con.execute(count_q).scalar() or 0

        page_q = _apply_document_browse_filters(
            sa.select(documents.c.id, documents.c.source, documents.c.title),
            **filter_kwargs,
        ).order_by(
            documents.c.date_added.is_(None),
            documents.c.date_added.desc(),
            documents.c.id.desc(),
        ).limit(limit).offset(offset)
        rows = con.execute(page_q).fetchall()

        doc_ids = [r[0] for r in rows]
        snippet_map: dict[int, str] = {}
        if doc_ids:
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
                sa.select(chunks.c.document_id, chunks.c.text)
                .select_from(
                    chunks.join(
                        min_idx,
                        (chunks.c.document_id == min_idx.c.document_id)
                        & (chunks.c.chunk_index == min_idx.c.min_idx),
                    )
                )
            ).fetchall()
            snippet_map = {r[0]: r[1] for r in chunk_rows}

    items = [
        {
            "id": doc_id,
            "source": source,
            "title": title or "",
            "description": _truncate_snippet(snippet_map.get(doc_id)),
        }
        for doc_id, source, title in rows
    ]
    return total, items
