"""``/tags`` — list source and overlay tags with optional filter, and fold them."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from pka.api.dependencies import get_engine
from pka.api.schemas.tags import (
    MergeRequest,
    ScanRequest,
    ScanResult,
    TagAliasOut,
    TagDeleteResult,
    VariantGroup,
)
from pka.constants import Source, TagOrigin
from pka.db import tag_aliases
from pka.db.tags import DELETABLE_ORIGINS, delete_overlay_tag
from pka.db.tags import list_tags as query_list_tags
from pka.tag_dedup import describe, scan, variant_report
from pka.tag_training import lifecycle

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("")
def list_tags(
    origin: str | None = Query(
        None,
        description="source | inferred | manual | llm | cluster_l1 | cluster_l2 | learned | collection",
    ),
    sources: Annotated[list[Source] | None, Query()] = None,
    source_tags: Annotated[list[str] | None, Query()] = None,
    cluster_l1_tags: Annotated[list[str] | None, Query()] = None,
    cluster_l2_tags: Annotated[list[str] | None, Query()] = None,
    collection_tags: Annotated[list[str] | None, Query()] = None,
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
        collection_tag_filter=collection_tags,
        wayback_only=wayback_only,
        q=q,
        limit=limit,
    )


@router.delete("", response_model=TagDeleteResult)
def delete_tag(
    tag: str = Query(..., min_length=1),
    origin: str = Query(..., description="manual | inferred | llm | learned"),
):
    """Remove a tag of one origin from every document that carries it.

    Only overlay origins that nothing regenerates can be deleted; a learned tag
    also archives its accepted model so the next ingested document does not
    bring it back.
    """
    if origin not in DELETABLE_ORIGINS:
        raise HTTPException(
            422,
            f"Tags of origin {origin!r} cannot be deleted; "
            f"deletable origins: {', '.join(sorted(DELETABLE_ORIGINS))}",
        )
    spellings, documents = delete_overlay_tag(tag, origin)
    if not spellings:
        raise HTTPException(404, "No such tag")
    archived = lifecycle.archive_accepted_sessions(spellings) if origin == TagOrigin.LEARNED else 0
    return TagDeleteResult(
        tag=tag,
        origin=origin,
        spellings=spellings,
        documents=documents,
        archived_sessions=archived,
    )


# ── Folding (DESIGN.md §3.8) ────────────────────────────────────────────────


@router.get("/aliases", response_model=list[TagAliasOut])
def list_aliases(
    state: Literal["candidate", "active", "rejected"] | None = Query(None),
    limit: int = Query(default=200, ge=1, le=2000),
):
    """Fold proposals and decisions, with counts and, for candidates, example titles."""
    return describe(tag_aliases.list_aliases(state)[:limit])


@router.post("/aliases", response_model=TagAliasOut)
def merge_tags(req: MergeRequest):
    """Fold tag ``alias`` into tag ``canonical`` (any stored spelling of either)."""
    try:
        row = tag_aliases.merge(req.alias, req.canonical)
    except tag_aliases.AliasError as exc:
        raise HTTPException(422, str(exc)) from exc
    return describe([row])[0]


@router.post("/aliases/scan", response_model=ScanResult)
def scan_aliases(req: ScanRequest):
    """Propose new candidates. Semantic proposals embed every compared tag locally."""
    return scan(tuple(req.kinds), threshold=req.threshold)


@router.post("/aliases/{alias_id}/accept", response_model=TagAliasOut)
def accept_alias(alias_id: int):
    try:
        row = tag_aliases.accept(alias_id)
    except KeyError as exc:
        raise HTTPException(404, "No such alias") from exc
    except tag_aliases.AliasError as exc:
        raise HTTPException(422, str(exc)) from exc
    return describe([row])[0]


@router.post("/aliases/{alias_id}/reject", status_code=204)
def reject_alias(alias_id: int):
    """Decline a candidate or undo a fold; the pair is not proposed again."""
    if not tag_aliases.reject(alias_id):
        raise HTTPException(404, "No such alias")


@router.get("/variants", response_model=list[VariantGroup])
def list_variants(limit: int = Query(default=200, ge=1, le=5000)):
    """Spellings the normalisation already folds, largest groups first."""
    return variant_report()[:limit]
