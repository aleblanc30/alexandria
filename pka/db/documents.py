"""Writes and reads on ``documents`` rows.

The ingestion upsert, the per-column setters, and the phase-2 fetch queues.
"""

import time
from dataclasses import asdict, dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from pka.constants import FetchStatus, Source
from pka.db import engine
from pka.db.schema import chunks, documents


@dataclass(frozen=True, slots=True)
class DocumentWrite:
    """The columns of ``documents`` that ingestion writes.

    Not every column: ``archive_url``, ``card_summary``, ``generated_summary``
    and ``doc_embedding`` are owned by their own helpers (``update_card_summary``,
    ``set_generated_summary``, the Wayback and doc-embedding paths) and are never
    written by an ingestion upsert. ``id`` and ``ingested_at`` are set by the
    writer, not the caller.

    Field order matches the pre-refactor positional parameter order of
    ``insert_document_if_new`` / ``upsert_document``.
    """

    source: Source | str
    source_id: str
    title: str | None = None
    url_or_path: str | None = None
    date_added: int | None = None
    fetch_status: FetchStatus | str = FetchStatus.PENDING
    zotero_attachment_key: str | None = None
    item_type: str | None = None
    note: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    isbn: str | None = None
    year: int | None = None
    authors_json: str | None = None
    zotero_url: str | None = None
    zotero_path: str | None = None

    def values(self) -> dict[str, Any]:
        """Column -> value, with the two string enums stringified."""
        v = asdict(self)
        v["source"] = str(self.source)
        v["fetch_status"] = str(self.fetch_status)
        return v


# Columns an upsert overwrites outright; every other writable column COALESCEs
# the incoming value over the stored one instead. COALESCE is the right default
# for a bibliographic field: a source that does not know an item's DOI must not
# erase the DOI another source supplied. Overwrite is reserved for the columns
# whose current value is by definition whatever the source last said.
_OVERWRITE_ON_UPSERT = frozenset({"title", "url_or_path", "fetch_status"})


def _write_document(doc: DocumentWrite, *, on_conflict: str) -> int | None:
    """Shared Core writer behind ``insert_document_if_new`` / ``upsert_document``.

    ``inserted_primary_key`` is not used to recover the id: on a
    ``DO UPDATE``/``DO NOTHING`` conflict SQLite's ``lastrowid`` (what
    SQLAlchemy's pysqlite dialect falls back to) reports the table's most
    recently inserted row, not the conflicted one — verified empirically, not
    from docs. A trailing ``SELECT`` by the natural key is the only reliable
    way to get it back.
    """
    values = doc.values()
    values["ingested_at"] = int(time.time())
    eng = engine.get_engine()
    with eng.begin() as con:
        stmt = sqlite_insert(documents).values(**values)
        if on_conflict == "update":
            set_ = {
                col: (
                    stmt.excluded[col]
                    if col in _OVERWRITE_ON_UPSERT
                    else sa.func.coalesce(stmt.excluded[col], documents.c[col])
                )
                for col in values
                if col not in ("source", "source_id", "ingested_at")
            }
            # ``ingested_at`` is set on first insert only — COALESCE preserves
            # the original value (opposite direction from every other column,
            # which prefers the incoming value).
            set_["ingested_at"] = sa.func.coalesce(
                documents.c.ingested_at, stmt.excluded.ingested_at
            )
            stmt = stmt.on_conflict_do_update(index_elements=["source", "source_id"], set_=set_)
            con.execute(stmt)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["source", "source_id"])
            if con.execute(stmt).rowcount == 0:
                return None
        row = con.execute(
            sa.select(documents.c.id).where(
                (documents.c.source == values["source"])
                & (documents.c.source_id == values["source_id"])
            )
        ).fetchone()
    return row[0]


def insert_document_if_new(doc: DocumentWrite) -> int | None:
    """Insert a document when ``(source, source_id)`` is not already archived."""
    return _write_document(doc, on_conflict="nothing")


def upsert_document(doc: DocumentWrite) -> int:
    """Insert a document or update its mutable fields. Returns the document id."""
    return _write_document(doc, on_conflict="update")


# Deliberately separate from ``DocumentWrite``/``_write_document`` above: this
# updates a *subset* of columns (never title/item_type/note/isbn/fetch_status,
# since a post-hoc refresh must not reset those) and writes url_or_path
# unconditionally. A new column belongs here too if Zotero can backfill it.
_ZOTERO_REFRESH_COALESCE_KEYS = (
    "zotero_attachment_key",
    "doi",
    "arxiv_id",
    "year",
    "authors_json",
    "zotero_url",
    "zotero_path",
)


