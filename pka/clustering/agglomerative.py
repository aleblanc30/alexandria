"""Step 4b: agglomerative clustering — an alternative to ``hdbscan_step``.

Split out of ``engine.py`` (planning/M1_CLUSTERING_ENGINE_SPLIT.md). One scipy
linkage tree per run, cut once for L1 and cut deeper (not rebuilt) for L2; see
planning/archive/AGGLOMERATIVE_CLUSTERING.md.
"""

from __future__ import annotations

import heapq
import logging

import numpy as np
from scipy.cluster.hierarchy import fcluster
from scipy.cluster.hierarchy import linkage as scipy_linkage

from pka.clustering.hdbscan_step import _normalize_for_cosine
from pka.clustering.types import _MONOTONE_LINKAGES

log = logging.getLogger(__name__)


def _build_linkage(
    reduced: np.ndarray,
    *,
    linkage_method: str = "ward",
    metric: str = "cosine",
) -> tuple[np.ndarray, np.ndarray]:
    """Build a scipy dendrogram once. Returns ``(Z, data)``.

    ``data`` is what ``Z`` was built on (L2-normalized when ``metric="cosine"``)
    and is reused for silhouette scoring — see ``_auto_k_agglomerative``.
    Restricted to monotone linkages: cutting by height (``_split_subtree``, the
    L2 tree-cut in ``_run_level2_pass_agglomerative``) is only valid when merge
    distances never decrease going up the tree, which ``centroid``/``median``
    do not guarantee.
    """
    if linkage_method not in _MONOTONE_LINKAGES:
        raise ValueError(f"linkage must be one of {_MONOTONE_LINKAGES}, got {linkage_method!r}")
    data = (
        _normalize_for_cosine(reduced)
        if metric == "cosine"
        else reduced.astype(np.float32, copy=False)
    )
    log.info("Building %s linkage tree (%d points)…", linkage_method, len(data))
    Z = scipy_linkage(data, method=linkage_method)
    return Z, data


def _cut_linkage(
    Z: np.ndarray,
    *,
    n_clusters: int | None = None,
    distance_threshold: float | None = None,
) -> np.ndarray:
    """Cut a prebuilt tree. Exactly one of the two stopping rules must be set.

    Returns 0-based labels (never -1) — ``fcluster`` is 1-based.
    """
    if (n_clusters is None) == (distance_threshold is None):
        raise ValueError("Exactly one of n_clusters or distance_threshold must be set.")
    if n_clusters is not None:
        return fcluster(Z, n_clusters, criterion="maxclust") - 1
    return fcluster(Z, distance_threshold, criterion="distance") - 1


