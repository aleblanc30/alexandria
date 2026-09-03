"""Step 4: HDBSCAN clustering and its adaptive parameter heuristic.

Split out of ``engine.py`` (planning/M1_CLUSTERING_ENGINE_SPLIT.md). A pure-numpy
sibling of ``agglomerative.py``; neither imports the other.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

_ADAPTIVE_MIN_CLUSTER_SIZE = 3
_ADAPTIVE_MAX_CLUSTER_SIZE = 50


def adaptive_cluster_params(n_docs: int) -> tuple[int, int, int]:
    """Derive HDBSCAN/UMAP params that target moderately sized clusters.

    ``min_cluster_size`` scales with ``sqrt(n_docs)`` but is capped at
    ``_ADAPTIVE_MAX_CLUSTER_SIZE`` rather than left to grow with the corpus. An
    earlier version derived it from a *target cluster count* capped at 12, which
    made ``min_cluster_size`` scale roughly linearly with ``n_docs`` instead —
    744 (with ``min_samples=372``) on an 18k-document archive, dense enough that
    HDBSCAN called ~83% of it noise. See planning/TODO.md's
    "adaptive_cluster_params manufactures the clustering noise" entry.
    """
    if n_docs < 8:
        return max(2, n_docs // 3), 2, max(2, n_docs - 1)

    min_cluster_size = min(
        _ADAPTIVE_MAX_CLUSTER_SIZE, max(_ADAPTIVE_MIN_CLUSTER_SIZE, round(n_docs**0.5 / 2))
    )
    min_samples = max(2, min_cluster_size // 2)
    n_neighbors = max(5, min(30, n_docs // 4))
    return min_cluster_size, min_samples, n_neighbors


def _normalize_for_cosine(matrix: np.ndarray) -> np.ndarray:
    from sklearn.preprocessing import normalize

    return normalize(matrix, norm="l2", axis=1).astype(np.float32)


def _run_hdbscan(
    reduced: np.ndarray,
    min_cluster_size: int = 5,
    min_samples: int | None = None,
    *,
    metric: str = "cosine",
) -> np.ndarray:
    """HDBSCAN clustering.

    When ``metric="cosine"``, rows are L2-normalized and euclidean distance is used
    (equivalent to cosine on unit vectors; supported by both hdbscan and sklearn).
    """
    if metric == "cosine":
        data = _normalize_for_cosine(reduced)
    else:
        data = reduced.astype(np.float32, copy=False)

    hdb_kwargs = dict(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_method="leaf",
    )

    try:
        import hdbscan as hdbscan_lib

        clusterer = hdbscan_lib.HDBSCAN(prediction_data=True, **hdb_kwargs)
        labels = clusterer.fit_predict(data)
        backend = "hdbscan pkg"
    except ImportError:
        from sklearn.cluster import HDBSCAN as SkHDBSCAN

        sk_kwargs = dict(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples or 1,
        )
        try:
            clusterer = SkHDBSCAN(cluster_selection_method="leaf", metric="euclidean", **sk_kwargs)
        except TypeError:
            clusterer = SkHDBSCAN(**sk_kwargs)
        labels = clusterer.fit_predict(data)
        backend = "sklearn"

    log.info(
        "HDBSCAN (%s): %d clusters, %d noise",
        backend,
        len(set(labels)) - (1 if -1 in labels else 0),
        (labels == -1).sum(),
    )
    return labels
