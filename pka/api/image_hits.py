"""Shared helpers to turn image rows / CLIP hits into :class:`ImageOut`."""
import sqlalchemy as sa

from pka.api.db_rows import fetchone_mapping
from pka.api.schemas.images import ImageOut
from pka.db.schema import image_tags
from pka.db.schema import images as images_tbl


def image_row_to_out(row, tags: list[str], similarity: float | None = None) -> ImageOut:
    return ImageOut(
        id=row["id"], path=row["path"], filename=row["filename"],
        image_type=row["image_type"], width=row["width"], height=row["height"],
        description=row["description"], ocr_text=row["ocr_text"],
        date_taken=row["date_taken"], tags=tags, similarity=similarity,
    )


def image_tags_for(con, image_id: int) -> list[str]:
    return [
        t[0] for t in con.execute(
            sa.select(image_tags.c.tag).where(image_tags.c.image_id == image_id)
        ).fetchall()
    ]


def clip_hits_to_image_out(con, hits, *, round_similarity: bool = False) -> list[ImageOut]:
    """Resolve CLIP search hits (``vector_id`` + ``distance``) to ``ImageOut`` rows."""
    out: list[ImageOut] = []
    for h in hits:
        row = fetchone_mapping(con.execute(
            sa.select(images_tbl).where(images_tbl.c.clip_vector_id == h["vector_id"])
        ))
        if not row:
            continue
        sim = 1.0 - h["distance"]
        if round_similarity:
            sim = round(sim, 3)
        out.append(image_row_to_out(row, image_tags_for(con, row["id"]), similarity=sim))
    return out
