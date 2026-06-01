"""``/tags`` — list source and overlay tags with optional filter."""
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query

from pka.api.dependencies import get_engine
from pka.constants import Source, TagOrigin
from pka.db.schema import documents, overlay_tags, source_tags

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("")
async def list_tags(
    origin: str | None = Query(
        None,
        description="source | inferred | manual | llm | cluster_l1 | cluster_l2",
    ),
    sources: Annotated[list[Source] | None, Query()] = None,
    q: str | None = Query(None),
    limit: int = 100,
    engine=Depends(get_engine),
):
    source_vals = [str(s) for s in sources] if sources else None

    with engine.connect() as con:
        src_q = (
            sa.select(
                source_tags.c.tag_string.label("tag"),
                sa.literal("source").label("origin"),
                sa.func.count(source_tags.c.id).label("n"),
            )
            .select_from(source_tags)
            .group_by(source_tags.c.tag_string)
        )
        if source_vals:
            src_q = src_q.join(
                documents, source_tags.c.document_id == documents.c.id
            ).where(documents.c.source.in_(source_vals))
        if q:
            src_q = src_q.where(source_tags.c.tag_string.ilike(f"%{q}%"))

        ov_q = (
            sa.select(
                overlay_tags.c.tag.label("tag"),
                overlay_tags.c.origin.label("origin"),
                sa.func.count(overlay_tags.c.id).label("n"),
            )
            .select_from(overlay_tags)
            .group_by(overlay_tags.c.tag, overlay_tags.c.origin)
        )
        if source_vals:
            ov_q = ov_q.join(
                documents, overlay_tags.c.document_id == documents.c.id
            ).where(documents.c.source.in_(source_vals))
        if q:
            ov_q = ov_q.where(overlay_tags.c.tag.ilike(f"%{q}%"))

        rows: list[dict] = []
        if not origin or origin == "source":
            rows += [{"tag": r[0], "origin": r[1], "count": r[2]}
                     for r in con.execute(src_q).fetchall()]
        overlay_origins = {
            str(TagOrigin.INFERRED),
            str(TagOrigin.MANUAL),
            str(TagOrigin.LLM),
            str(TagOrigin.CLUSTER_L1),
            str(TagOrigin.CLUSTER_L2),
        }
        if not origin or origin in overlay_origins:
            rows += [{"tag": r[0], "origin": r[1], "count": r[2]}
                     for r in con.execute(ov_q).fetchall()
                     if not origin or r[1] == origin]

        rows.sort(key=lambda x: -x["count"])
        return rows[:limit]
