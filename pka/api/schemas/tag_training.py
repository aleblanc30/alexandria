"""Pydantic schemas for tag training API."""
from typing import Any

from pydantic import BaseModel, Field


class LabelIn(BaseModel):
    doc_id: int
    label: int = Field(ge=0, le=1)


class SessionCreate(BaseModel):
    tag: str
    labels: list[LabelIn]


class SessionFromSourceTag(BaseModel):
    source_tag: str
    target_tag: str


class LabelsBatch(BaseModel):
    labels: list[LabelIn]


class PseudoLabelRequest(BaseModel):
    mode: str = Field(description="model | llm")
    batch_size: int | None = Field(default=None, ge=1, le=200)


class PseudoLabelResultOut(BaseModel):
    mode: str
    added_positive: int
    added_negative: int
    pseudo_label_high: float | None = None
    pseudo_label_low: float | None = None
    errors: int | None = None
    batch_size: int | None = None


class SessionOut(BaseModel):
    session_id: int
    tag: str
    status: str
    created_at: int
    accepted_at: int | None = None
    parameters: dict[str, Any]
    provenance: dict[str, Any] | None = None
    positive_count: int
    negative_count: int
    has_model: bool
    train_stats: dict[str, Any] | None = None
    bootstrap_negatives_added: int | None = None
    pseudo_label_result: PseudoLabelResultOut | None = None


class QueueDocOut(BaseModel):
    doc_id: int
    title: str
    probability: float
    uncertainty: float
