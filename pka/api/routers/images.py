"""``/images`` — list, search-by-text, file, and detail view."""
import mimetypes
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from pka.api.db_rows import fetchall_mappings, fetchone_mapping
from pka.api.dependencies import get_engine
from pka.api.image_hits import (
    clip_hits_to_image_out,
    image_row_to_out,
    image_tags_for,
    inferred_hits_to_image_out,
    merge_image_hits,
)
from pka.api.schemas.images import ImageOut
from pka.db.schema import images as images_tbl

router = APIRouter(prefix="/images", tags=["images"])


@router.get("", response_model=list[ImageOut])
def list_images(
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
def search_images(
    q: str = Query(...),
    n: int = 10,
    mode: str = Query("hybrid", pattern="^(hybrid|clip|text)$"),
    engine=Depends(get_engine),
):
    """Search images by text over both paths (DESIGN.md §3.3).

    ``clip`` matches the query against the picture itself; ``text`` matches it
    against what the extraction passes read out of the picture. ``hybrid`` (the
    default) runs both and merges them — and is what keeps this endpoint useful
    with ``clip_enabled`` off, where the CLIP path yields nothing. Each result
    reports which path found it in ``matched_by``.
    """
    from pka.ingestion.image_pipeline import (
        search_images_by_inferred_text,
        search_images_by_text,
    )

    clip_hits = search_images_by_text(q, n=n) if mode in ("hybrid", "clip") else []
    text_hits = search_images_by_inferred_text(q, n=n) if mode in ("hybrid", "text") else []

    with engine.connect() as con:
        merged = merge_image_hits(
            clip_hits_to_image_out(con, clip_hits, round_similarity=True),
            inferred_hits_to_image_out(con, text_hits, round_similarity=True),
        )
    return merged[:n]


@router.get("/{image_id}/file")
def get_image_file(image_id: int, engine=Depends(get_engine)):
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
def get_image(image_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        row = fetchone_mapping(con.execute(
            sa.select(images_tbl).where(images_tbl.c.id == image_id)
        ))
        if not row:
            raise HTTPException(404, "Image not found")
        tags = image_tags_for(con, image_id)
    return image_row_to_out(row, tags)
