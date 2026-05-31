"""``/documents`` list and ``/documents/{id}`` detail + tag patch."""
import time
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query

from pka.api.db_rows import fetchone_mapping
from pka.api.dependencies import get_engine
from pka.api.schemas.documents import (
    DocumentDetail,
    DocumentListItem,
    DocumentListResponse,
    TagPatchRequest,
)
from pka.constants import Source, TagOrigin
from pka.db.queries import list_documents as query_list_documents
from pka.db.schema import (
    chunks,
    cluster_assignments,
    cluster_runs,
    clusters,
    documents,
    overlay_tags,
    source_collections,
    source_tags,
)

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    sources: Annotated[list[Source] | None, Query()] = None,
    limit: int = Query(default=48, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    source_vals = [str(s) for s in sources] if sources else None
    total, rows = query_list_documents(
        sources=source_vals, limit=limit, offset=offset,
    )
    return DocumentListResponse(
        total=total,
        documents=[DocumentListItem(**row) for row in rows],
    )


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        row = fetchone_mapping(con.execute(
            sa.select(documents).where(documents.c.id == doc_id)
        ))
        if not row:
            raise HTTPException(404, detail="Document not found")

        run = con.execute(
            sa.select(cluster_runs.c.run_id)
            .where(cluster_runs.c.accepted == True)  # noqa: E712
            .order_by(cluster_runs.c.run_id.desc()).limit(1)
        ).fetchone()
        run_id = run[0] if run else None

        stags = [r[0] for r in con.execute(
            sa.select(source_tags.c.tag_string)
            .where(source_tags.c.document_id == doc_id)
        ).fetchall()]
        otags = [{"tag": r[0], "origin": r[1], "confidence": r[2]}
                 for r in con.execute(
            sa.select(overlay_tags.c.tag, overlay_tags.c.origin, overlay_tags.c.confidence)
            .where(overlay_tags.c.document_id == doc_id)
        ).fetchall()]
        colls = [r[0] for r in con.execute(
            sa.select(source_collections.c.collection)
            .where(source_collections.c.document_id == doc_id)
        ).fetchall()]
        n_chunks = con.execute(
            sa.select(sa.func.count()).select_from(chunks)
            .where(chunks.c.document_id == doc_id)
        ).scalar() or 0

        cluster_id = cluster_label = None
        if run_id:
            ca = con.execute(
                sa.select(cluster_assignments.c.cluster_id)
                .where((cluster_assignments.c.document_id == doc_id) &
                       (cluster_assignments.c.run_id == run_id))
            ).fetchone()
            if ca:
                cluster_id = ca[0]
                cl = con.execute(
                    sa.select(clusters.c.label)
                    .where(clusters.c.cluster_id == cluster_id)
                ).fetchone()
                cluster_label = cl[0] if cl else None

    return DocumentDetail(
        id=doc_id, source=row["source"], source_id=row["source_id"],
        title=row["title"] or "", url_or_path=row["url_or_path"],
        date_added=row["date_added"], fetch_status=row["fetch_status"],
        source_tags=stags, overlay_tags=otags,
        cluster_id=cluster_id, cluster_label=cluster_label,
        collections=colls, chunks_count=n_chunks,
    )


@router.patch("/{doc_id}/tags", response_model=dict)
async def patch_tags(doc_id: int, req: TagPatchRequest, engine=Depends(get_engine)):
    now = int(time.time())
    with engine.begin() as con:
        for tag in req.add:
            con.execute(sa.text("""
                INSERT OR IGNORE INTO overlay_tags
                    (document_id, tag, origin, created_at)
                VALUES (:did, :tag, :origin, :now)
            """), {
                "did": doc_id, "tag": tag,
                "origin": str(TagOrigin.MANUAL), "now": now,
            })
        for tag in req.remove:
            con.execute(
                overlay_tags.delete().where(
                    (overlay_tags.c.document_id == doc_id) &
                    (overlay_tags.c.tag == tag) &
                    (overlay_tags.c.origin == str(TagOrigin.MANUAL))
                )
            )
    return {"ok": True}
