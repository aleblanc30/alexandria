"""Source tags and collections written at ingestion, and the tag listing."""

from typing import Any

import sqlalchemy as sa

from pka.constants import Source, TagOrigin
from pka.db import engine
from pka.db.browse import apply_document_browse_filters, norm_filter
from pka.db.schema import documents, overlay_tags, source_collections, source_tags


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
    if not cols:
        return
    eng = engine.get_engine()
    with eng.begin() as con:
        con.execute(
            source_collections.delete().where(
                (source_collections.c.document_id == document_id)
                & (source_collections.c.source == str(source))
            )
        )
        con.execute(
            source_collections.insert(),
            [{"document_id": document_id, "collection": c, "source": str(source)} for c in cols],
        )


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
    source_filter = norm_filter(sources)
    source_tag_filter = norm_filter(source_tag_filter)
    cluster_l1_tag_filter = norm_filter(cluster_l1_tag_filter)
    cluster_l2_tag_filter = norm_filter(cluster_l2_tag_filter)
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

    with engine.get_engine().connect() as con:
        doc_scope = None
        if has_doc_scope:
            doc_scope = apply_document_browse_filters(
                sa.select(documents.c.id),
                **filter_kwargs,
            )

        # ``grp`` exists only to order source tags ahead of overlay tags on a
        # count tie, which is what the previous Python-side stable sort did by
        # concatenating the two result lists in that order. It is dropped from
        # the projection below.
        src_q = (
            sa.select(
                source_tags.c.tag_string.label("tag"),
                sa.literal("source").label("origin"),
                sa.func.count(source_tags.c.id).label("n"),
                sa.literal(0).label("grp"),
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
                sa.literal(1).label("grp"),
            )
            .select_from(overlay_tags)
            .group_by(overlay_tags.c.tag, overlay_tags.c.origin)
        )
        if doc_scope is not None:
            ov_q = ov_q.where(overlay_tags.c.document_id.in_(doc_scope))
        if q:
            ov_q = ov_q.where(overlay_tags.c.tag.ilike(f"%{q}%"))

        overlay_origins = {
            str(TagOrigin.INFERRED),
            str(TagOrigin.MANUAL),
            str(TagOrigin.LLM),
            str(TagOrigin.CLUSTER_L1),
            str(TagOrigin.CLUSTER_L2),
            str(TagOrigin.LEARNED),
        }
        parts = []
        if not origin or origin == "source":
            parts.append(src_q)
        if not origin or origin in overlay_origins:
            # Filtering before the GROUP BY is equivalent to filtering the rows
            # after it, because ``origin`` is part of the group key.
            parts.append(ov_q.where(overlay_tags.c.origin == origin) if origin else ov_q)
        if not parts:
            # An origin that is neither "source" nor an overlay origin matches
            # nothing, as it did when both branches were skipped.
            return []

        # ORDER BY / LIMIT belong in SQL: the two GROUP BYs are over the whole
        # of source_tags and overlay_tags, so ranking in Python meant building
        # every distinct tag in the archive to return `limit` of them.
        combined = sa.union_all(*parts).subquery() if len(parts) > 1 else parts[0].subquery()
        stmt = (
            sa.select(combined.c.tag, combined.c.origin, combined.c.n)
            .order_by(combined.c.n.desc(), combined.c.grp.asc(), combined.c.tag.asc())
            .limit(limit)
        )
        return [{"tag": r[0], "origin": r[1], "count": r[2]} for r in con.execute(stmt).fetchall()]
