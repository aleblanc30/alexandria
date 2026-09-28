"""Source tags and collections written at ingestion, and the tag listing."""

import time
from collections.abc import Collection, Iterable
from typing import Any

import sqlalchemy as sa

from pka.constants import Source, TagOrigin
from pka.db import engine
from pka.db.browse import apply_document_browse_filters, norm_filter
from pka.db.duplicates import exclude_duplicates, owner_of
from pka.db.schema import documents, overlay_tags, source_collections, source_tags
from pka.db.tag_fold import SOURCE_ORIGIN, fold_map


def insert_source_tags(document_id: int, tags: list[str], source: Source | str) -> None:
    if not tags:
        return
    eng = engine.get_engine()
    with eng.begin() as con:
        # Replace existing tags for this (document, source) pair
        con.execute(
            source_tags.delete().where(
                (source_tags.c.document_id == document_id) & (source_tags.c.source == str(source))
            )
        )
        con.execute(
            source_tags.insert(),
            [{"document_id": document_id, "tag_string": t, "source": str(source)} for t in tags],
        )


def insert_source_collections(
    document_id: int,
    cols: list[str],
    source: Source | str,
) -> None:
    """Replace a document's collections from *source*; an empty list clears them.

    Clearing matters: an item taken out of its last collection must lose the
    old rows, or collection tags derived from them would come back.
    """
    eng = engine.get_engine()
    with eng.begin() as con:
        con.execute(
            source_collections.delete().where(
                (source_collections.c.document_id == document_id)
                & (source_collections.c.source == str(source))
            )
        )
        if cols:
            con.execute(
                source_collections.insert(),
                [
                    {"document_id": document_id, "collection": c, "source": str(source)}
                    for c in cols
                ],
            )


def sync_overlay_tags(
    document_id: int,
    desired: Iterable[str],
    origin: TagOrigin | str,
    *,
    within: Collection[str] | None = None,
) -> None:
    """Make one document's overlay tags of *origin* exactly *desired*.

    Adds the missing tags and deletes the stale ones, so a re-run converges
    instead of accumulating. With *within*, only tags in that set are read or
    deleted: an origin shared with other writers keeps theirs.
    """
    wanted = set(desired)
    if within is not None:
        wanted &= set(within)
    origin = str(origin)
    scope = (overlay_tags.c.document_id == document_id) & (overlay_tags.c.origin == origin)
    if within is not None:
        scope = scope & overlay_tags.c.tag.in_(sorted(within))
    now = int(time.time())
    with engine.get_engine().begin() as con:
        existing = {r[0] for r in con.execute(sa.select(overlay_tags.c.tag).where(scope))}
        for tag in sorted(wanted - existing):
            con.execute(
                sa.text("""
                    INSERT OR IGNORE INTO overlay_tags
                        (document_id, tag, origin, confidence, created_at)
                    VALUES (:did, :tag, :origin, 1.0, :now)
                """),
                {"did": document_id, "tag": tag, "origin": origin, "now": now},
            )
        stale = existing - wanted
        if stale:
            con.execute(overlay_tags.delete().where(scope & overlay_tags.c.tag.in_(sorted(stale))))


