"""Linked duplicate documents: the read helpers and the writer (DESIGN.md §3.9).

A ``merged`` row in ``document_duplicates`` makes its ``duplicate_id`` read as
its ``canonical_id``: browse lists the canonical only, search scores it with
the duplicate's hits too, a tag filter finds it through either row's tags, and
clustering and tag training skip the duplicate. Nothing is deleted, so undoing
a link is one state change and a sync re-reading either row changes nothing.

The writer keeps links one hop deep: linking to a document that is itself a
merged duplicate links to its canonical instead, and a document that becomes a
duplicate hands its own duplicates to the new canonical.
"""

from __future__ import annotations

import time
from typing import Any

import sqlalchemy as sa

from pka.db import engine
from pka.db.schema import document_duplicates as dd
from pka.db.schema import documents

MATCH_KEYS = ("doi", "arxiv_id", "isbn", "url", "embedding", "manual")


class LinkError(ValueError):
    """A link that cannot be made: a document to itself, or into a cycle."""


# ── Read helpers ─────────────────────────────────────────────────────────────


def _merged():
    # A literal, not a bound parameter: SQLite uses the partial index
    # uq_document_duplicates_merged only when the query spells its condition.
    return dd.c.state == sa.literal_column("'merged'")


def exclude_duplicates(q: sa.Select) -> sa.Select:
    """Drop merged duplicates from a query selecting FROM ``documents``."""
    return q.where(
        ~sa.exists(sa.select(dd.c.id).where((dd.c.duplicate_id == documents.c.id) & _merged()))
    )


def with_duplicates(doc_col: sa.ColumnElement) -> sa.ColumnElement:
    """A condition true for ``documents.id`` itself and for its merged duplicates.

    For a correlated child-table test: ``tags.document_id`` belongs to the
    current document, or to a document merged into it.
    """
    # Correlated explicitly: this sits two SELECTs deep (inside the caller's
    # EXISTS), and auto-correlation only reaches the enclosing one, which would
    # leave `documents` uncorrelated and match every document's duplicates.
    dup_ids = (
        sa.select(dd.c.duplicate_id)
        .where((dd.c.canonical_id == documents.c.id) & _merged())
        .correlate(documents)
    )
    return (doc_col == documents.c.id) | doc_col.in_(dup_ids)


def source_in(sources: list[str]) -> sa.ColumnElement:
    """``documents.source`` is one of *sources*, or a merged duplicate's source is.

    So filtering on Zotero still finds an item whose canonical row is the
    Firefox bookmark of the same paper.
    """
    dup = documents.alias("dup_doc")
    via_duplicate = sa.exists(
        sa.select(dd.c.id)
        .select_from(dd.join(dup, dup.c.id == dd.c.duplicate_id))
        .where((dd.c.canonical_id == documents.c.id) & _merged() & dup.c.source.in_(sources))
    )
    return documents.c.source.in_(sources) | via_duplicate


def owner_of(doc_col: sa.ColumnElement) -> sa.ColumnElement:
    """The document a child row counts for: its canonical when merged, else itself."""
    canonical = (
        sa.select(dd.c.canonical_id)
        .where((dd.c.duplicate_id == doc_col) & _merged())
        .scalar_subquery()
    )
    return sa.func.coalesce(canonical, doc_col)


def merged_duplicate_ids(con: sa.Connection) -> set[int]:
    return {r[0] for r in con.execute(sa.select(dd.c.duplicate_id).where(_merged()))}


def canonical_map(con: sa.Connection) -> dict[int, int]:
    """``{duplicate_id: canonical_id}`` for every merged link."""
    return {
        r[0]: r[1]
        for r in con.execute(sa.select(dd.c.duplicate_id, dd.c.canonical_id).where(_merged()))
    }


def duplicates_of(con: sa.Connection, doc_ids: list[int]) -> dict[int, list[int]]:
    """``{canonical_id: [duplicate_id, …]}`` for the given canonicals."""
    out: dict[int, list[int]] = {}
    if not doc_ids:
        return out
    for canonical, dup in con.execute(
        sa.select(dd.c.canonical_id, dd.c.duplicate_id)
        .where(dd.c.canonical_id.in_(doc_ids) & _merged())
        .order_by(dd.c.duplicate_id)
    ):
        out.setdefault(canonical, []).append(dup)
    return out


def linked_copies(con: sa.Connection, doc_id: int) -> list[dict[str, Any]]:
    """The other documents *doc_id* is linked with, for "also saved in"."""
    canonical = con.execute(
        sa.select(dd.c.canonical_id).where((dd.c.duplicate_id == doc_id) & _merged())
    ).scalar()
    root = canonical if canonical is not None else doc_id
    ids = {root, *duplicates_of(con, [root]).get(root, [])} - {doc_id}
    if not ids:
        return []
    return [
        {"id": r.id, "source": r.source, "title": r.title or "", "url_or_path": r.url_or_path}
        for r in con.execute(
            sa.select(
                documents.c.id, documents.c.source, documents.c.title, documents.c.url_or_path
            )
            .where(documents.c.id.in_(sorted(ids)))
            .order_by(documents.c.id)
        )
    ]


