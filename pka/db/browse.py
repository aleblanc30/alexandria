"""The paginated browse list and the filters it shares with search and tags."""

from typing import Any

import sqlalchemy as sa

from pka.constants import Source, TagOrigin
from pka.db import engine
from pka.db.cards import first_chunk_map, resolve_description
from pka.db.schema import documents, images, overlay_tags, source_tags


def norm_filter(values: list | None) -> list[str] | None:
    """Stringify a browse filter list, or ``None`` when empty."""
    return [str(v) for v in values] if values else None


def _where_source_tag(q: sa.Select, tag: str) -> sa.Select:
    return q.where(
        sa.exists(
            sa.select(source_tags.c.id).where(
                (source_tags.c.document_id == documents.c.id) & (source_tags.c.tag_string == tag)
            )
        )
    )


def _where_overlay_tag(q: sa.Select, tag: str, origin=None) -> sa.Select:
    cond = (overlay_tags.c.document_id == documents.c.id) & (overlay_tags.c.tag == tag)
    if origin is not None:
        cond = cond & (overlay_tags.c.origin == origin)
    return q.where(sa.exists(sa.select(overlay_tags.c.id).where(cond)))


def apply_document_browse_filters(
    q: sa.Select,
    *,
    source_filter: list[str] | None,
    source_tag_filter: list[str] | None,
    overlay_tag_filter: list[str] | None,
    general_tag_filter: list[str] | None = None,
    cluster_l1_tag_filter: list[str] | None = None,
    cluster_l2_tag_filter: list[str] | None = None,
    learned_tag_filter: list[str] | None = None,
    collection_tag_filter: list[str] | None = None,
    wayback_only: bool = False,
) -> sa.Select:
    if source_filter:
        q = q.where(documents.c.source.in_(source_filter))
    if wayback_only:
        q = q.where(
            (documents.c.source == str(Source.FIREFOX)) & documents.c.archive_url.isnot(None)
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
    for tag in collection_tag_filter or []:
        q = _where_overlay_tag(q, tag, TagOrigin.COLLECTION)
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
    collection_tag_filter: list[str] | None = None,
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
            collection_tag_filter,
            wayback_only,
        )
    )
    if not has_filters:
        return set(doc_ids)
    q = sa.select(documents.c.id).where(documents.c.id.in_(doc_ids))
    q = apply_document_browse_filters(
        q,
        source_filter=source_filter,
        source_tag_filter=source_tag_filter,
        overlay_tag_filter=None,
        general_tag_filter=general_tag_filter,
        cluster_l1_tag_filter=cluster_l1_tag_filter,
        cluster_l2_tag_filter=cluster_l2_tag_filter,
        collection_tag_filter=collection_tag_filter,
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
            overlay_tags.c.origin.in_([TagOrigin.CLUSTER_L1, TagOrigin.CLUSTER_L2]),
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
    collection_tags: list[str] | None = None,
    wayback_only: bool = False,
    limit: int = 48,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Paginated document browse list with card summary or first-chunk snippet as description."""
    filter_kwargs = {
        "source_filter": norm_filter(sources),
        "source_tag_filter": norm_filter(source_tags),
        "overlay_tag_filter": norm_filter(overlay_tags),
        "general_tag_filter": norm_filter(general_tags),
        "cluster_l1_tag_filter": norm_filter(cluster_l1_tags),
        "cluster_l2_tag_filter": norm_filter(cluster_l2_tags),
        "learned_tag_filter": norm_filter(learned_tags),
        "collection_tag_filter": norm_filter(collection_tags),
        "wayback_only": wayback_only,
    }

    with engine.get_engine().connect() as con:
        count_q = _exclude_pending_images(
            apply_document_browse_filters(
                sa.select(sa.func.count()).select_from(documents),
                **filter_kwargs,
            )
        )
        total = con.execute(count_q).scalar() or 0

        page_q = (
            _exclude_pending_images(
                apply_document_browse_filters(
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
            )
            .order_by(
                documents.c.date_added.is_(None),
                documents.c.date_added.desc(),
                documents.c.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        rows = con.execute(page_q).fetchall()

        doc_ids = [r[0] for r in rows]
        snippet_map: dict[int, str] = {}
        if doc_ids:
            needs_chunk = [r[0] for r in rows if not (r[7] and str(r[7]).strip())]
            if needs_chunk:
                snippet_map = first_chunk_map(con, needs_chunk)
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
