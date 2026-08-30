"""``/search`` endpoint — semantic, fulltext, and hybrid modes.

The N+1 query problem in the per-document lookup loop is avoided by
:func:`pka.api.document_serialize.documents_out_batch`, which collects all
required relations (``source_tags``, ``overlay_tags``, ``cluster_assignments``)
in batched ``IN`` queries.
"""
import logging

import sqlalchemy as sa
from fastapi import APIRouter, Depends

from pka.api.active_run import fetch_active_run_id
from pka.api.db_rows import fetchall_mappings
from pka.api.dependencies import get_engine
from pka.api.document_serialize import documents_out_batch
from pka.api.schemas.search import SearchRequest, SearchResponse
from pka.constants import Source
from pka.db.queries import filter_document_ids
from pka.db.schema import cluster_assignments, documents

log = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
def search(req: SearchRequest, engine=Depends(get_engine)):
    from pka.storage.vector_store import query as vquery

    results: list[tuple[int, float | None]] = []

    # ── Semantic / hybrid ────────────────────────────────────────────────────
    if req.mode in ("semantic", "hybrid"):
        try:
            where_filter: dict = {}
            if req.sources:
                where_filter["source"] = {"$in": [str(s) for s in req.sources]}
            # Fetch enough hits to fill the requested page, not just page 1.
            hits = vquery(
                req.query,
                n_results=(req.offset + req.limit) * 3,
                where=where_filter or None,
            )
            seen: dict[int, float] = {}
            for h in hits:
                did = int(h["metadata"].get("document_id", -1))
                sim = float(1.0 - h["distance"])
                if did not in seen or sim > seen[did]:
                    seen[did] = sim
            results = sorted(seen.items(), key=lambda x: -x[1])
        except Exception:
            # Fall through to fulltext if the vector store is unavailable
            log.warning(
                "Semantic search unavailable; falling back to fulltext", exc_info=True,
            )

    with engine.connect() as con:
        run_id = fetch_active_run_id(con)

        # ── Fulltext fallback / merge ────────────────────────────────────────
        if req.mode in ("fulltext", "hybrid") or not results:
            # No LIMIT here: pagination happens after merging/filtering, so a
            # pre-limited fetch would make page 2+ incomplete and undercount total.
            q = (
                sa.select(documents.c.id)
                .where(documents.c.title.ilike(f"%{req.query}%"))
                .order_by(documents.c.id)
            )
            if req.sources:
                q = q.where(documents.c.source.in_([str(s) for s in req.sources]))
            existing_ids = {doc_id for doc_id, _ in results}
            for row in con.execute(q):
                if row[0] not in existing_ids:
                    results.append((row[0], None))

        # ── CLIP cross-modal matches merged into the same result list ────────
        # Image documents already surface via their inferred-text vectors (the
        # semantic branch above queries the same collection those chunks live
        # in); CLIP adds purely-visual matches (query text → image) that share
        # no words with the image, folded in by (max) similarity. It is off by
        # default — ``search_images_by_text`` short-circuits to [] unless
        # ``clip_enabled``, leaving the text path as the only one, which is why
        # nothing here needs a flag check of its own.
        images_in_scope = (not req.sources) or (Source.IMAGE in req.sources)
        if req.query.strip() and images_in_scope:
            try:
                from pka.ingestion.image_pipeline import search_images_by_text

                clip_hits = search_images_by_text(
                    req.query, n=max(10, req.offset + req.limit),
                )
            except Exception:
                log.warning("CLIP image search unavailable", exc_info=True)
                clip_hits = []
            if clip_hits:
                best: dict[int, float | None] = {}
                for doc_id, sim in results:
                    if doc_id not in best or (
                        sim is not None and (best[doc_id] is None or sim > best[doc_id])
                    ):
                        best[doc_id] = sim
                for hit in clip_hits:
                    did = hit.get("document_id")
                    if did is None:
                        continue
                    did = int(did)
                    sim = 1.0 - hit["distance"]
                    if did not in best or best[did] is None or sim > best[did]:
                        best[did] = sim
                scored = sorted(
                    ((d, s) for d, s in best.items() if s is not None),
                    key=lambda x: -x[1],
                )
                unscored = [(d, None) for d, s in best.items() if s is None]
                results = scored + unscored

        # ── Browse-style filters (sources, tags, wayback) ─────────────────────
        if results and (
            req.sources
            or req.source_tags
            or req.general_tags
            or req.cluster_l1_tags
            or req.cluster_l2_tags
            or req.wayback_only
        ):
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
            results = [(doc_id, sim) for doc_id, sim in results if doc_id in allowed]

        # ── Filters: cluster_ids, tags, date range, fetch_status ─────────────
        if req.cluster_ids or req.tags or req.date_from or req.date_to or req.fetch_status:
            filtered: list[tuple[int, float | None]] = []
            doc_ids_to_check = [d for d, _ in results]
            row_map = {
                r["id"]: r for r in fetchall_mappings(con.execute(
                    sa.select(documents).where(documents.c.id.in_(doc_ids_to_check))
                ))
            }
            cluster_membership: dict[int, int] = {}
            if req.cluster_ids and run_id:
                cluster_membership = {
                    r[0]: r[1] for r in con.execute(
                        sa.select(
                            cluster_assignments.c.document_id,
                            cluster_assignments.c.cluster_id,
                        ).where(
                            (cluster_assignments.c.run_id == run_id) &
                            (cluster_assignments.c.document_id.in_(doc_ids_to_check))
                        )
                    ).fetchall()
                }

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
            results = filtered

        # ── Paginate ──────────────────────────────────────────────────────────
        total = len(results)
        page = results[req.offset: req.offset + req.limit]

        docs_out = documents_out_batch(page, con, run_id)

    return SearchResponse(query=req.query, total=total, documents=docs_out)
