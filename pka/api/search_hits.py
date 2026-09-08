"""Stages of the ``/search`` query, as functions over ranked document ids.

Every stage consumes and returns :data:`Hits` — ``(document_id, similarity)``
pairs, most-similar first, where a similarity of ``None`` means "matched, but
not by anything that produces a score" (the fulltext branch). ``None`` is not
interchangeable with ``0.0``: :func:`merge_clip_hits` partitions on it, and it
reaches the API as ``DocumentOut.similarity``.

:mod:`pka.api.routers.search` composes these in order and paginates. They live
here rather than in the router so they can be tested without going through
FastAPI, the way :mod:`pka.api.image_hits` and :mod:`pka.api.document_serialize`
already are.
"""

import logging

import sqlalchemy as sa

from pka.api.db_rows import fetchall_mappings
from pka.api.schemas.search import SearchRequest
from pka.constants import Source
from pka.db.queries import filter_document_ids
from pka.db.schema import cluster_assignments, documents

log = logging.getLogger(__name__)

Hits = list[tuple[int, float | None]]

# Ceiling on the chunk hits one semantic query asks Chroma for (audit P-5).
#
# The request is over-fetched 3x because hits are chunk-level and collapse to
# one row per document, so asking for exactly ``offset + limit`` routinely comes
# back with too few distinct documents to fill the page. That term scales with
# page depth rather than page size: at the default limit, page 1 asks for 60 and
# offset 2000 asks for 6060, all to return 20 rows.
#
# The trade this makes is explicit. Past the cap a deep page sees fewer
# candidates than it would need, so ``total`` drops and some documents stop
# appearing. At the default limit that starts around offset 313, well beyond
# where anyone pages by hand. It also bounds a request that sets a very large
# ``limit``, which nothing else currently does.
_MAX_SEMANTIC_HITS = 1000


def semantic_hits(req: SearchRequest) -> Hits:
    """Query the vector store and collapse chunk hits to one row per document.

    Returns ``[]`` — rather than raising — when the vector store is unavailable,
    so the caller can fall through to fulltext. That fallback is the reason the
    ``except`` is broad: any Chroma failure should degrade the search, not 500 it.
    """
    from pka.storage.vector_store import query as vquery

    try:
        where_filter: dict = {}
        if req.sources:
            where_filter["source"] = {"$in": [str(s) for s in req.sources]}
        # Fetch enough hits to fill the requested page, not just page 1.
        hits = vquery(
            req.query,
            n_results=min((req.offset + req.limit) * 3, _MAX_SEMANTIC_HITS),
            where=where_filter or None,
        )
    except Exception:  # noqa: BLE001 - a vector store outage falls back to fulltext
        log.warning(
            "Semantic search unavailable; falling back to fulltext",
            exc_info=True,
        )
        return []

    seen: dict[int, float] = {}
    for h in hits:
        did = int(h["metadata"].get("document_id", -1))
        sim = float(1.0 - h["distance"])
        if did not in seen or sim > seen[did]:
            seen[did] = sim
    return sorted(seen.items(), key=lambda x: -x[1])


def fulltext_hits(con, req: SearchRequest) -> Hits:
    """Title substring matches, in ``documents.id`` order, with no similarity.

    No ``LIMIT``: pagination happens after merging and filtering, so a
    pre-limited fetch would make page 2+ incomplete and undercount the total.
    Replacing the scan with an FTS5 index is a separate follow-up; this is the
    one place the query lives.
    """
    q = (
        sa.select(documents.c.id)
        .where(documents.c.title.ilike(f"%{req.query}%"))
        .order_by(documents.c.id)
    )
    if req.sources:
        q = q.where(documents.c.source.in_([str(s) for s in req.sources]))
    return [(row[0], None) for row in con.execute(q)]


def merge_new(base: Hits, extra: Hits) -> Hits:
    """Append entries of *extra* whose document id is not already in *base*."""
    existing = {doc_id for doc_id, _ in base}
    return base + [(doc_id, sim) for doc_id, sim in extra if doc_id not in existing]


