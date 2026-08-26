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
    cluster_assignments,
    documents,
    fetch_log,
    image_rejections,
    image_tags,
    images,
    meta,
    overlay_tags,
    reading_list_items,
    reddit_items,
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
        # Migration: cache generated summaries so a re-ingest never re-infers
        # (DESIGN.md §3.2)
        if "generated_summary" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN generated_summary TEXT"
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
        if "note" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN note TEXT"
            ))
        if "doc_embedding" not in cols:
            con.execute(sa.text(
                "ALTER TABLE documents ADD COLUMN doc_embedding BLOB"
            ))

        # Migration: persist enrichment provenance alongside each chunk so the
        # API can report which rung of the ladder produced it (DESIGN.md §3.2).
        # Pre-existing chunks keep NULLs — there is no Chroma backfill.
        chunk_cols = [r[1] for r in con.execute(
            sa.text("PRAGMA table_info(chunks)")
        ).fetchall()]
        for col in ("chunk_pass", "resolved_by", "source_ref", "ref_title"):
            if chunk_cols and col not in chunk_cols:
                con.execute(sa.text(f"ALTER TABLE chunks ADD COLUMN {col} TEXT"))

        # Migration: link images to their unified documents row
        img_cols = [r[1] for r in con.execute(
            sa.text("PRAGMA table_info(images)")
        ).fetchall()]
        if img_cols and "document_id" not in img_cols:
            con.execute(sa.text(
                "ALTER TABLE images ADD COLUMN document_id INTEGER "
                "REFERENCES documents(id)"
            ))
        # Migration: cache the per-type cover extraction (DESIGN.md §3.2)
        if img_cols and "books_json" not in img_cols:
            con.execute(sa.text(
                "ALTER TABLE images ADD COLUMN books_json TEXT"
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

        # Migration: dedupe overlay_tags, then enforce (document_id, tag, origin)
        # uniqueness on DBs that predate the unique index.
        con.execute(sa.text(
            "DELETE FROM overlay_tags WHERE id NOT IN ("
            " SELECT MIN(id) FROM overlay_tags GROUP BY document_id, tag, origin)"
        ))
        con.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_overlay_doc_tag_origin "
            "ON overlay_tags(document_id, tag, origin)"
        ))

        # Migration: index the two columns the progress/status counts filter on.
        # create_all() skips indexes on tables that already exist, so DBs
        # predating these declarations need them created explicitly.
        con.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_chunks_document_id ON chunks(document_id)"
        ))
        con.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source)"
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
    note: str | None = None,
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
                     item_type, note)
                VALUES
                    (:source, :sid, :title, :url, :zak, :da, :now, :fs, :item_type, :note)
            """),
            {
                "source": str(source), "sid": source_id,
                "title": title, "url": url_or_path,
                "zak": zotero_attachment_key,
                "da": date_added, "now": now, "fs": str(fetch_status),
                "item_type": item_type, "note": note,
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
    note: str | None = None,
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
                 item_type, note)
            VALUES
                (:source, :sid, :title, :url, :zak, :da, :now, :fs, :item_type, :note)
            ON CONFLICT(source, source_id) DO UPDATE SET
                title        = excluded.title,
                url_or_path  = excluded.url_or_path,
                fetch_status = excluded.fetch_status,
                item_type    = COALESCE(excluded.item_type, documents.item_type),
                zotero_attachment_key = COALESCE(
                    excluded.zotero_attachment_key, documents.zotero_attachment_key
                ),
                note         = COALESCE(excluded.note, documents.note),
                ingested_at  = COALESCE(documents.ingested_at, excluded.ingested_at)
        """)
        con.execute(stmt, {
            "source": str(source), "sid": source_id,
            "title": title, "url": url_or_path,
            "zak": zotero_attachment_key,
            "da": date_added, "now": now, "fs": str(fetch_status),
            "item_type": item_type, "note": note,
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


# Enrichment provenance columns, optional per row (see :func:`document_enrichment`).
_CHUNK_PROVENANCE_KEYS = ("chunk_pass", "resolved_by", "source_ref", "ref_title")


def insert_chunks(rows: list[dict[str, Any]]) -> None:
    """rows: dicts with keys document_id, chunk_index, text, token_count, vector_id.

    Optionally also chunk_pass, resolved_by, source_ref, ref_title (enrichment
    provenance — see :func:`document_enrichment`).
    """
    if not rows:
        return
    # ``executemany`` binds one compiled statement across the batch, so every
    # dict must carry the same keys. Without this, a batch mixing an enriched
    # row with a plain one raises "A value is required for bind parameter" —
    # which would make the "optional" above a lie for any multi-row caller.
    rows = [{**dict.fromkeys(_CHUNK_PROVENANCE_KEYS), **row} for row in rows]
    eng = get_engine()
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
    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(
                chunks.c.document_id,
                chunks.c.chunk_pass,
                chunks.c.resolved_by,
                chunks.c.source_ref,
                chunks.c.ref_title,
                chunks.c.text,
            )
            .where(
                chunks.c.document_id.in_(doc_ids)
                & chunks.c.chunk_pass.in_(ENRICHMENT_PASSES)
            )
            .order_by(chunks.c.document_id, chunks.c.chunk_index)
        ).fetchall()
    out: dict[int, list[dict]] = {}
    for doc_id, chunk_pass, resolved_by, source_ref, ref_title, text in rows:
        out.setdefault(doc_id, []).append({
            "chunk_pass":  chunk_pass,
            "resolved_by": resolved_by,
            "source_ref":  source_ref,
            "ref_title":   ref_title,
            "text":        text,
        })
    return out


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



def get_generated_summary(doc_id: int) -> str | None:
    """Cached LLM summary for a document, or ``None``.

    Cached in SQLite so a purge-and-reingest replays without paying for
    inference again (DESIGN.md §3.2).
    """
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(documents.c.generated_summary).where(documents.c.id == doc_id)
        ).fetchone()
    return (row[0] or None) if row else None