def _run_agglomerative(
    reduced: np.ndarray,
    n_clusters: int | None = None,
    distance_threshold: float | None = None,
    *,
    linkage_method: str = "ward",
    metric: str = "cosine",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Agglomerative clustering. Returns ``(labels, Z, data)``.

    ``Z`` and ``data`` escape (unlike ``_run_hdbscan``'s labels-only contract)
    so the caller can cut the same tree again — for the auto-k sweep
    (``_auto_k_agglomerative``) and for L2 (``_run_level2_pass_agglomerative``)
    — instead of rebuilding it. Emits no ``-1``: every point is assigned.
    """
    Z, data = _build_linkage(reduced, linkage_method=linkage_method, metric=metric)
    labels = _cut_linkage(Z, n_clusters=n_clusters, distance_threshold=distance_threshold)
    log.info(
        "Agglomerative (%s, %s): %d clusters",
        linkage_method,
        metric,
        len(set(labels.tolist())),
    )
    return labels, Z, data


def _agglomerative_k_candidates(n_docs: int, k_min: int = 8, k_max: int = 40) -> list[int]:
    """Geometrically spaced candidate cluster counts, clamped to the corpus."""
    hi = min(k_max, max(k_min, n_docs - 1))
    if hi <= k_min:
        return [max(2, min(hi, n_docs - 1))]
    raw = np.geomspace(k_min, hi, num=8)
    candidates = sorted({int(round(x)) for x in raw})
    return [c for c in candidates if 2 <= c <= n_docs - 1] or [max(2, hi)]


def _pick_best_k(candidates: list[int], scores: dict[int, float]) -> int:
    """Highest silhouette score wins; a near-tie (within ``tol``) prefers the
    smaller, more browsable ``k`` (candidates are iterated ascending)."""
    best_k = candidates[0]
    best_score = float("-inf")
    tol = 1e-3
    for k in candidates:
        score = scores.get(k)
        if score is not None and score > best_score + tol:
            best_score = score
            best_k = k
    return best_k


def _auto_k_agglomerative(
    Z: np.ndarray,
    data: np.ndarray,
    *,
    k_min: int = 8,
    k_max: int = 40,
    sample_size: int = 3000,
    random_state: int = 42,
) -> tuple[int, dict[int, float]]:
    """Silhouette sweep over cuts of a prebuilt tree — cheap because cutting is
    ~ms once ``Z`` exists (see planning/archive/AGGLOMERATIVE_CLUSTERING.md §2.2c).
    Returns ``(best_k, {k: silhouette_score})`` — the sweep is recorded in
    ``params`` so a bad auto-pick is diagnosable rather than invisible.
    """
    from sklearn.metrics import silhouette_score

    n_docs = len(data)
    candidates = _agglomerative_k_candidates(n_docs, k_min, k_max)
    scores: dict[int, float] = {}
    for k in candidates:
        labels = _cut_linkage(Z, n_clusters=k)
        if len(set(labels.tolist())) < 2:
            continue
        try:
            scores[k] = float(
                silhouette_score(
                    data, labels, sample_size=min(sample_size, n_docs), random_state=random_state
                )
            )
        except ValueError:
            continue
    best_k = _pick_best_k(candidates, scores) if scores else candidates[0]
    return best_k, scores


def _subtree_leaves(Z: np.ndarray, n: int, node: int) -> list[int]:
    """Leaf (original observation) indices under dendrogram node id ``node``."""
    out: list[int] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur < n:
            out.append(cur)
        else:
            row = Z[cur - n]
            stack.append(int(row[0]))
            stack.append(int(row[1]))
    return out


def _split_subtree(Z: np.ndarray, n: int, node: int, k: int) -> list[list[int]]:
    """Split dendrogram ``node`` into ``k`` parts, by repeatedly opening the
    highest remaining merge — equivalent to ``fcluster(Z, k, "maxclust")``
    restricted to the subtree under ``node``, verified at ARI 1.0 against an
    independent rebuild (planning/archive/AGGLOMERATIVE_CLUSTERING.md §2.4). Each
    returned group is a list of leaf indices into the matrix ``Z`` was built on.
    """
    if node < n:
        return [[node]]
    heap: list[tuple[float, int]] = [(-Z[node - n, 2], node)]
    leaves: list[int] = []
    while heap and len(heap) + len(leaves) < k:
        _, cur = heapq.heappop(heap)
        row = Z[cur - n]
        for child in (int(row[0]), int(row[1])):
            if child < n:
                leaves.append(child)
            else:
                heapq.heappush(heap, (-Z[child - n, 2], child))
    groups = [_subtree_leaves(Z, n, c) for _, c in heap]
    groups.extend([leaf] for leaf in leaves)
    return groups


def _split_node_auto(
    Z: np.ndarray,
    n_leaves: int,
    node: int,
    member_doc_ids: list[int],
    doc_id_to_idx: dict[int, int],
    data: np.ndarray,
    *,
    k_min: int = 2,
    k_max: int = 12,
    sample_size: int = 3000,
    random_state: int = 42,
) -> np.ndarray:
    """L2 for one L1 group: cut ``node``'s subtree at the silhouette-best ``k``.

    Returns 0-based labels aligned to ``member_doc_ids``. No tree rebuild — see
    ``_split_subtree`` and planning/archive/AGGLOMERATIVE_CLUSTERING.md §2.4.
    """
    n_sub = len(member_doc_ids)
    candidates = _agglomerative_k_candidates(n_sub, k_min, k_max)
    global_idx_to_pos = {doc_id_to_idx[d]: i for i, d in enumerate(member_doc_ids)}
    sub_data = data[[doc_id_to_idx[d] for d in member_doc_ids]]

    labels_by_k: dict[int, np.ndarray] = {}
    for k in candidates:
        groups = _split_subtree(Z, n_leaves, node, k)
        labels = np.full(n_sub, -1, dtype=int)
        for gi, leaf_group in enumerate(groups):
            for leaf in leaf_group:
                pos = global_idx_to_pos.get(leaf)
                if pos is not None:
                    labels[pos] = gi
        labels_by_k[k] = labels

    from sklearn.metrics import silhouette_score

    scores: dict[int, float] = {}
    for k, labels in labels_by_k.items():
        if len(set(labels.tolist())) < 2:
            continue
        try:
            scores[k] = float(
                silhouette_score(
                    sub_data, labels, sample_size=min(sample_size, n_sub), random_state=random_state
                )
            )
        except ValueError:
            continue

    best_k = _pick_best_k(candidates, scores) if scores else candidates[0]
    return labels_by_k[best_k]