def merge_clip_hits(results: Hits, req: SearchRequest) -> Hits:
    """Fold purely-visual CLIP matches into *results* by best similarity.

    Image documents already surface via their inferred-text vectors (the
    semantic stage queries the same collection those chunks live in); CLIP adds
    matches that share no words with the image. It is off by default —
    ``search_images_by_text`` short-circuits to [] unless ``clip_enabled``,
    leaving the text path as the only one, which is why nothing here needs a
    flag check of its own.

    Scored entries come back sorted by similarity and unscored ones after them,
    so in ``fulltext`` mode a CLIP hit moves its document ahead of the plain
    ``documents.id`` ordering. That is intended, and comparing a CLIP score
    against a MiniLM one is the same deliberate approximation
    :func:`pka.api.image_hits.merge_image_hits` documents.
    """
    images_in_scope = (not req.sources) or (Source.IMAGE in req.sources)
    if not req.query.strip() or not images_in_scope:
        return results

    try:
        from pka.ingestion.image_pipeline import search_images_by_text

        clip_hits = search_images_by_text(req.query, n=max(10, req.offset + req.limit))
    except Exception:  # noqa: BLE001 - CLIP is optional; search still answers
        log.warning("CLIP image search unavailable", exc_info=True)
        clip_hits = []
    if not clip_hits:
        return results

    best: dict[int, float | None] = {}
    for doc_id, sim in results:
        if doc_id not in best or (sim is not None and (best[doc_id] is None or sim > best[doc_id])):
            best[doc_id] = sim
    for hit in clip_hits:
        did = hit.get("document_id")
        if did is None:
            continue
        did = int(did)
        sim = 1.0 - hit["distance"]
        if did not in best or best[did] is None or sim > best[did]:
            best[did] = sim

    scored = sorted(((d, s) for d, s in best.items() if s is not None), key=lambda x: -x[1])
    unscored: Hits = [(d, None) for d, s in best.items() if s is None]
    return scored + unscored


def apply_browse_filters(con, results: Hits, req: SearchRequest) -> Hits:
    """Drop hits failing the browse-style filters, preserving order."""
    wanted = (
        req.sources
        or req.source_tags
        or req.general_tags
        or req.cluster_l1_tags
        or req.cluster_l2_tags
        or req.wayback_only
    )
    if not results or not wanted:
        return results

    allowed = filter_document_ids(
        con,
        [doc_id for doc_id, _ in results],
        source_filter=[str(s) for s in req.sources] if req.sources else None,
        source_tag_filter=req.source_tags or None,
        general_tag_filter=req.general_tags or None,
        cluster_l1_tag_filter=req.cluster_l1_tags or None,
        cluster_l2_tag_filter=req.cluster_l2_tags or None,
        wayback_only=req.wayback_only,
    )
    return [(doc_id, sim) for doc_id, sim in results if doc_id in allowed]


def apply_row_filters(con, results: Hits, req: SearchRequest, run_id: int | None) -> Hits:
    """Drop hits failing the per-row filters, preserving order.

    ``cluster_membership`` is loaded only when there is an active run, so a
    ``cluster_ids`` filter with no run drops everything rather than matching
    everything. That is deliberate: without a run there is no membership to
    test against, and silently ignoring the filter would return documents the
    caller explicitly excluded.

    The row fetch is sized by the pre-pagination result count, not by
    ``req.limit``, so it selects the three columns it reads rather than the
    whole table: a query matching several thousand titles would otherwise pull
    each row's 1.5 KB ``doc_embedding`` blob to compare a timestamp.
    """
    if not (req.cluster_ids or req.tags or req.date_from or req.date_to or req.fetch_status):
        return results

    doc_ids_to_check = [d for d, _ in results]
    row_map = {
        r["id"]: r
        for r in fetchall_mappings(
            con.execute(
                sa.select(
                    documents.c.id,
                    documents.c.fetch_status,
                    documents.c.date_added,
                ).where(documents.c.id.in_(doc_ids_to_check))
            )
        )
    }
    cluster_membership: dict[int, int] = {}
    if req.cluster_ids and run_id:
        cluster_membership = {
            r[0]: r[1]
            for r in con.execute(
                sa.select(
                    cluster_assignments.c.document_id,
                    cluster_assignments.c.cluster_id,
                ).where(
                    (cluster_assignments.c.run_id == run_id)
                    & (cluster_assignments.c.document_id.in_(doc_ids_to_check))
                )
            ).fetchall()
        }

    filtered: Hits = []
    for doc_id, sim in results:
        row = row_map.get(doc_id)
        if not row:
            continue
        if req.fetch_status and row["fetch_status"] != req.fetch_status:
            continue
        if req.date_from and (row["date_added"] or 0) < req.date_from:
            continue
        if req.date_to and (row["date_added"] or 0) > req.date_to:
            continue
        if req.cluster_ids:
            cid = cluster_membership.get(doc_id)
            if cid not in req.cluster_ids:
                continue
        filtered.append((doc_id, sim))
    return filtered
