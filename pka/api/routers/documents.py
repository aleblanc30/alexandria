"""``/documents`` list and ``/documents/{id}`` detail + tag patch."""
import mimetypes
from pathlib import Path
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from pka.api.active_run import fetch_active_run_id
from pka.api.db_rows import fetchone_mapping
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
from pka.db.schema import documents as documents_tbl
from pka.db.schema import overlay_tags

router = APIRouter(prefix="/documents", tags=["documents"])

# Calibre stores a cover image alongside each book's format files.
_COVER_FILENAME = "cover.jpg"


@router.get("", response_model=DocumentListResponse)
def list_documents(
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
def get_document(doc_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        detail = document_detail(con, doc_id, run_id)
    if detail is None:
        raise HTTPException(404, detail="Document not found")
    return detail


@router.get("/{doc_id}/cover")
def get_document_cover(doc_id: int, engine=Depends(get_engine)):
    """Serve a document's cover image.

    Calibre books have a ``cover.jpg`` next to their format files; image
    documents *are* the image, so their ``url_or_path`` is streamed directly.
    """
    with engine.connect() as con:
        row = fetchone_mapping(con.execute(
            sa.select(documents_tbl.c.source, documents_tbl.c.url_or_path)
            .where(documents_tbl.c.id == doc_id)
        ))
    if not row or not row["url_or_path"]:
        raise HTTPException(404, detail="No cover available")

    if row["source"] == Source.IMAGE:
        image_path = Path(row["url_or_path"])
        if not image_path.is_file():
            raise HTTPException(404, detail="No cover available")
        media_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        return FileResponse(image_path, media_type=media_type)

    if row["source"] != Source.CALIBRE:
        raise HTTPException(404, detail="No cover available")

    cover_path = Path(row["url_or_path"]).parent / _COVER_FILENAME
    if not cover_path.is_file():
        raise HTTPException(404, detail="No cover available")
    return FileResponse(cover_path, media_type="image/jpeg")


@router.patch("/{doc_id}/tags", response_model=dict)
def patch_tags(doc_id: int, req: TagPatchRequest, engine=Depends(get_engine)):
    from pka.clustering.cluster_tags import insert_overlay_tags

    with engine.begin() as con:
        for tag in req.add:
            insert_overlay_tags(con, [doc_id], tag, TagOrigin.MANUAL)
        if req.remove:
            con.execute(
                overlay_tags.delete().where(
                    (overlay_tags.c.document_id == doc_id) &
                    overlay_tags.c.tag.in_(req.remove) &
                    (overlay_tags.c.origin == str(TagOrigin.MANUAL))
                )
            )
    return {"ok": True}
