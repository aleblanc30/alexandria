"""``/images`` — list, search-by-text (CLIP), file, and detail view."""
import mimetypes
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from pka.api.db_rows import fetchall_mappings, fetchone_mapping
from pka.api.dependencies import get_engine
from pka.api.image_hits import clip_hits_to_image_out, image_row_to_out, image_tags_for
from pka.api.schemas.images import ImageOut
from pka.db.schema import images as images_tbl

router = APIRouter(prefix="/images", tags=["images"])


@router.get("", response_model=list[ImageOut])
async def list_images(
    image_type: str | None = Query(None),
    limit: int = 20,
    offset: int = 0,
    engine=Depends(get_engine),
):
    with engine.connect() as con:
        # Only surface fully-ingested images; a registered-but-not-yet-embedded
        # image has indexed_at IS NULL and is deferred until ingestion completes.
        q = sa.select(images_tbl).where(images_tbl.c.indexed_at.isnot(None))
        if image_type:
            q = q.where(images_tbl.c.image_type == image_type)
        rows = fetchall_mappings(con.execute(q.limit(limit).offset(offset)))
        out = [image_row_to_out(r, image_tags_for(con, r["id"])) for r in rows]
    return out


@router.get("/search", response_model=list[ImageOut])
async def search_images(
    q: str = Query(...),
    n: int = 10,
    engine=Depends(get_engine),
):
    from pka.ingestion.image_pipeline import search_images_by_text

    hits = search_images_by_text(q, n=n)
    with engine.connect() as con:
        return clip_hits_to_image_out(con, hits, round_similarity=True)


@router.get("/{image_id}/file")
async def get_image_file(image_id: int, engine=Depends(get_engine)):
    """Serve the raw image file so the frontend can render it in an ``<img>``."""
    with engine.connect() as con:
        row = fetchone_mapping(con.execute(
            sa.select(images_tbl.c.path).where(images_tbl.c.id == image_id)
        ))
    if not row or not row["path"]:
        raise HTTPException(404, "Image not found")

    file_path = Path(row["path"])
    if not file_path.is_file():
        raise HTTPException(404, "Image file missing")

    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type)


@router.get("/{image_id}", response_model=ImageOut)
async def get_image(image_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        row = fetchone_mapping(con.execute(
            sa.select(images_tbl).where(images_tbl.c.id == image_id)
        ))
        if not row:
            raise HTTPException(404, "Image not found")
        tags = image_tags_for(con, image_id)
    return image_row_to_out(row, tags)
