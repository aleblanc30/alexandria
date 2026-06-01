"""``/search`` endpoint — semantic, fulltext, and hybrid modes.

The N+1 query problem in the per-document lookup loop is avoided by
:func:`pka.api.document_serialize.documents_out_batch`, which collects all
required relations (``source_tags``, ``overlay_tags``, ``cluster_assignments``)
in batched ``IN`` queries.
"""
import sqlalchemy as sa
from fastapi import APIRouter, Depends

from pka.api.active_run import fetch_active_run_id
from pka.api.db_rows import fetchall_mappings
from pka.api.dependencies import get_engine
from pka.api.document_serialize import documents_out_batch
from pka.api.image_hits import clip_hits_to_image_out
from pka.api.schemas.images import ImageOut
from pka.api.schemas.search import SearchRequest, SearchResponse
from pka.db.queries import filter_document_ids
from pka.db.schema import cluster_assignments, documents

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(req: SearchRequest, engine=Depends(get_engine)):
    from pka.storage.vector_store import query as vquery

    results: list[tuple[int, float | None]] = []

    # ── Semantic / hybrid ────────────────────────────────────────────────────
    if req.mode in ("semantic", "hybrid"):
        try:
            where_filter: dict = {}
            if req.sources:
                where_filter["source"] = {"$in": [str(s) for s in req.sources]}
            hits = vquery(req.query, n_results=req.limit * 3, where=where_filter or None)
            seen: dict[int, float] = {}
            for h in hits:
                did = int(h["metadata"].get("document_id", -1))
                sim = float(1.0 - h["distance"])
                if did not in seen or sim > seen[did]:
                    seen[did] = sim
            results = sorted(seen.items(), key=lambda x: -x[1])
        except Exception:
            # Fall through to fulltext if the vector store is unavailable
            pass

    with engine.connect() as con:
        run_id = fetch_active_run_id(con)

        # ── Fulltext fallback / merge ────────────────────────────────────────
        if req.mode in ("fulltext", "hybrid") or not results:
            q = sa.select(documents).where(
                documents.c.title.ilike(f"%{req.query}%")
            )
            if req.sources:
                q = q.where(documents.c.source.in_([str(s) for s in req.sources]))
            rows = fetchall_mappings(con.execute(q.limit(req.limit)))
            existing_ids = {r[0] for r in results}
            for row in rows:
                if row["id"] not in existing_ids:
                    results.append((row["id"], None))

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

        # ── Images ────────────────────────────────────────────────────────────
        images_out: list[ImageOut] = []
        if req.include_images:
            from pka.ingestion.image_pipeline import search_images_by_text

            images_out = clip_hits_to_image_out(
                con, search_images_by_text(req.query, n=10)
            )

    return SearchResponse(
        query=req.query, total=total,
        documents=docs_out, images=images_out,
    )