# ── Writer ───────────────────────────────────────────────────────────────────


def known_pairs(con: sa.Connection) -> set[frozenset[int]]:
    """Every pair already linked, proposed or rejected, in either direction."""
    return {
        frozenset((r[0], r[1]))
        for r in con.execute(sa.select(dd.c.canonical_id, dd.c.duplicate_id))
    }


def link(
    canonical_id: int,
    duplicate_id: int,
    *,
    match_key: str = "manual",
    match_value: str | None = None,
    score: float | None = None,
    decided_by: str = "user",
    con: sa.Connection | None = None,
) -> dict[str, Any]:
    """Merge *duplicate_id* into *canonical_id*; returns the merged row.

    The canonical resolves through existing links first, *duplicate_id*'s own
    duplicates move to the new canonical, and a link *duplicate_id* already had
    to another canonical is replaced.
    """
    if con is None:
        with engine.get_engine().begin() as c:
            return link(
                canonical_id,
                duplicate_id,
                match_key=match_key,
                match_value=match_value,
                score=score,
                decided_by=decided_by,
                con=c,
            )
    now = int(time.time())
    merged = canonical_map(con)
    canonical_id = merged.get(canonical_id, canonical_id)
    if canonical_id == duplicate_id:
        raise LinkError("a document cannot be a duplicate of itself")
    for dup in [d for d, c in merged.items() if c == duplicate_id]:
        _set_merged(con, canonical_id, dup, "manual", None, None, "user", now)
    return _set_merged(
        con, canonical_id, duplicate_id, match_key, match_value, score, decided_by, now
    )


def _set_merged(con, canonical_id, duplicate_id, match_key, match_value, score, decided_by, now):
    con.execute(
        dd.update()
        .where(
            (dd.c.duplicate_id == duplicate_id) & _merged() & (dd.c.canonical_id != canonical_id)
        )
        .values(state="rejected", decided_by="user", decided_at=now)
    )
    existing = con.execute(
        sa.select(dd.c.id).where(
            (dd.c.canonical_id == canonical_id) & (dd.c.duplicate_id == duplicate_id)
        )
    ).scalar()
    values = {"state": "merged", "decided_by": decided_by, "decided_at": now}
    if existing is None:
        result = con.execute(
            dd.insert().values(
                canonical_id=canonical_id,
                duplicate_id=duplicate_id,
                match_key=match_key,
                match_value=match_value,
                score=score,
                created_at=now,
                **values,
            )
        )
        assert result.inserted_primary_key is not None
        existing = result.inserted_primary_key[0]
    else:
        con.execute(dd.update().where(dd.c.id == existing).values(**values))
    return dict(con.execute(sa.select(dd).where(dd.c.id == existing)).one()._mapping)


def add_candidates(rows: list[dict[str, Any]]) -> int:
    """Insert ``{canonical_id, duplicate_id, match_key, match_value, score}`` proposals."""
    if not rows:
        return 0
    now = int(time.time())
    added = 0
    with engine.get_engine().begin() as con:
        known = known_pairs(con)
        for row in rows:
            pair = frozenset((row["canonical_id"], row["duplicate_id"]))
            if len(pair) < 2 or pair in known:
                continue
            con.execute(
                dd.insert().values(
                    canonical_id=row["canonical_id"],
                    duplicate_id=row["duplicate_id"],
                    match_key=row["match_key"],
                    match_value=row.get("match_value"),
                    score=row.get("score"),
                    state="candidate",
                    decided_by="scan",
                    created_at=now,
                )
            )
            known.add(pair)
            added += 1
    return added


def list_links(state: str | None = None) -> list[dict[str, Any]]:
    q = sa.select(dd)
    if state:
        q = q.where(dd.c.state == state)
    q = q.order_by(dd.c.score.desc().nulls_last(), dd.c.id)
    with engine.get_engine().connect() as con:
        return [dict(r._mapping) for r in con.execute(q)]


def accept(link_id: int) -> dict[str, Any]:
    with engine.get_engine().begin() as con:
        row = con.execute(sa.select(dd).where(dd.c.id == link_id)).first()
        if row is None:
            raise KeyError(link_id)
        return link(
            row.canonical_id,
            row.duplicate_id,
            match_key=row.match_key,
            match_value=row.match_value,
            score=row.score,
            con=con,
        )


def reject(link_id: int) -> bool:
    """Decline a candidate or undo a link; the pair is not proposed again."""
    with engine.get_engine().begin() as con:
        n = con.execute(
            dd.update()
            .where(dd.c.id == link_id)
            .values(state="rejected", decided_by="user", decided_at=int(time.time()))
        ).rowcount
    return bool(n)


def delete_for_documents(con: sa.Connection, doc_ids: list[int]) -> int:
    """Remove every link touching *doc_ids*, for a purge that deletes them."""
    if not doc_ids:
        return 0
    return con.execute(
        dd.delete().where(dd.c.canonical_id.in_(doc_ids) | dd.c.duplicate_id.in_(doc_ids))
    ).rowcount
