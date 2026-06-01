"""Cluster, run, diagnostics, and UMAP response models."""
from pydantic import BaseModel


class TagCandidateOut(BaseModel):
    tag: str
    source: str
    coverage: float
    doc_count: int


class ClusterOut(BaseModel):
    cluster_id: int
    label: str
    description: str | None
    run_id: int
    doc_count: int
    level: int = 1
    parent_cluster_id: int | None = None
    parent_label: str | None = None
    suggested_tag: str
    tag_candidates: list[TagCandidateOut]
    llm_error: str | None = None


class ClusterDetail(ClusterOut):
    top_tags: list[str]


class ApplyTagRequest(BaseModel):
    tag: str | None = None


class ApplyTagResult(BaseModel):
    cluster_id: int
    tag: str
    applied: int
    skipped: int


class ApplyAllTagsResult(BaseModel):
    clusters: list[ApplyTagResult]
    total_applied: int
    total_skipped: int


class RunOut(BaseModel):
    run_id: int
    timestamp: int
    algorithm: str
    parameters: dict
    accepted: bool
    status: str
    n_clusters: int
    n_noise: int
    notes: str | None


class DiagnosticsOut(BaseModel):
    run_id: int
    n_clusters: int
    n_noise: int
    cluster_sizes: dict[str, int]
    drift_flags: list[dict]
    merge_suggestions: list[dict]


class UmapPoint(BaseModel):
    doc_id: int
    x: float
    y: float
    cluster_id: int | None
    title: str
