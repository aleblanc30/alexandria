"""Shared clustering data structures and constants.

Split out of ``engine.py`` (planning/M1_CLUSTERING_ENGINE_SPLIT.md): every other
``pka.clustering`` module imports from here, and this module imports nothing from
them, so there are no cycles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

ALGORITHM_PCA = "HDBSCAN-hierarchical-pca"
ALGORITHM_LEGACY = "HDBSCAN-hierarchical"
ALGORITHM_AGGLOMERATIVE = "agglomerative-hierarchical"

# The per-run noise bucket (``clusters.is_noise``). Documents HDBSCAN could not
# place land here instead of being left without a row; the label is shown in the
# UI, so it says what the bucket means rather than naming an algorithm.
NOISE_CLUSTER_LABEL = "Unclustered"
NOISE_CLUSTER_DESCRIPTION = (
    "Documents with no dense neighbourhood in this run. They are held here "
    "rather than filed into the nearest cluster, and are never tagged or "
    "relabelled. Re-cluster to give them another chance at a home."
)

_MONOTONE_LINKAGES = ("ward", "average", "complete", "single")


@dataclass
class L2ClusterBatch:
    """Level-2 clustering result scoped to one L1 HDBSCAN cluster."""

    parent_l1_id: int
    doc_ids: list[int]
    labels: np.ndarray
    label_map: dict[int, str]
    desc_map: dict[int, str]


@dataclass
class ClusterRunResult:
    run_id: int
    n_clusters: int
    n_noise: int
    cluster_labels: dict[int, str]
    cluster_descriptions: dict[int, str]
    umap_2d: np.ndarray  # shape (n_docs, 2)
    doc_ids: list[int]
    assignments: dict[int, int]  # {doc_id: L1 hdbscan_label} (-1 = noise)
    diagnostics: dict


@dataclass
class ClusterParams:
    """Tuning knobs for one clustering run.

    Per-call context (``source_filter``, ``run_id``) is deliberately *not* here —
    those are arguments to ``run_clustering``, not settings a caller would store
    or reuse. Field names and defaults are the single source of truth for
    ``TriggerRunRequest`` (``api/schemas/clusters.py``) and the CLI
    (``cli/clustering.py``).
    """

    min_cluster_size: int | None = None
    min_samples: int | None = None
    n_neighbors: int | None = None
    min_dist: float = 0.1
    n_components: int = 5
    #: ``None`` on any of the four below means "take the configured default"
    #: (``cfg.cluster_pca_components`` / ``cluster_async_labelling`` /
    #: ``cluster_space`` / ``cluster_linkage``), resolved inside
    #: ``run_clustering`` — not at construction time, so a stored ``ClusterParams``
    #: still follows config changes.
    pca_components: int | None = None
    label_model: str | None = None
    skip_labelling: bool = False
    async_labelling: bool | None = None
    cluster_space: str | None = None
    linkage: str | None = None
    n_clusters: int | None = None
    distance_threshold: float | None = None


@dataclass
class PipelineOutput:
    """What every ``_run_*_pipeline`` hands back to ``run_clustering``.

    Replaces the three differently-shaped positional tuples the pipelines used to
    return, which ``run_clustering`` destructured by branch.
    """

    labels: np.ndarray
    reduced_2d: np.ndarray
    label_map: dict[int, str]
    desc_map: dict[int, str]
    l2_batches: list[L2ClusterBatch]
    n_l1: int
    n_noise: int
    l2_noise: int
    extra_params: dict


@dataclass
class _StepTimer:
    timings_ms: dict[str, float] = field(default_factory=dict)

    def record(self, name: str, start: float) -> None:
        self.timings_ms[name] = round((time.perf_counter() - start) * 1000, 1)
