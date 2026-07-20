"""Shared helpers to turn image rows / CLIP hits into :class:`ImageOut`."""
import sqlalchemy as sa

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


def _batch_image_tags(con, image_ids: list[int]) -> dict[int, list[str]]:
    tags_map: dict[int, list[str]] = {iid: [] for iid in image_ids}
    if image_ids:
        for image_id, tag in con.execute(
            sa.select(image_tags.c.image_id, image_tags.c.tag).where(
                image_tags.c.image_id.in_(image_ids)
            )
        ):
            tags_map[image_id].append(tag)
    return tags_map


def clip_hits_to_image_out(con, hits, *, round_similarity: bool = False) -> list[ImageOut]:
    """Resolve CLIP search hits (``vector_id`` + ``distance``) to ``ImageOut`` rows."""
    hits = list(hits)
    if not hits:
        return []
    vector_ids = [h["vector_id"] for h in hits]
    rows_by_vid = {
        row["clip_vector_id"]: row
        for row in con.execute(
            sa.select(images_tbl).where(images_tbl.c.clip_vector_id.in_(vector_ids))
        ).mappings()
    }
    tags_map = _batch_image_tags(con, [r["id"] for r in rows_by_vid.values()])

    out: list[ImageOut] = []
    for h in hits:
        row = rows_by_vid.get(h["vector_id"])
        if not row:
            continue
        sim = 1.0 - h["distance"]
        if round_similarity:
            sim = round(sim, 3)
        out.append(image_row_to_out(row, tags_map.get(row["id"], []), similarity=sim))
    return out
