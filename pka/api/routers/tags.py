"""``/tags`` — list source and overlay tags with optional filter."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from pka.api.dependencies import get_engine
from pka.constants import Source
from pka.db.queries import list_tags as query_list_tags

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("")
async def list_tags(
    origin: str | None = Query(
        None,
        description="source | inferred | manual | llm | cluster_l1 | cluster_l2",
    ),
    sources: Annotated[list[Source] | None, Query()] = None,
    source_tags: Annotated[list[str] | None, Query()] = None,
    cluster_l1_tags: Annotated[list[str] | None, Query()] = None,
    cluster_l2_tags: Annotated[list[str] | None, Query()] = None,
    wayback_only: bool = Query(default=False),
    q: str | None = Query(None),
    limit: int = 100,
    engine=Depends(get_engine),
):
    del engine  # query layer uses get_engine()
    source_vals = [str(s) for s in sources] if sources else None
    return query_list_tags(
        origin=origin,
        sources=source_vals,
        source_tag_filter=source_tags,
        cluster_l1_tag_filter=cluster_l1_tags,
        cluster_l2_tag_filter=cluster_l2_tags,
        wayback_only=wayback_only,
        q=q,
        limit=limit,
    )
