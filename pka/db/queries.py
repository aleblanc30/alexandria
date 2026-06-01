"""
Reusable insert / select helpers for ``archive.db``.

Engine setup, table initialisation, document/tag/collection/chunk upserts.
Higher-level orchestration lives in :mod:`pka.ingestion.runners` and :mod:`pka.ingestion.core`.
"""
import time
from typing import Any

import sqlalchemy as sa

from pka.card_summary import truncate_summary
from pka.config import settings as cfg
from pka.constants import FetchStatus, Source, TagOrigin
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
        if "zotero_attachment_key" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN zotero_attachment_key TEXT"
            ))
        if "archive_url" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN archive_url TEXT"
            ))
        if "item_type" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN item_type TEXT"
            ))
        if "card_summary" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN card_summary TEXT"
            ))
        if "doc_embedding" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN doc_embedding BLOB"
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

        # Migration: hierarchical cluster columns
        cl_cols = [r[1] for r in con.execute(
            sa.text("PRAGMA table_info(clusters)")
        ).fetchall()]
        if cl_cols and "level" not in cl_cols:
            con.execute(sa.text(
                "ALTER TABLE clusters ADD COLUMN level INTEGER NOT NULL DEFAULT 1"
            ))
        if cl_cols and "parent_cluster_id" not in cl_cols:
            con.execute(sa.text(
                "ALTER TABLE clusters ADD COLUMN parent_cluster_id INTEGER "
                "REFERENCES clusters(cluster_id)"
            ))

        ca_cols = [r[1] for r in con.execute(
            sa.text("PRAGMA table_info(cluster_assignments)")
        ).fetchall()]
        if ca_cols and "level" not in ca_cols:
            con.execute(sa.text(
                "ALTER TABLE cluster_assignments ADD COLUMN level INTEGER NOT NULL DEFAULT 1"
            ))


# ── Documents ────────────────────────────────────────────────────────────────

def insert_document_if_new(
    source: Source | str,
    source_id: str,
    title: str,
    url_or_path: str | None,
    date_added: int | None,
    fetch_status: FetchStatus | str = FetchStatus.PENDING,
    zotero_attachment_key: str | None = None,
    item_type: str | None = None,
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
                     zotero_attachment_key, date_added, ingested_at, fetch_status,
                     item_type)
                VALUES
                    (:source, :sid, :title, :url, :zak, :da, :now, :fs, :item_type)
            """),
            {
                "source": str(source), "sid": source_id,
                "title": title, "url": url_or_path,
                "zak": zotero_attachment_key,
                "da": date_added, "now": now, "fs": str(fetch_status),
                "item_type": item_type,
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
    zotero_attachment_key: str | None = None,
    item_type: str | None = None,
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
                 zotero_attachment_key, date_added, ingested_at, fetch_status,
                 item_type)
            VALUES
                (:source, :sid, :title, :url, :zak, :da, :now, :fs, :item_type)
            ON CONFLICT(source, source_id) DO UPDATE SET
                title        = excluded.title,
                url_or_path  = excluded.url_or_path,
                fetch_status = excluded.fetch_status,
                item_type    = COALESCE(excluded.item_type, documents.item_type),
                zotero_attachment_key = COALESCE(
                    excluded.zotero_attachment_key, documents.zotero_attachment_key
                ),
                ingested_at  = COALESCE(documents.ingested_at, excluded.ingested_at)
        """)
        con.execute(stmt, {
            "source": str(source), "sid": source_id,
            "title": title, "url": url_or_path,
            "zak": zotero_attachment_key,
            "da": date_added, "now": now, "fs": str(fetch_status),
            "item_type": item_type,
        })
        row = con.execute(
            sa.select(documents.c.id).where(
                (documents.c.source == str(source)) &
                (documents.c.source_id == source_id)
            )
        ).fetchone()
    return row[0]


def refresh_zotero_attachment_keys(keys_by_source_id: dict[str, str]) -> int:
    """Backfill ``zotero_attachment_key`` for archived Zotero rows after connector load."""
    if not keys_by_source_id:
        return 0
    eng = get_engine()
    updated = 0
    with eng.begin() as con:
        for source_id, attachment_key in keys_by_source_id.items():
            result = con.execute(
                sa.update(documents)
                .where(
                    (documents.c.source == Source.ZOTERO)
                    & (documents.c.source_id == source_id)
                )
                .values(zotero_attachment_key=attachment_key)
            )
            updated += result.rowcount or 0
    return updated