def refresh_zotero_metadata(by_source_id: dict[str, dict]) -> int:
    """Backfill Zotero-derived columns for already-archived rows after connector load.

    Each value in ``by_source_id`` carries ``zotero_attachment_key``, ``doi``,
    ``arxiv_id``, ``year``, ``authors_json``, ``zotero_url``, ``zotero_path``
    (``COALESCE``d — a missing value here leaves the stored one alone) and
    ``url_or_path`` (written unconditionally, already recomputed by the
    caller). The unconditional ``url_or_path`` write is what reconciles a row
    written by the old DOI-in-url_or_path ladder with one written by the new
    one; otherwise the two rows would disagree about where the DOI lives.
    """
    if not by_source_id:
        return 0
    eng = engine.get_engine()
    updated = 0
    with eng.begin() as con:
        for source_id, fields in by_source_id.items():
            values = {
                key: sa.func.coalesce(sa.literal(fields.get(key)), documents.c[key])
                for key in _ZOTERO_REFRESH_COALESCE_KEYS
            }
            values["url_or_path"] = fields.get("url_or_path")
            result = con.execute(
                sa.update(documents)
                .where((documents.c.source == Source.ZOTERO) & (documents.c.source_id == source_id))
                .values(**values)
            )
            updated += result.rowcount or 0
    return updated


def update_document_item_type(source: Source | str, source_id: str, item_type: str) -> int:
    """Set ``item_type`` on an existing document. Returns rows updated."""
    eng = engine.get_engine()
    with eng.begin() as con:
        result = con.execute(
            sa.update(documents)
            .where((documents.c.source == str(source)) & (documents.c.source_id == source_id))
            .values(item_type=item_type)
        )
    return result.rowcount or 0


def document_index(source: Source | str) -> dict[str, int]:
    """Map ``source_id`` → ``documents.id`` for one connector."""
    with engine.get_engine().connect() as con:
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
    with engine.get_engine().connect() as con:
        row = con.execute(
            sa.select(documents.c.generated_summary).where(documents.c.id == doc_id)
        ).fetchone()
    return (row[0] or None) if row else None


def set_generated_summary(doc_id: int, summary: str | None, *, run_id: int | None = None) -> None:
    """Persist (or clear) the cached LLM summary for a document.

    ``run_id`` stamps which enrichment run produced it. The stamp moves with the
    summary in both directions: clearing the text clears the provenance, since a
    run id pointing at an absent summary would make a provenance-filtered purge
    count rows it cannot delete.
    """
    with engine.get_engine().begin() as con:
        con.execute(
            documents.update()
            .where(documents.c.id == doc_id)
            .values(
                generated_summary=summary or None,
                summary_run_id=run_id if summary else None,
            )
        )


def document_titles(doc_ids: list[int]) -> dict[int, str]:
    """Batched ``documents.id`` → title (missing ids omitted, NULL title → "").

    Used by the fetched-text embed paths, which know only a document id but need
    the persisted title — a fetch handler may have overridden it — in the
    embedded text. Batched so the phase-2 loop stays a single query.
    """
    if not doc_ids:
        return {}
    with engine.get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.id, documents.c.title).where(documents.c.id.in_(doc_ids))
        ).fetchall()
    return {row[0]: row[1] or "" for row in rows}


def source_ingest_queue(
    source: Source | str,
    limit: int | None = None,
) -> list[tuple[int, str]]:
    """Pending fetch URLs for ``source`` plus fetched docs missing chunks (orphans).

    Pending rows come first; duplicates by document id are dropped (pending wins).
    """
    eng = engine.get_engine()
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
                        sa.select(chunks.c.id).where(chunks.c.document_id == documents.c.id)
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


def set_fetch_status(doc_id: int, status: FetchStatus | str) -> None:
    """Record the outcome of trying to get text for a document."""
    with engine.get_engine().begin() as con:
        con.execute(
            sa.update(documents).where(documents.c.id == doc_id).values(fetch_status=str(status))
        )


def update_card_summary(doc_id: int, summary: str | None) -> None:
    """Set or clear the card excerpt for a document."""
    with engine.get_engine().begin() as con:
        con.execute(
            sa.update(documents).where(documents.c.id == doc_id).values(card_summary=summary)
        )
