"""``/duplicates``: find, review and undo links between duplicate documents (DESIGN.md §3.9)."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from pka.api.schemas.duplicates import (
    DuplicateLinkOut,
    DuplicateScanRequest,
    DuplicateScanResult,
    LinkRequest,
)
from pka.db import duplicates
from pka.dedupe import describe, scan

router = APIRouter(prefix="/duplicates", tags=["duplicates"])


@router.get("", response_model=list[DuplicateLinkOut])
def list_links(
    state: Literal["candidate", "merged", "rejected"] | None = Query(None),
    limit: int = Query(default=200, ge=1, le=5000),
):
    """Links and proposals, with both documents' title, source and URL."""
    return describe(duplicates.list_links(state)[:limit])


@router.post("/scan", response_model=DuplicateScanResult)
def scan_duplicates(req: DuplicateScanRequest):
    """Link exact duplicates, and propose near ones for review when ``embeddings``."""
    return scan(embeddings=req.embeddings, threshold=req.threshold)


@router.post("", response_model=DuplicateLinkOut)
def link_documents(req: LinkRequest):
    """Link two documents by hand."""
    try:
        row = duplicates.link(req.canonical_id, req.duplicate_id)
    except duplicates.LinkError as exc:
        raise HTTPException(422, str(exc)) from exc
    return describe([row])[0]


@router.post("/{link_id}/accept", response_model=DuplicateLinkOut)
def accept_link(link_id: int):
    try:
        row = duplicates.accept(link_id)
    except KeyError as exc:
        raise HTTPException(404, "No such link") from exc
    except duplicates.LinkError as exc:
        raise HTTPException(422, str(exc)) from exc
    return describe([row])[0]


@router.post("/{link_id}/reject", status_code=204)
def reject_link(link_id: int):
    """Decline a proposal or undo a link; the pair is not proposed again."""
    if not duplicates.reject(link_id):
        raise HTTPException(404, "No such link")