def set_generated_summary(doc_id: int, summary: str | None) -> None:
    """Persist (or clear) the cached LLM summary for a document."""
    with get_engine().begin() as con:
        con.execute(
            documents.update()
            .where(documents.c.id == doc_id)
            .values(generated_summary=summary or None)
        )

def document_titles(doc_ids: list[int]) -> dict[int, str]:
    """Batched ``documents.id`` → title (missing ids omitted, NULL title → "").

    Used by the fetched-text embed paths, which know only a document id but need
    the persisted title — a fetch handler may have overridden it — in the
    embedded text. Batched so the phase-2 loop stays a single query.
    """
    if not doc_ids:
        return {}
    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.id, documents.c.title).where(
                documents.c.id.in_(doc_ids)
            )
        ).fetchall()
    return {row[0]: row[1] or "" for row in rows}


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


def source_ingest_queue(
    source: Source | str, limit: int | None = None,
) -> list[tuple[int, str]]:
    """Pending fetch URLs for ``source`` plus fetched docs missing chunks (orphans).

    Pending rows come first; duplicates by document id are dropped (pending wins).
    """
    eng = get_engine()
    src = str(source)
    has_url = documents.c.url_or_path.isnot(None) & (documents.c.url_or_path != "")
    with eng.connect() as con:
        pending_rows = [
            (r[0], r[1])
            for r in con.execute(
                sa.select(documents.c.id, documents.c.url_or_path).where(
                    (documents.c.source == src)
                    & (documents.c.fetch_status == str(FetchStatus.PENDING))
                    & has_url
                )
            ).fetchall()
        ]
        orphan_rows = [
            (r[0], r[1])
            for r in con.execute(
                sa.select(documents.c.id, documents.c.url_or_path).where(
                    (documents.c.source == src)
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


def firefox_ingest_queue(limit: int | None = None) -> list[tuple[int, str]]:
    """Firefox fetch queue (see :func:`source_ingest_queue`)."""
    return source_ingest_queue(Source.FIREFOX, limit)


def existing_chunk_count(document_id: int) -> int:
    """Number of chunks already stored for ``document_id`` (used by two-phase ingestion)."""
    with get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count())
            .select_from(chunks)
            .where(chunks.c.document_id == document_id)
        ).scalar()
    return n or 0


def resolve_description(card_summary: str | None, chunk_text: str | None) -> str:
    """Prefer stored card summary; fall back to first-chunk snippet."""
    if card_summary and card_summary.strip():
        return truncate_summary(card_summary)
    return truncate_summary(chunk_text)


def update_card_summary(doc_id: int, summary: str | None) -> None:
    """Set or clear the card excerpt for a document."""
    with get_engine().begin() as con:
        con.execute(
            sa.update(documents)
            .where(documents.c.id == doc_id)
            .values(card_summary=summary)
        )


# ── Reddit saved items ───────────────────────────────────────────────────────

def upsert_reddit_item(
    doc_id: int,
    *,
    kind: str,
    subreddit: str | None = None,
    permalink: str | None = None,
    external_url: str | None = None,
    body: str | None = None,
) -> None:
    """Store the Reddit-specific fields for *doc_id* (insert or update).

    Called on every pass over a saved item, not only the first, so a library
    ingested before this table existed gains its rows on the next metadata run
    without a dedicated backfill.
    """
    with get_engine().begin() as con:
        existing = con.execute(
            sa.select(reddit_items.c.id)
            .where(reddit_items.c.document_id == doc_id)
        ).fetchone()
        values = dict(
            kind=kind,
            subreddit=subreddit or None,
            permalink=permalink or None,
            external_url=external_url or None,
            body=body or None,
        )
        if existing:
            con.execute(
                sa.update(reddit_items)
                .where(reddit_items.c.document_id == doc_id)
                .values(**values)
            )
        else:
            con.execute(
                sa.insert(reddit_items).values(document_id=doc_id, **values)
            )


def reddit_item(con: sa.Connection, doc_id: int) -> dict | None:
    """Reddit fields for *doc_id*, or ``None`` when the document has no row."""
    row = con.execute(
        sa.select(
            reddit_items.c.kind,
            reddit_items.c.subreddit,
            reddit_items.c.permalink,
            reddit_items.c.external_url,
            reddit_items.c.body,
        ).where(reddit_items.c.document_id == doc_id)
    ).fetchone()
    if not row:
        return None
    return {
        "kind": row[0],
        "subreddit": row[1],
        "permalink": row[2],
        "external_url": row[3],
        "body": row[4],
    }


def all_reddit_items() -> list[dict]:
    """Every persisted Reddit document joined with its ``reddit_items`` row.

    Feeds the ingest phase, which needs a body for every item still missing
    chunks: the metadata phase already persists (and refreshes, on every pass)
    kind/subreddit/permalink/external_url/body for each saved item, so this is
    the same data a second live feed poll would return, without the request.
    """
    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(
                documents.c.source_id,
                documents.c.title,
                documents.c.date_added,
                reddit_items.c.kind,
                reddit_items.c.subreddit,
                reddit_items.c.permalink,
                reddit_items.c.external_url,
                reddit_items.c.body,
            )
            .select_from(
                documents.join(reddit_items, reddit_items.c.document_id == documents.c.id)
            )
            .where(documents.c.source == str(Source.REDDIT))
        ).fetchall()
    return [
        {
            "source_id": r[0],
            "title": r[1],
            "date_added": r[2],
            "kind": r[3],
            "subreddit": r[4],
            "permalink": r[5],
            "external_url": r[6],
            "body": r[7],
        }
        for r in rows
    ]


