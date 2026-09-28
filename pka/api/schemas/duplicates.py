"""Duplicate-document request/response models (``/duplicates``)."""

from pydantic import BaseModel

from pka.api.schemas.documents import LinkedCopy


class DuplicateLinkOut(BaseModel):
    id: int
    canonical_id: int
    duplicate_id: int
    match_key: str
    match_value: str | None = None
    score: float | None = None
    state: str
    decided_by: str | None = None
    created_at: int | None = None
    decided_at: int | None = None
    canonical: LinkedCopy
    duplicate: LinkedCopy


class LinkRequest(BaseModel):
    canonical_id: int
    duplicate_id: int


class DuplicateScanRequest(BaseModel):
    embeddings: bool = True
    threshold: float | None = None


class DuplicateScanResult(BaseModel):
    linked: int
    by_key: dict[str, int]
    proposed: int