def update_document_item_type(source: Source | str, source_id: str, item_type: str) -> int:
    """Set ``item_type`` on an existing document. Returns rows updated."""
    eng = get_engine()
    with eng.begin() as con:
        result = con.execute(
            sa.update(documents)
            .where(
                (documents.c.source == str(source))
                & (documents.c.source_id == source_id)
            )
            .values(item_type=item_type)
        )
    return result.rowcount or 0


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


def firefox_ingest_queue(limit: int | None = None) -> list[tuple[int, str]]:
    """Pending Firefox URLs plus fetched docs missing chunks (orphan backfill).

    Pending rows come first; duplicates by document id are dropped (pending wins).
    """
    eng = get_engine()
    has_url = documents.c.url_or_path.isnot(None) & (documents.c.url_or_path != "")
    with eng.connect() as con:
        pending_rows = [
            (r[0], r[1])
            for r in con.execute(
                sa.select(documents.c.id, documents.c.url_or_path).where(
                    (documents.c.source == str(Source.FIREFOX))
                    & (documents.c.fetch_status == str(FetchStatus.PENDING))
                    & has_url
                )
            ).fetchall()
        ]
        orphan_rows = [
            (r[0], r[1])
            for r in con.execute(
                sa.select(documents.c.id, documents.c.url_or_path).where(
                    (documents.c.source == str(Source.FIREFOX))
                    & (documents.c.fetch_status == str(FetchStatus.FETCHED))
                    & has_url
                    & ~sa.exists(
                        sa.select(chunks.c.id).where(
                            chunks.c.document_id == documents.c.id
                        )
                    )
                )
            ).fetchall()
        ]

    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    for doc_id, url in pending_rows + orphan_rows:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        out.append((doc_id, url))
    if limit is not None:
        out = out[:limit]
    return out


def existing_chunk_count(document_id: int) -> int:
    """Number of chunks already stored for ``document_id`` (used by two-phase ingestion)."""
    with get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count())
            .select_from(chunks)
            .where(chunks.c.document_id == document_id)
        ).scalar()
    return n or 0


_SNIPPET_MAX = 280


def _truncate_snippet(text: str | None, max_len: int = _SNIPPET_MAX) -> str:
    return truncate_summary(text, max_len)


def resolve_description(card_summary: str | None, chunk_text: str | None) -> str:
    """Prefer stored card summary; fall back to first-chunk snippet."""
    if card_summary and card_summary.strip():
        return truncate_summary(card_summary)
    return _truncate_snippet(chunk_text)


def update_card_summary(doc_id: int, summary: str | None) -> None:
    """Set or clear the card excerpt for a document."""
    with get_engine().begin() as con:
        con.execute(
            sa.update(documents)
            .where(documents.c.id == doc_id)
            .values(card_summary=summary)
        )