# ── Image gate rejection cache ────────────────────────────────────────────────

def record_image_rejection(
    path: str,
    reason: str,
    text_coverage: float | None = None,
    image_type: str | None = None,
) -> None:
    """Cache an image path rejected by the admission gate (upsert by path)."""
    now = int(time.time())
    with get_engine().begin() as con:
        con.execute(sa.text("""
            INSERT INTO image_rejections
                (path, reason, text_coverage, image_type, rejected_at)
            VALUES
                (:path, :reason, :cov, :itype, :now)
            ON CONFLICT(path) DO UPDATE SET
                reason        = excluded.reason,
                text_coverage = excluded.text_coverage,
                image_type    = excluded.image_type,
                rejected_at   = excluded.rejected_at
        """), {
            "path": path, "reason": reason, "cov": text_coverage,
            "itype": image_type, "now": now,
        })


def get_rejected_paths() -> set[str]:
    """Return the set of image paths currently in the rejection cache."""
    with get_engine().connect() as con:
        rows = con.execute(sa.select(image_rejections.c.path)).fetchall()
    return {r[0] for r in rows}


def clear_image_rejections() -> int:
    """Empty the gate rejection cache; return the number of rows removed.

    The cache is consulted by the metadata pass (``register_images``) as well as
    the embed pass, so a stale entry keeps a path skipped even after its image
    rows are gone. Cleared on a full image purge and by ``images
    --reset-rejections`` when re-tuning the gate.
    """
    with get_engine().begin() as con:
        return con.execute(image_rejections.delete()).rowcount


