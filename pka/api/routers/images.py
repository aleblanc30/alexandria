"""``/images`` — list, search-by-text (CLIP), and detail view."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query

from pka.api.db_rows import fetchall_mappings, fetchone_mapping
from pka.api.dependencies import get_engine
from pka.api.schemas.images import ImageOut
from pka.db.schema import image_tags
from pka.db.schema import images as images_tbl

router = APIRouter(prefix="/images", tags=["images"])


def _row_to_image_out(row, tags: list[str], similarity: float | None = None) -> ImageOut:
    return ImageOut(
        id=row["id"], path=row["path"], filename=row["filename"],
        image_type=row["image_type"], width=row["width"], height=row["height"],
        description=row["description"], ocr_text=row["ocr_text"],
        date_taken=row["date_taken"], tags=tags, similarity=similarity,
    )


@router.get("", response_model=list[ImageOut])
async def list_images(
    image_type: str | None = Query(None),
    limit: int = 20,
    offset: int = 0,
    engine=Depends(get_engine),
):
    with engine.connect() as con:
        q = sa.select(images_tbl)
        if image_type:
            q = q.where(images_tbl.c.image_type == image_type)
        rows = fetchall_mappings(con.execute(q.limit(limit).offset(offset)))
        out: list[ImageOut] = []
        for r in rows:
            tags = [t[0] for t in con.execute(
                sa.select(image_tags.c.tag)
                .where(image_tags.c.image_id == r["id"])
            ).fetchall()]
            out.append(_row_to_image_out(r, tags))
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
        out: list[ImageOut] = []
        for h in hits:
            row = fetchone_mapping(con.execute(
                sa.select(images_tbl)
                .where(images_tbl.c.clip_vector_id == h["vector_id"])
            ))
            if not row:
                continue
            tags = [t[0] for t in con.execute(
                sa.select(image_tags.c.tag)
                .where(image_tags.c.image_id == row["id"])
            ).fetchall()]
            out.append(_row_to_image_out(
                row, tags, similarity=round(1.0 - h["distance"], 3),
            ))
    return out


@router.get("/{image_id}", response_model=ImageOut)
async def get_image(image_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        row = fetchone_mapping(con.execute(
            sa.select(images_tbl).where(images_tbl.c.id == image_id)
        ))
        if not row:
            raise HTTPException(404, "Image not found")
        tags = [t[0] for t in con.execute(
            sa.select(image_tags.c.tag)
            .where(image_tags.c.image_id == image_id)
        ).fetchall()]
    return _row_to_image_out(row, tags)
