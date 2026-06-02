"""``/documents`` list and ``/documents/{id}`` detail + tag patch."""
import time
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query

from pka.api.active_run import fetch_active_run_id
from pka.api.dependencies import get_engine
from pka.api.document_serialize import document_detail
from pka.api.schemas.documents import (
    DocumentDetail,
    DocumentListItem,
    DocumentListResponse,
    TagPatchRequest,
)
from pka.constants import Source, TagOrigin
from pka.db.queries import list_documents as query_list_documents
from pka.db.schema import overlay_tags

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    sources: Annotated[list[Source] | None, Query()] = None,
    source_tags: Annotated[list[str] | None, Query()] = None,
    general_tags: Annotated[list[str] | None, Query()] = None,
    overlay_tags: Annotated[list[str] | None, Query()] = None,
    cluster_l1_tags: Annotated[list[str] | None, Query()] = None,
    cluster_l2_tags: Annotated[list[str] | None, Query()] = None,
    learned_tags: Annotated[list[str] | None, Query()] = None,
    wayback_only: bool = Query(default=False),
    limit: int = Query(default=48, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    source_vals = [str(s) for s in sources] if sources else None
    total, rows = query_list_documents(
        sources=source_vals,
        source_tags=source_tags,
        general_tags=general_tags,
        overlay_tags=overlay_tags,
        cluster_l1_tags=cluster_l1_tags,
        cluster_l2_tags=cluster_l2_tags,
        learned_tags=learned_tags,
        wayback_only=wayback_only,
        limit=limit,
        offset=offset,
    )
    return DocumentListResponse(
        total=total,
        documents=[DocumentListItem(**row) for row in rows],
    )


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        detail = document_detail(con, doc_id, run_id)
    if detail is None:
        raise HTTPException(404, detail="Document not found")
    return detail


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
