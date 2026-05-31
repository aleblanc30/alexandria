"""``/search`` endpoint — semantic, fulltext, and hybrid modes.

The N+1 query problem in the per-document lookup loop is avoided by
:func:`_batch_doc_rows_to_out`, which collects all required relations
(``source_tags``, ``overlay_tags``, ``cluster_assignments``) in three
batched ``IN`` queries.
"""
import sqlalchemy as sa
from fastapi import APIRouter, Depends

from pka.api.db_rows import fetchall_mappings, fetchone_mapping
from pka.api.dependencies import get_engine
from pka.api.schemas.documents import DocumentOut
from pka.api.schemas.images import ImageOut
from pka.api.schemas.search import SearchRequest, SearchResponse
from pka.db.schema import (
    cluster_assignments,
    cluster_runs,
    clusters,
    documents,
    overlay_tags,
    source_tags,
)

router = APIRouter(prefix="/search", tags=["search"])


def _active_run_id(con) -> int | None:
    row = con.execute(
        sa.select(cluster_runs.c.run_id)
        .where(cluster_runs.c.accepted == True)  # noqa: E712 — SQLA expression
        .order_by(cluster_runs.c.run_id.desc())
        .limit(1)
    ).fetchone()
    return row[0] if row else None


def _batch_doc_rows_to_out(
    doc_ids_with_sim: list[tuple[int, float | None]],
    con,
    run_id: int | None,
) -> list[DocumentOut]:
    """Build a list of :class:`DocumentOut` with a single batched query per relation."""
    if not doc_ids_with_sim:
        return []

    sim_map = dict(doc_ids_with_sim)
    doc_ids = list(sim_map.keys())

    # 1. Documents (one query)
    doc_rows = {
        r["id"]: r for r in fetchall_mappings(con.execute(
            sa.select(documents).where(documents.c.id.in_(doc_ids))
        ))
    }

    # 2. Source tags (one query)
    source_tag_map: dict[int, list[str]] = {did: [] for did in doc_ids}
    for r in con.execute(
        sa.select(source_tags.c.document_id, source_tags.c.tag_string)
        .where(source_tags.c.document_id.in_(doc_ids))
    ).fetchall():
        source_tag_map[r[0]].append(r[1])

    # 3. Overlay tags (one query)
    overlay_tag_map: dict[int, list[dict]] = {did: [] for did in doc_ids}
    for r in con.execute(
        sa.select(
            overlay_tags.c.document_id,
            overlay_tags.c.tag,
            overlay_tags.c.origin,
            overlay_tags.c.confidence,
        ).where(overlay_tags.c.document_id.in_(doc_ids))
    ).fetchall():
        overlay_tag_map[r[0]].append(
            {"tag": r[1], "origin": r[2], "confidence": r[3]}
        )

    # 4. Cluster assignments + labels (single join)
    cluster_map: dict[int, tuple[int, str]] = {}
    if run_id:
        for r in con.execute(
            sa.select(
                cluster_assignments.c.document_id,
                cluster_assignments.c.cluster_id,
                clusters.c.label,
            )
            .join(clusters, clusters.c.cluster_id == cluster_assignments.c.cluster_id)
            .where(
                (cluster_assignments.c.run_id == run_id) &
                (cluster_assignments.c.document_id.in_(doc_ids))
            )
        ).fetchall():
            cluster_map[r[0]] = (r[1], r[2])

    out: list[DocumentOut] = []
    for doc_id, sim in doc_ids_with_sim:
        row = doc_rows.get(doc_id)
        if not row:
            continue
        cid, clabel = cluster_map.get(doc_id, (None, None))
        out.append(DocumentOut(
            id=doc_id, source=row["source"], source_id=row["source_id"],
            title=row["title"] or "", url_or_path=row["url_or_path"],
            date_added=row["date_added"], fetch_status=row["fetch_status"],
            source_tags=source_tag_map.get(doc_id, []),
            overlay_tags=overlay_tag_map.get(doc_id, []),
            cluster_id=cid, cluster_label=clabel,
            similarity=sim,
        ))
    return out


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
        run_id = _active_run_id(con)

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

        docs_out = _batch_doc_rows_to_out(page, con, run_id)

        # ── Images ────────────────────────────────────────────────────────────
        images_out: list[ImageOut] = []
        if req.include_images:
            from pka.db.schema import image_tags as itags_tbl
            from pka.db.schema import images as images_tbl
            from pka.ingestion.image_pipeline import search_images_by_text

            img_hits = search_images_by_text(req.query, n=10)
            for h in img_hits:
                irow = fetchone_mapping(con.execute(
                    sa.select(images_tbl)
                    .where(images_tbl.c.clip_vector_id == h["vector_id"])
                ))
                if not irow:
                    continue
                itags = [r[0] for r in con.execute(
                    sa.select(itags_tbl.c.tag)
                    .where(itags_tbl.c.image_id == irow["id"])
                ).fetchall()]
                images_out.append(ImageOut(
                    id=irow["id"], path=irow["path"], filename=irow["filename"],
                    image_type=irow["image_type"], width=irow["width"],
                    height=irow["height"], description=irow["description"],
                    ocr_text=irow["ocr_text"], date_taken=irow["date_taken"],
                    tags=itags, similarity=1.0 - h["distance"],
                ))

    return SearchResponse(
        query=req.query, total=total,
        documents=docs_out, images=images_out,
    )
