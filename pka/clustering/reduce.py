"""Steps 2-3: PCA reduction and the UMAP projections.

Split out of ``engine.py`` (planning/M1_CLUSTERING_ENGINE_SPLIT.md).
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)


def _run_pca(
    matrix: np.ndarray,
    n_components: int = 50,
) -> tuple[np.ndarray, float]:
    """Project to ``n_components`` dims. Returns (pca_matrix, variance_explained_sum)."""
    n_docs, n_features = matrix.shape
    n_comp = min(n_components, n_docs - 1, n_features)
    n_comp = max(2, n_comp)
    # Imported here, not at module scope: sklearn costs ~1s to import and the API
    # only ever reaches it through a clustering run (see planning audit P-2).
    from sklearn.decomposition import PCA

    log.info("Running PCA (n_components=%d)…", n_comp)
    reducer = PCA(n_components=n_comp, random_state=42)
    pca_matrix = reducer.fit_transform(matrix).astype(np.float32)
    var_sum = float(reducer.explained_variance_ratio_.sum())
    log.info("PCA done. Shape: %s  variance explained: %.1f%%", pca_matrix.shape, var_sum * 100)
    return pca_matrix, var_sum


# ── Step 3: supervised UMAP (viz only) ───────────────────────────────────────


def _run_supervised_umap(
    matrix: np.ndarray,
    labels: np.ndarray,
    n_neighbors: int = 15,
    min_dist: float = 0.1,
) -> np.ndarray:
    """2d supervised UMAP for scatter plot; ``labels`` may contain -1 (noise)."""
    try:
        import umap
    except ImportError as e:
        raise ImportError("umap-learn is required: pip install umap-learn") from e

    n_docs = len(matrix)
    nn = max(2, min(n_neighbors, max(2, n_docs - 1)))
    log.info("Running supervised UMAP 2d (n_neighbors=%d)…", nn)
    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=nn,
        min_dist=min_dist,
        metric="cosine",
        random_state=42,
        n_jobs=-1,
    )
    reduced_2d = reducer.fit_transform(matrix, y=labels)
    log.info("Supervised UMAP done.")
    return reduced_2d.astype(np.float32)


def _run_umap_legacy(
    matrix: np.ndarray,
    n_components_cluster: int = 5,
    n_neighbors: int = 15,
    min_dist: float = 0.1,
    *,
    compute_2d: bool = True,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Legacy UMAP path for ``cluster_space=legacy_umap``."""
    try:
        import umap
    except ImportError as e:
        raise ImportError("umap-learn is required: pip install umap-learn") from e

    n_docs = len(matrix)
    nn = max(2, min(n_neighbors, max(2, n_docs - 1)))
    n_comp = min(n_components_cluster, max(2, n_docs - 1))

    log.info("Running legacy UMAP (n_neighbors=%d, n_components=%d)…", nn, n_comp)

    reducer_nd = umap.UMAP(
        n_components=n_comp,
        n_neighbors=nn,
        min_dist=min_dist,
        metric="cosine",
        random_state=42,
        n_jobs=-1,
    )
    reduced_nd = reducer_nd.fit_transform(matrix)

    reduced_2d = None
    if compute_2d:
        reducer_2d = umap.UMAP(
            n_components=2,
            n_neighbors=nn,
            min_dist=min_dist,
            metric="cosine",
            random_state=42,
            n_jobs=-1,
        )
        reduced_2d = reducer_2d.fit_transform(matrix)

    return reduced_nd.astype(np.float32), (
        reduced_2d.astype(np.float32) if reduced_2d is not None else None
    )
