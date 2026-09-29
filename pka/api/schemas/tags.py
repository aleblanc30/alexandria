"""Tag folding request/response models (``/tags/aliases``, ``/tags/variants``)."""

from typing import Literal

from pydantic import BaseModel


class AliasSide(BaseModel):
    key: str
    label: str
    documents: int
    examples: list[str] = []


class TagAliasOut(BaseModel):
    id: int
    kind: str
    state: str
    score: float | None = None
    decided_by: str | None = None
    created_at: int | None = None
    decided_at: int | None = None
    alias: AliasSide
    canonical: AliasSide


class MergeRequest(BaseModel):
    alias: str
    canonical: str


class ScanRequest(BaseModel):
    kinds: list[Literal["semantic", "morphology", "initialism"]] = [
        "semantic",
        "morphology",
        "initialism",
    ]
    threshold: float | None = None


class ScanResult(BaseModel):
    tags: int
    proposed: int
    by_kind: dict[str, int]


class VariantGroup(BaseModel):
    origin: str
    tag: str
    variants: list[str]
    documents: int


class TagDeleteResult(BaseModel):
    tag: str
    origin: str
    spellings: list[str]
    documents: int
    archived_sessions: int = 0
