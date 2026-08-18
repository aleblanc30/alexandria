"""Shared helpers to turn image rows / search hits into :class:`ImageOut`.

Two hit shapes reach here (DESIGN.md §3.3): CLIP hits, keyed by
``clip_vector_id``, and inferred-text hits, keyed by ``document_id``.
:func:`merge_image_hits` folds both into one ranked list.
"""
import sqlalchemy as sa

from pka.api.schemas.images import ImageOut
from pka.db.schema import image_tags
from pka.db.schema import images as images_tbl


def image_row_to_out(
    row,
    tags: list[str],
    similarity: float | None = None,
    matched_by: str | None = None,
) -> ImageOut:
    return ImageOut(
        id=row["id"], path=row["path"], filename=row["filename"],
        image_type=row["image_type"], width=row["width"], height=row["height"],
        description=row["description"], ocr_text=row["ocr_text"],
        date_taken=row["date_taken"], tags=tags, similarity=similarity,
        matched_by=matched_by,
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
        out.append(image_row_to_out(
            row, tags_map.get(row["id"], []), similarity=sim, matched_by="clip",
        ))
    return out


def inferred_hits_to_image_out(con, hits, *, round_similarity: bool = False) -> list[ImageOut]:
    """Resolve inferred-text hits (``document_id`` + ``distance``) to ``ImageOut`` rows.

    Chunk vectors are keyed by ``document_id``, so the join is through the
    image's backing document rather than ``clip_vector_id`` — which is exactly
    why this path still works with the visual index turned off. Images that were
    registered but not yet ingested (``indexed_at IS NULL``) are excluded, as
    they are everywhere else images are listed.
    """
    hits = list(hits)
    if not hits:
        return []
    doc_ids = [h["document_id"] for h in hits]
    rows_by_doc = {
        row["document_id"]: row
        for row in con.execute(
            sa.select(images_tbl).where(
                images_tbl.c.document_id.in_(doc_ids)
                & images_tbl.c.indexed_at.isnot(None)
            )
        ).mappings()
    }
    tags_map = _batch_image_tags(con, [r["id"] for r in rows_by_doc.values()])

    out: list[ImageOut] = []
    for h in hits:
        row = rows_by_doc.get(h["document_id"])
        if not row:
            continue
        sim = 1.0 - h["distance"]
        if round_similarity:
            sim = round(sim, 3)
        out.append(image_row_to_out(
            row, tags_map.get(row["id"], []), similarity=sim, matched_by="text",
        ))
    return out


def merge_image_hits(*hit_lists: list[ImageOut]) -> list[ImageOut]:
    """Merge per-path ``ImageOut`` lists into one, ranked by best similarity.

    An image found by both paths is kept once, at its higher score, and reports
    ``matched_by="clip+text"``. The two scores come from different embedding
    spaces (CLIP vs MiniLM), so comparing them is a deliberate approximation —
    the same one :mod:`pka.api.routers.search` already makes when folding CLIP
    hits into the unified result list. It decides ordering only; both paths
    return their own results either way.
    """
    best: dict[int, ImageOut] = {}
    for hits in hit_lists:
        for hit in hits:
            current = best.get(hit.id)
            if current is None:
                best[hit.id] = hit
                continue
            merged = current if (current.similarity or 0) >= (hit.similarity or 0) else hit
            if current.matched_by != hit.matched_by:
                merged = merged.model_copy(update={"matched_by": "clip+text"})
            best[hit.id] = merged
    return sorted(best.values(), key=lambda h: -(h.similarity or 0.0))