# Child tables keyed by document_id, cleared before the parent document row.
_IMAGE_DOC_CHILD_TABLES = (
    reading_list_items,
    cluster_assignments,
    overlay_tags,
    fetch_log,
    chunks,
    source_tags,
    source_collections,
)


def delete_image_document(path: str) -> dict[str, Any]:
    """Delete the ``images`` sidecar + backing ``documents`` row for ``path``.

    Called when the admission gate rejects an image that an earlier metadata
    pass already registered, so no orphan document/image row lingers in browse.
    Returns ``{"chunk_vector_ids", "clip_vector_id"}`` so the caller can purge
    any Chroma vectors (present only if the image had been fully ingested
    before, e.g. under ``--force-reindex``). Idempotent: a no-op when ``path``
    is not registered.
    """
    empty: dict[str, Any] = {"chunk_vector_ids": [], "clip_vector_id": None}
    eng = get_engine()
    with eng.connect() as con:
        row = con.execute(
            sa.select(images.c.id, images.c.document_id, images.c.clip_vector_id)
            .where(images.c.path == path)
        ).fetchone()
    if row is None:
        return empty
    image_id, doc_id, clip_vid = row

    with eng.connect() as con:
        chunk_vids = [
            r[0]
            for r in con.execute(
                sa.select(chunks.c.vector_id)
                .where(chunks.c.document_id == doc_id)
                .where(chunks.c.vector_id.isnot(None))
            ).fetchall()
        ]

    with eng.begin() as con:
        con.execute(image_tags.delete().where(image_tags.c.image_id == image_id))
        con.execute(images.delete().where(images.c.id == image_id))
        for tbl in _IMAGE_DOC_CHILD_TABLES:
            con.execute(tbl.delete().where(tbl.c.document_id == doc_id))
        con.execute(documents.delete().where(documents.c.id == doc_id))

    return {"chunk_vector_ids": chunk_vids, "clip_vector_id": clip_vid}


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


def _doc_title_excerpts(
    con: sa.Connection, ids: list[int],
) -> dict[int, tuple[str, str]]:
    """Map doc id -> (title, excerpt), preserving DB row order for ``ids``."""
    rows = con.execute(
        sa.select(documents.c.id, documents.c.title, documents.c.card_summary)
        .where(documents.c.id.in_(ids))
    ).fetchall()
    chunk_map = _batch_first_chunk_map(con, ids)
    return {
        doc_id: (
            (title or "").strip() or "Untitled",
            resolve_description(card_summary, chunk_map.get(doc_id)),
        )
        for doc_id, title, card_summary in rows
    }


def sample_cluster_documents(
    con: sa.Connection,
    doc_ids: list[int],
    n: int = 8,
) -> list[tuple[str, str]]:
    """Return up to ``n`` (title, excerpt) pairs for cluster labelling prompts."""
    if not doc_ids:
        return []
    return list(_doc_title_excerpts(con, doc_ids[:n]).values())


def sample_cluster_documents_for_clusters(
    con: sa.Connection,
    cluster_docs: dict[int, list[int]],
    n: int = 8,
) -> dict[int, list[tuple[str, str]]]:
    """Batch sample (title, excerpt) per cluster id."""
    all_ids = {did for docs in cluster_docs.values() for did in docs}
    if not all_ids:
        return {cid: [] for cid in cluster_docs}
    by_id = _doc_title_excerpts(con, list(all_ids))
    return {
        cid: [by_id[d] for d in doc_ids if d in by_id][:n]
        for cid, doc_ids in cluster_docs.items()
    }


def _norm_filter(values: list | None) -> list[str] | None:
    """Stringify a browse filter list, or ``None`` when empty."""
    return [str(v) for v in values] if values else None


def _where_source_tag(q: sa.Select, tag: str) -> sa.Select:
    return q.where(
        sa.exists(
            sa.select(source_tags.c.id).where(
                (source_tags.c.document_id == documents.c.id)
                & (source_tags.c.tag_string == tag)
            )
        )
    )