def _batch_first_chunk_map(con: sa.Connection, doc_ids: list[int]) -> dict[int, str]:
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
        sa.select(chunks.c.document_id, chunks.c.text)
        .select_from(
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
    chunk_map = _batch_first_chunk_map(con, [doc_id])
    return resolve_description(card_summary, chunk_map.get(doc_id))


def first_chunk_snippet(con: sa.Connection, doc_id: int) -> str:
    """First-chunk text for a document, collapsed and truncated like browse cards."""
    chunk_map = _batch_first_chunk_map(con, [doc_id])
    return _truncate_snippet(chunk_map.get(doc_id))


def _apply_document_browse_filters(
    q: sa.Select,
    *,
    source_filter: list[str] | None,
    source_tag_filter: list[str] | None,
    overlay_tag_filter: list[str] | None,
    general_tag_filter: list[str] | None = None,
    cluster_l1_tag_filter: list[str] | None = None,
    cluster_l2_tag_filter: list[str] | None = None,
    wayback_only: bool = False,
) -> sa.Select:
    if source_filter:
        q = q.where(documents.c.source.in_(source_filter))
    if wayback_only:
        q = q.where(
            (documents.c.source == str(Source.FIREFOX))
            & documents.c.archive_url.isnot(None)
        )
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
    if general_tag_filter:
        for tag in general_tag_filter:
            q = q.where(
                sa.exists(
                    sa.select(overlay_tags.c.id).where(
                        (overlay_tags.c.document_id == documents.c.id)
                        & (overlay_tags.c.tag == tag)
                        & (overlay_tags.c.origin == TagOrigin.INFERRED)
                    )
                )
            )
    if cluster_l1_tag_filter:
        for tag in cluster_l1_tag_filter:
            q = q.where(
                sa.exists(
                    sa.select(overlay_tags.c.id).where(
                        (overlay_tags.c.document_id == documents.c.id)
                        & (overlay_tags.c.tag == tag)
                        & (overlay_tags.c.origin == TagOrigin.CLUSTER_L1)
                    )
                )
            )
    if cluster_l2_tag_filter:
        for tag in cluster_l2_tag_filter:
            q = q.where(
                sa.exists(
                    sa.select(overlay_tags.c.id).where(
                        (overlay_tags.c.document_id == documents.c.id)
                        & (overlay_tags.c.tag == tag)
                        & (overlay_tags.c.origin == TagOrigin.CLUSTER_L2)
                    )
                )
            )
    return q


def filter_document_ids(
    con: sa.Connection,
    doc_ids: list[int],
    *,
    source_filter: list[str] | None = None,
    source_tag_filter: list[str] | None = None,
    general_tag_filter: list[str] | None = None,
    cluster_l1_tag_filter: list[str] | None = None,
    cluster_l2_tag_filter: list[str] | None = None,
    wayback_only: bool = False,
) -> set[int]:
    """Return document ids from ``doc_ids`` that match browse-style filters."""
    if not doc_ids:
        return set()
    has_filters = any(
        (
            source_filter,
            source_tag_filter,
            general_tag_filter,
            cluster_l1_tag_filter,
            cluster_l2_tag_filter,
            wayback_only,
        )
    )
    if not has_filters:
        return set(doc_ids)
    q = sa.select(documents.c.id).where(documents.c.id.in_(doc_ids))
    q = _apply_document_browse_filters(
        q,
        source_filter=source_filter,
        source_tag_filter=source_tag_filter,
        overlay_tag_filter=None,
        general_tag_filter=general_tag_filter,
        cluster_l1_tag_filter=cluster_l1_tag_filter,
        cluster_l2_tag_filter=cluster_l2_tag_filter,
        wayback_only=wayback_only,
    )
    return {row[0] for row in con.execute(q).fetchall()}


def _browse_tag_maps(
    con: sa.Connection,
    doc_ids: list[int],
) -> tuple[dict[int, list[str]], dict[int, list[str]], dict[int, list[str]]]:
    """Batch-fetch source and cluster overlay tags for browse list items."""
    source_map: dict[int, list[str]] = {doc_id: [] for doc_id in doc_ids}
    l1_map: dict[int, list[str]] = {doc_id: [] for doc_id in doc_ids}
    l2_map: dict[int, list[str]] = {doc_id: [] for doc_id in doc_ids}
    if not doc_ids:
        return source_map, l1_map, l2_map

    for doc_id, tag in con.execute(
        sa.select(source_tags.c.document_id, source_tags.c.tag_string).where(
            source_tags.c.document_id.in_(doc_ids)
        )
    ):
        source_map[doc_id].append(tag)

    for doc_id, tag, origin in con.execute(
        sa.select(
            overlay_tags.c.document_id,
            overlay_tags.c.tag,
            overlay_tags.c.origin,
        ).where(
            overlay_tags.c.document_id.in_(doc_ids),
            overlay_tags.c.origin.in_(
                [TagOrigin.CLUSTER_L1, TagOrigin.CLUSTER_L2]
            ),
        )
    ):
        if origin == TagOrigin.CLUSTER_L1:
            l1_map[doc_id].append(tag)
        else:
            l2_map[doc_id].append(tag)

    return source_map, l1_map, l2_map


def list_documents(
    sources: list[str] | None = None,
    source_tags: list[str] | None = None,
    overlay_tags: list[str] | None = None,
    general_tags: list[str] | None = None,
    cluster_l1_tags: list[str] | None = None,
    cluster_l2_tags: list[str] | None = None,
    wayback_only: bool = False,
    limit: int = 48,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Paginated document browse list with card summary or first-chunk snippet as description."""
    source_filter = [str(s) for s in sources] if sources else None
    source_tag_filter = [str(t) for t in source_tags] if source_tags else None
    overlay_tag_filter = [str(t) for t in overlay_tags] if overlay_tags else None
    general_tag_filter = [str(t) for t in general_tags] if general_tags else None
    cluster_l1_tag_filter = [str(t) for t in cluster_l1_tags] if cluster_l1_tags else None
    cluster_l2_tag_filter = [str(t) for t in cluster_l2_tags] if cluster_l2_tags else None
    filter_kwargs = {
        "source_filter": source_filter,
        "source_tag_filter": source_tag_filter,
        "overlay_tag_filter": overlay_tag_filter,
        "general_tag_filter": general_tag_filter,
        "cluster_l1_tag_filter": cluster_l1_tag_filter,
        "cluster_l2_tag_filter": cluster_l2_tag_filter,
        "wayback_only": wayback_only,
    }

    with get_engine().connect() as con:
        count_q = _apply_document_browse_filters(
            sa.select(sa.func.count()).select_from(documents),
            **filter_kwargs,
        )
        total = con.execute(count_q).scalar() or 0

        page_q = _apply_document_browse_filters(
            sa.select(
                documents.c.id,
                documents.c.source,
                documents.c.source_id,
                documents.c.title,
                documents.c.url_or_path,
                documents.c.archive_url,
                documents.c.zotero_attachment_key,
                documents.c.card_summary,
            ),
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
            needs_chunk = [
                r[0] for r in rows if not (r[7] and str(r[7]).strip())
            ]
            if needs_chunk:
                snippet_map = _batch_first_chunk_map(con, needs_chunk)
        source_map, l1_map, l2_map = _browse_tag_maps(con, doc_ids)

    items = [
        {
            "id": doc_id,
            "source": source,
            "source_id": source_id,
            "title": title or "",
            "description": resolve_description(card_summary, snippet_map.get(doc_id)),
            "url_or_path": url_or_path,
            "archive_url": archive_url,
            "zotero_attachment_key": zotero_attachment_key,
            "source_tags": source_map.get(doc_id, []),
            "cluster_l1_tags": l1_map.get(doc_id, []),
            "cluster_l2_tags": l2_map.get(doc_id, []),
        }
        for doc_id, source, source_id, title, url_or_path, archive_url, zotero_attachment_key, card_summary in rows
    ]
    return total, items


def list_tags(
    origin: str | None = None,
    sources: list[str] | None = None,
    source_tag_filter: list[str] | None = None,
    cluster_l1_tag_filter: list[str] | None = None,
    cluster_l2_tag_filter: list[str] | None = None,
    wayback_only: bool = False,
    q: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List tags with counts, optionally scoped to documents matching browse filters."""
    source_filter = [str(s) for s in sources] if sources else None
    source_tag_filter = [str(t) for t in source_tag_filter] if source_tag_filter else None
    cluster_l1_tag_filter = [str(t) for t in cluster_l1_tag_filter] if cluster_l1_tag_filter else None
    cluster_l2_tag_filter = [str(t) for t in cluster_l2_tag_filter] if cluster_l2_tag_filter else None
    filter_kwargs = {
        "source_filter": source_filter,
        "source_tag_filter": source_tag_filter,
        "overlay_tag_filter": None,
        "cluster_l1_tag_filter": cluster_l1_tag_filter,
        "cluster_l2_tag_filter": cluster_l2_tag_filter,
        "wayback_only": wayback_only,
    }
    has_doc_scope = any(
        (
            source_filter,
            source_tag_filter,
            cluster_l1_tag_filter,
            cluster_l2_tag_filter,
            wayback_only,
        )
    )

    with get_engine().connect() as con:
        doc_scope = None
        if has_doc_scope:
            doc_scope = _apply_document_browse_filters(
                sa.select(documents.c.id),
                **filter_kwargs,
            )

        src_q = (
            sa.select(
                source_tags.c.tag_string.label("tag"),
                sa.literal("source").label("origin"),
                sa.func.count(source_tags.c.id).label("n"),
            )
            .select_from(source_tags)
            .group_by(source_tags.c.tag_string)
        )
        if doc_scope is not None:
            src_q = src_q.where(source_tags.c.document_id.in_(doc_scope))
        if q:
            src_q = src_q.where(source_tags.c.tag_string.ilike(f"%{q}%"))

        ov_q = (
            sa.select(
                overlay_tags.c.tag.label("tag"),
                overlay_tags.c.origin.label("origin"),
                sa.func.count(overlay_tags.c.id).label("n"),
            )
            .select_from(overlay_tags)
            .group_by(overlay_tags.c.tag, overlay_tags.c.origin)
        )
        if doc_scope is not None:
            ov_q = ov_q.where(overlay_tags.c.document_id.in_(doc_scope))
        if q:
            ov_q = ov_q.where(overlay_tags.c.tag.ilike(f"%{q}%"))

        rows: list[dict[str, Any]] = []
        if not origin or origin == "source":
            rows += [
                {"tag": r[0], "origin": r[1], "count": r[2]}
                for r in con.execute(src_q).fetchall()
            ]
        overlay_origins = {
            str(TagOrigin.INFERRED),
            str(TagOrigin.MANUAL),
            str(TagOrigin.LLM),
            str(TagOrigin.CLUSTER_L1),
            str(TagOrigin.CLUSTER_L2),
        }
        if not origin or origin in overlay_origins:
            rows += [
                {"tag": r[0], "origin": r[1], "count": r[2]}
                for r in con.execute(ov_q).fetchall()
                if not origin or r[1] == origin
            ]

        rows.sort(key=lambda x: -x["count"])
        return rows[:limit]