def list_tags(
    origin: str | None = None,
    sources: list[str] | None = None,
    source_tag_filter: list[str] | None = None,
    cluster_l1_tag_filter: list[str] | None = None,
    cluster_l2_tag_filter: list[str] | None = None,
    collection_tag_filter: list[str] | None = None,
    wayback_only: bool = False,
    q: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List tags with counts, optionally scoped to documents matching browse filters."""
    source_filter = norm_filter(sources)
    source_tag_filter = norm_filter(source_tag_filter)
    cluster_l1_tag_filter = norm_filter(cluster_l1_tag_filter)
    cluster_l2_tag_filter = norm_filter(cluster_l2_tag_filter)
    collection_tag_filter = norm_filter(collection_tag_filter)
    filter_kwargs = {
        "source_filter": source_filter,
        "source_tag_filter": source_tag_filter,
        "overlay_tag_filter": None,
        "cluster_l1_tag_filter": cluster_l1_tag_filter,
        "cluster_l2_tag_filter": cluster_l2_tag_filter,
        "collection_tag_filter": collection_tag_filter,
        "wayback_only": wayback_only,
    }
    has_doc_scope = any(
        (
            source_filter,
            source_tag_filter,
            cluster_l1_tag_filter,
            cluster_l2_tag_filter,
            collection_tag_filter,
            wayback_only,
        )
    )

    with engine.get_engine().connect() as con:
        doc_scope = None
        if has_doc_scope:
            doc_scope = exclude_duplicates(
                apply_document_browse_filters(sa.select(documents.c.id), **filter_kwargs)
            )

        src_q = (
            sa.select(
                source_tags.c.tag_string.label("tag"),
                sa.literal(SOURCE_ORIGIN).label("origin"),
                # Per item: a linked duplicate counts as its canonical (§3.9).
                sa.func.count(sa.distinct(owner_of(source_tags.c.document_id))).label("n"),
            )
            .select_from(source_tags)
            .group_by(source_tags.c.tag_string)
        )
        if doc_scope is not None:
            src_q = src_q.where(owner_of(source_tags.c.document_id).in_(doc_scope))

        ov_q = (
            sa.select(
                overlay_tags.c.tag.label("tag"),
                overlay_tags.c.origin.label("origin"),
                sa.func.count(sa.distinct(owner_of(overlay_tags.c.document_id))).label("n"),
            )
            .select_from(overlay_tags)
            .group_by(overlay_tags.c.tag, overlay_tags.c.origin)
        )
        if doc_scope is not None:
            ov_q = ov_q.where(owner_of(overlay_tags.c.document_id).in_(doc_scope))

        overlay_origins = {
            str(TagOrigin.INFERRED),
            str(TagOrigin.MANUAL),
            str(TagOrigin.LLM),
            str(TagOrigin.CLUSTER_L1),
            str(TagOrigin.CLUSTER_L2),
            str(TagOrigin.LEARNED),
            str(TagOrigin.COLLECTION),
        }
        parts = []
        if not origin or origin == SOURCE_ORIGIN:
            parts.append(src_q)
        if not origin or origin in overlay_origins:
            # Filtering before the GROUP BY is equivalent to filtering the rows
            # after it, because ``origin`` is part of the group key.
            parts.append(ov_q.where(overlay_tags.c.origin == origin) if origin else ov_q)
        if not parts:
            # An origin that is neither "source" nor an overlay origin matches
            # nothing, as it did when both branches were skipped.
            return []
        rows = con.execute(sa.union_all(*parts) if len(parts) > 1 else parts[0]).fetchall()
        folded = _fold_rows(con, rows, doc_scope)

    # Ranked in Python: the fold changes both the rows and their counts, so SQL
    # cannot order them. Source tags first on a tie, then alphabetical.
    out = [f for f in folded if not q or any(q.lower() in v.lower() for v in f["variants"])]
    out.sort(key=lambda f: (-f["count"], f["origin"] != SOURCE_ORIGIN, f["tag"]))
    return out[:limit]


def _fold_rows(con: sa.Connection, rows, doc_scope) -> list[dict[str, Any]]:
    """Merge ``(tag, origin, documents)`` rows that read as one tag (DESIGN.md §3.8).

    Each row counts distinct documents, so a group of one keeps its count. A
    group of several is recounted, because one document can carry two
    spellings of the same tag and must count once.
    """
    fm = fold_map()
    groups: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for tag, origin, n in rows:
        groups.setdefault((origin, fm.canonical(tag, origin)), []).append((tag, n))

    multi = {k: [t for t, _ in v] for k, v in groups.items() if len(v) > 1}
    recount: dict[tuple[str, str], int] = {}
    if multi:
        docs: dict[tuple[str, str], set[int]] = {k: set() for k in multi}
        by_raw: dict[tuple[str, str], tuple[str, str]] = {
            (o, t): k for k, tags in multi.items() for t in tags for o in (k[0],)
        }
        src_raw = [t for (o, t) in by_raw if o == SOURCE_ORIGIN]
        ov_raw = [t for (o, t) in by_raw if o != SOURCE_ORIGIN]
        if src_raw:
            owner = owner_of(source_tags.c.document_id)
            q = sa.select(source_tags.c.tag_string, owner).where(
                source_tags.c.tag_string.in_(src_raw)
            )
            if doc_scope is not None:
                q = q.where(owner.in_(doc_scope))
            for tag, doc_id in con.execute(q):
                docs[by_raw[(SOURCE_ORIGIN, tag)]].add(doc_id)
        if ov_raw:
            owner = owner_of(overlay_tags.c.document_id)
            q = sa.select(overlay_tags.c.tag, overlay_tags.c.origin, owner).where(
                overlay_tags.c.tag.in_(ov_raw)
            )
            if doc_scope is not None:
                q = q.where(owner.in_(doc_scope))
            for tag, origin, doc_id in con.execute(q):
                key = by_raw.get((origin, tag))
                if key is not None:
                    docs[key].add(doc_id)
        recount = {k: len(v) for k, v in docs.items()}

    out = []
    for (origin, canonical), members in groups.items():
        variants = sorted(members, key=lambda m: (-m[1], m[0]))
        out.append(
            {
                "tag": fm.display(variants[0][0], origin),
                "origin": origin,
                "count": recount.get((origin, canonical), variants[0][1]),
                "variants": [t for t, _ in variants],
            }
        )
    return out