def _where_overlay_tag(q: sa.Select, tag: str, origin=None) -> sa.Select:
    cond = (overlay_tags.c.document_id == documents.c.id) & (overlay_tags.c.tag == tag)
    if origin is not None:
        cond = cond & (overlay_tags.c.origin == origin)
    return q.where(sa.exists(sa.select(overlay_tags.c.id).where(cond)))


def _apply_document_browse_filters(
    q: sa.Select,
    *,
    source_filter: list[str] | None,
    source_tag_filter: list[str] | None,
    overlay_tag_filter: list[str] | None,
    general_tag_filter: list[str] | None = None,
    cluster_l1_tag_filter: list[str] | None = None,
    cluster_l2_tag_filter: list[str] | None = None,
    learned_tag_filter: list[str] | None = None,
    wayback_only: bool = False,
) -> sa.Select:
    if source_filter:
        q = q.where(documents.c.source.in_(source_filter))
    if wayback_only:
        q = q.where(
            (documents.c.source == str(Source.FIREFOX))
            & documents.c.archive_url.isnot(None)
        )
    for tag in source_tag_filter or []:
        q = _where_source_tag(q, tag)
    for tag in overlay_tag_filter or []:
        q = _where_overlay_tag(q, tag)
    for tag in general_tag_filter or []:
        q = _where_overlay_tag(q, tag, TagOrigin.INFERRED)
    for tag in cluster_l1_tag_filter or []:
        q = _where_overlay_tag(q, tag, TagOrigin.CLUSTER_L1)
    for tag in cluster_l2_tag_filter or []:
        q = _where_overlay_tag(q, tag, TagOrigin.CLUSTER_L2)
    for tag in learned_tag_filter or []:
        q = _where_overlay_tag(q, tag, TagOrigin.LEARNED)
    return q


def _exclude_pending_images(q: sa.Select) -> sa.Select:
    """Hide image documents that haven't finished ingestion yet.

    A registered-but-not-embedded image has an ``images`` row with
    ``indexed_at IS NULL`` (created by the metadata pass, set by the embed
    pass). Keep those out of browse until ingestion completes, so the panel
    only shows images that are fully processed. Non-image documents have no
    ``images`` row and are unaffected. The subquery correlates on
    ``documents.id``, so the outer query must select FROM ``documents``.
    """
    pending = (
        sa.select(images.c.id)
        .where(images.c.document_id == documents.c.id)
        .where(images.c.indexed_at.is_(None))
        .exists()
    )
    return q.where(~pending)


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
    learned_tags: list[str] | None = None,
    wayback_only: bool = False,
    limit: int = 48,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Paginated document browse list with card summary or first-chunk snippet as description."""
    filter_kwargs = {
        "source_filter": _norm_filter(sources),
        "source_tag_filter": _norm_filter(source_tags),
        "overlay_tag_filter": _norm_filter(overlay_tags),
        "general_tag_filter": _norm_filter(general_tags),
        "cluster_l1_tag_filter": _norm_filter(cluster_l1_tags),
        "cluster_l2_tag_filter": _norm_filter(cluster_l2_tags),
        "learned_tag_filter": _norm_filter(learned_tags),
        "wayback_only": wayback_only,
    }

    with get_engine().connect() as con:
        count_q = _exclude_pending_images(
            _apply_document_browse_filters(
                sa.select(sa.func.count()).select_from(documents),
                **filter_kwargs,
            )
        )
        total = con.execute(count_q).scalar() or 0

        page_q = _exclude_pending_images(
            _apply_document_browse_filters(
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
            )
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
    source_filter = _norm_filter(sources)
    source_tag_filter = _norm_filter(source_tag_filter)
    cluster_l1_tag_filter = _norm_filter(cluster_l1_tag_filter)
    cluster_l2_tag_filter = _norm_filter(cluster_l2_tag_filter)
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
            str(TagOrigin.LEARNED),
        }
        if not origin or origin in overlay_origins:
            rows += [
                {"tag": r[0], "origin": r[1], "count": r[2]}
                for r in con.execute(ov_q).fetchall()
                if not origin or r[1] == origin
            ]

        rows.sort(key=lambda x: -x["count"])
        return rows[:limit]
