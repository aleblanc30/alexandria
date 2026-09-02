"""Cluster, run, diagnostics, and UMAP response models."""

from pydantic import BaseModel, Field, model_validator


class ClusterOut(BaseModel):
    cluster_id: int
    label: str
    description: str | None
    run_id: int
    doc_count: int
    level: int = 1
    parent_cluster_id: int | None = None
    parent_label: str | None = None
    # The run's noise bucket: documents with no dense neighbourhood. Not a
    # topic, so the UI must not offer labelling or tagging for it.
    is_noise: bool = False


class ClusterDetail(ClusterOut):
    top_tags: list[str]


class ClusterPatchRequest(BaseModel):
    label: str = Field(..., min_length=1)
    description: str | None = None


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


class TriggerRunRequest(BaseModel):
    cluster_space: str | None = Field(None, pattern="^(pca|legacy_umap|agglomerative)$")
    min_cluster_size: int | None = Field(None, ge=2, le=1000)
    min_samples: int | None = Field(None, ge=1, le=1000)
    n_neighbors: int | None = Field(None, ge=2, le=200)
    min_dist: float = Field(0.1, ge=0.0, le=1.0)
    pca_components: int | None = Field(None, ge=2, le=500)
    n_components: int = Field(5, ge=2, le=50)
    linkage: str | None = Field(None, pattern="^(ward|average|complete|single)$")
    n_clusters: int | None = Field(None, ge=2, le=1000)
    distance_threshold: float | None = Field(None, gt=0)
    skip_labelling: bool = False
    async_labelling: bool = False

    @model_validator(mode="after")
    def _n_clusters_xor_distance_threshold(self) -> "TriggerRunRequest":
        if self.n_clusters is not None and self.distance_threshold is not None:
            raise ValueError("Set at most one of n_clusters and distance_threshold, not both.")
        return self


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
