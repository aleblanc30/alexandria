"""
Clustering engine: PCA-reduced embeddings → clusterer → supervised UMAP viz → LLM labels.

Default pipeline (``cluster_space=pca``):
  1. Aggregate chunk embeddings per document (mean pooling; SQLite cache when available).
  2. PCA → 50d; hold ``pca_matrix`` for L1 and L2 HDBSCAN (cosine metric).
  3. L2 subclusters labelled via LLM from document title + content excerpts.
  4. L1 labels from L2 child labels when subclusters exist, else title + content.
  5. Supervised UMAP → 2d scatter (``y`` = L1 labels; noise = -1).

``cluster_space=agglomerative`` swaps step 2's clusterer for scipy hierarchical
(ward/average/complete/single) on the same PCA matrix: one linkage tree per run,
cut once for L1 and cut deeper (not rebuilt) inside each L1 group for L2. See
``planning/archive/AGGLOMERATIVE_CLUSTERING.md``. Unlike HDBSCAN it partitions every
document — no noise label.

Legacy ``cluster_space=legacy_umap`` retains the old UMAP→HDBSCAN path for comparison.
"""

from __future__ import annotations

import heapq
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
import sqlalchemy as sa
from scipy.cluster.hierarchy import fcluster, leaders
from scipy.cluster.hierarchy import linkage as scipy_linkage

from pka.clustering.doc_embeddings import embedding_to_blob
from pka.clustering.run_progress import ClusterRunCancelled, raise_if_cancelled
from pka.config import settings as cfg
from pka.db.queries import (
    get_engine,
    sample_cluster_documents,
    sample_cluster_documents_for_clusters,
)
from pka.db.schema import (
    cluster_assignments,
    cluster_runs,
    clusters,
)
from pka.json_utils import parse_llm_json as _parse_llm_json  # noqa: F401  (re-export for tests)

log = logging.getLogger(__name__)

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


# ── Data structures ───────────────────────────────────────────────────────────


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
class _StepTimer:
    timings_ms: dict[str, float] = field(default_factory=dict)

    def record(self, name: str, start: float) -> None:
        self.timings_ms[name] = round((time.perf_counter() - start) * 1000, 1)


# ── Step 1: aggregate document embeddings ────────────────────────────────────


def _mean_pool_from_chroma(
    vector_ids: list[str],
    metadatas: list[dict],
    embeddings: dict[str, list[float]],
    source_filter: list[str] | None,
) -> tuple[list[int], np.ndarray]:
    doc_vecs: dict[int, list[list[float]]] = {}
    for vid, meta in zip(vector_ids, metadatas, strict=False):
        emb = embeddings.get(vid)
        if emb is None:
            continue
        doc_id = int(meta.get("document_id", -1))
        if doc_id == -1:
            continue
        if source_filter and meta.get("source") not in source_filter:
            continue
        doc_vecs.setdefault(doc_id, []).append(emb)

    if not doc_vecs:
        raise ValueError("No embeddings found after filtering.")

    doc_ids = sorted(doc_vecs.keys())
    matrix = np.array(
        [np.mean(doc_vecs[d], axis=0) for d in doc_ids],
        dtype=np.float32,
    )
    return doc_ids, matrix


def _load_document_embeddings(
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> tuple[list[int], np.ndarray]:
    """Mean-pool chunk embeddings per document; prefer SQLite cache when present."""
    from pka.clustering.doc_embeddings import load_cached_embeddings, refresh_document_embedding
    from pka.storage.vector_store import (
        fetch_embeddings_by_ids,
        fetch_records,
    )

    meta_page = fetch_records(include=["metadatas"])
    vector_ids = meta_page["ids"]
    metadatas = meta_page["metadatas"]
    if not vector_ids:
        raise ValueError("Vector store is empty — run ingestion first.")

    if run_id is not None:
        raise_if_cancelled(run_id)

    # Collect doc ids from metadata (respecting source filter)
    candidate_doc_ids: set[int] = set()
    for meta in metadatas:
        doc_id = int(meta.get("document_id", -1))
        if doc_id == -1:
            continue
        if source_filter and meta.get("source") not in source_filter:
            continue
        candidate_doc_ids.add(doc_id)

    if not candidate_doc_ids:
        raise ValueError("No embeddings found after filtering.")

    sorted_ids = sorted(candidate_doc_ids)
    cached, missing = load_cached_embeddings(sorted_ids)

    if missing:
        log.info(
            "Loading %d vectors from Chroma (%d docs without cache)…",
            len(vector_ids),
            len(missing),
        )
        embeddings, corrupt_ids = fetch_embeddings_by_ids(vector_ids)
        if corrupt_ids:
            affected_docs = {
                int(metadatas[i].get("document_id", -1))
                for i, vid in enumerate(vector_ids)
                if vid in corrupt_ids and metadatas[i].get("document_id") is not None
            }
            log.warning(
                "Skipping %d unreadable Chroma vectors (%d documents)",
                len(corrupt_ids),
                len(affected_docs),
            )
        doc_ids_chroma, matrix_chroma = _mean_pool_from_chroma(
            vector_ids,
            metadatas,
            embeddings,
            source_filter,
        )
        chroma_map = dict(zip(doc_ids_chroma, matrix_chroma, strict=False))
        for did in missing:
            if did in chroma_map:
                cached[did] = chroma_map[did]
                refresh_document_embedding(did)
    else:
        log.info("Loaded cached embeddings for %d documents", len(cached))

    doc_ids = sorted(cached.keys())
    matrix = np.stack([cached[d] for d in doc_ids], axis=0)
    log.info("Aggregated embeddings for %d documents", len(doc_ids))
    return doc_ids, matrix


# ── Step 2: PCA reduction ─────────────────────────────────────────────────────


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


# ── Step 4: HDBSCAN clustering ────────────────────────────────────────────────


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


# ── Step 4b: agglomerative clustering (alternative to HDBSCAN) ────────────────


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


# ── Step 5: LLM cluster labelling ─────────────────────────────────────────────

DocSample = tuple[str, str]  # (title, excerpt)


def _format_doc_sample_lines(samples: list[DocSample]) -> str:
    lines: list[str] = []
    for title, excerpt in samples:
        if excerpt.strip():
            lines.append(f"- Title: {title}\n  Excerpt: {excerpt}")
        else:
            lines.append(f"- Title: {title}")
    return "\n".join(lines)


def _regenerate_prompt_suffix(previous_label: str | None) -> str:
    if not previous_label or not previous_label.strip():
        return "\nProvide a fresh topic label and description; avoid generic placeholders.\n"
    return (
        f'\nThe current label is "{previous_label.strip()}".\n'
        "Provide a fresh alternative label and description (not identical wording).\n"
    )


def _json_response_hint(topic_hint: str) -> str:
    return (
        "\nRespond with ONLY valid JSON in this exact format:\n"
        '{"label": "' + topic_hint + '", "description": "<one sentence>"}\n'
        "No explanation, no markdown, just the JSON object."
    )


def _label_via_chat(
    prompt: str,
    model: str | None,
    temperature: float | None,
    *,
    fallback,
    what: str,
) -> tuple[str, str]:
    """Run an LLM labelling prompt; fall back to a TF-IDF label on error."""
    from pka.ollama_chat import chat_json

    parsed, err = chat_json(prompt, model=model, temperature=temperature)
    if err:
        log.warning("LLM %s failed: %s — using fallback", what, err)
        return fallback(), ""
    return parsed.get("label", "Unlabelled"), parsed.get("description", "")


def _label_cluster_with_llm(
    samples: list[DocSample],
    model: str | None = None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    """Call Ollama for a short label and one-sentence description."""
    if not samples:
        return "Unlabelled", ""

    prompt = (
        "You are labelling a topic cluster from a research library.\n"
        "Below are sample documents from the cluster (title and excerpt when available).\n"
        "Use both title and excerpt when present.\n\n"
        + _format_doc_sample_lines(samples)
        + (_regenerate_prompt_suffix(previous_label) if previous_label is not None else "")
        + _json_response_hint("<3-5 word topic name>")
    )
    return _label_via_chat(
        prompt,
        model,
        temperature,
        fallback=lambda: _tfidf_label(samples),
        what="labelling",
    )


def _label_parent_from_children_with_llm(
    child_labels: list[str],
    child_descriptions: list[str],
    model: str | None = None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    if not child_labels:
        return "Unlabelled", ""

    lines: list[str] = []
    for label, desc in zip(child_labels, child_descriptions, strict=False):
        if desc.strip():
            lines.append(f"- {label}: {desc}")
        else:
            lines.append(f"- {label}")

    prompt = (
        "You are naming a broad parent topic that groups several sub-clusters "
        "in a research library.\n"
        "Below are labels (and descriptions) of sub-clusters (do not rename them):\n\n"
        + "\n".join(lines)
        + (_regenerate_prompt_suffix(previous_label) if previous_label is not None else "")
        + _json_response_hint("<3-5 word broader topic name>")
    )
    return _label_via_chat(
        prompt,
        model,
        temperature,
        fallback=lambda: _tfidf_label_from_strings(child_labels),
        what="parent labelling",
    )


_TFIDF_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "in",
    "and",
    "to",
    "for",
    "with",
    "on",
    "is",
    "are",
    "by",
    "from",
    "at",
    "as",
    "that",
    "this",
    "its",
    "it",
}


def _tfidf_label_from_strings(texts: list[str], n_words: int = 4) -> str:
    from collections import Counter

    words: list[str] = []
    for t in texts:
        words.extend(re.findall(r"[a-z]{3,}", t.lower()))
    freq = Counter(w for w in words if w not in _TFIDF_STOPWORDS)
    top = [w for w, _ in freq.most_common(n_words)]
    return " / ".join(top) if top else "Unlabelled"


def _tfidf_label(samples: list[DocSample], n_words: int = 4) -> str:
    return _tfidf_label_from_strings(
        [f"{title} {excerpt}" for title, excerpt in samples],
        n_words,
    )


def _label_one_cluster(
    cid: int,
    samples: list[DocSample],
    skip_labelling: bool,
    chat_model: str | None,
) -> tuple[int, str, str]:
    if skip_labelling:
        return cid, _tfidf_label(samples), ""
    label, desc = _label_cluster_with_llm(samples, chat_model)
    return cid, label, desc


def _label_clusters(
    cluster_docs: dict[int, list[int]],
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[dict[int, str], dict[int, str]]:
    """Label clusters from document title + content samples (L2 subclusters)."""
    if not cluster_docs:
        return {}, {}

    eng = get_engine()
    with eng.connect() as con:
        samples_map = sample_cluster_documents_for_clusters(con, cluster_docs)

    label_map: dict[int, str] = {}
    desc_map: dict[int, str] = {}
    workers = 1 if skip_labelling else max(1, cfg.cluster_label_workers)
    cids = sorted(cluster_docs.keys())

    if workers == 1:
        for cid in cids:
            if run_id is not None:
                raise_if_cancelled(run_id)
            cid, label, desc = _label_one_cluster(
                cid,
                samples_map[cid],
                skip_labelling,
                chat_model,
            )
            label_map[cid] = label
            desc_map[cid] = desc
        return label_map, desc_map

    # Not a context manager: on cancellation we shut down without waiting for
    # already-dispatched futures, so a stop request isn't stuck behind a
    # ThreadPoolExecutor.__exit__() that blocks on shutdown(wait=True).
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(
                _label_one_cluster,
                cid,
                samples_map[cid],
                skip_labelling,
                chat_model,
            ): cid
            for cid in cids
        }
        for fut in as_completed(futures):
            if run_id is not None:
                raise_if_cancelled(run_id)
            cid, label, desc = fut.result()
            label_map[cid] = label
            desc_map[cid] = desc
            log.debug("Cluster %d → %s", cid, label)
    except ClusterRunCancelled:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return label_map, desc_map


def _label_l1_clusters(
    l1_cluster_docs: dict[int, list[int]],
    l2_batches: list[L2ClusterBatch],
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[dict[int, str], dict[int, str]]:
    """L1 labels from L2 children when available, else title+content on L1 docs."""
    batch_by_parent = {b.parent_l1_id: b for b in l2_batches}
    label_map: dict[int, str] = {}
    desc_map: dict[int, str] = {}
    eng = get_engine()

    with eng.connect() as con:
        fallback_samples = sample_cluster_documents_for_clusters(con, l1_cluster_docs)

    for l1_cid in sorted(l1_cluster_docs.keys()):
        if run_id is not None:
            raise_if_cancelled(run_id)
        batch = batch_by_parent.get(l1_cid)
        if batch and batch.label_map:
            child_labels = [batch.label_map[c] for c in sorted(batch.label_map)]
            child_descs = [batch.desc_map.get(c, "") for c in sorted(batch.label_map)]
            if skip_labelling:
                label_map[l1_cid] = _tfidf_label_from_strings(child_labels)
                desc_map[l1_cid] = ""
            else:
                label, desc = _label_parent_from_children_with_llm(
                    child_labels,
                    child_descs,
                    chat_model,
                )
                label_map[l1_cid] = label
                desc_map[l1_cid] = desc
        else:
            samples = fallback_samples.get(l1_cid, [])
            if skip_labelling:
                label_map[l1_cid] = _tfidf_label(samples)
                desc_map[l1_cid] = ""
            else:
                label, desc = _label_cluster_with_llm(samples, chat_model)
                label_map[l1_cid] = label
                desc_map[l1_cid] = desc
    return label_map, desc_map


def _label_cluster_from_docs(
    doc_ids: list[int],
    skip_labelling: bool,
    chat_model: str | None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    eng = get_engine()
    with eng.connect() as con:
        samples = sample_cluster_documents(con, doc_ids)
    if skip_labelling:
        return _tfidf_label(samples), ""
    return _label_cluster_with_llm(
        samples,
        chat_model,
        temperature=temperature,
        previous_label=previous_label,
    )


def _label_l1_db_cluster_from_children(
    con,
    cluster_id: int,
    run_id: int,
    chat_model: str | None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    child_rows = con.execute(
        sa.select(clusters.c.label, clusters.c.description).where(
            (clusters.c.parent_cluster_id == cluster_id)
            & (clusters.c.run_id == run_id)
            & (clusters.c.level == 2)
        )
    ).fetchall()
    if len(child_rows) >= 2:
        child_labels = [r[0] or "" for r in child_rows]
        child_descs = [r[1] or "" for r in child_rows]
        return _label_parent_from_children_with_llm(
            child_labels,
            child_descs,
            chat_model,
            temperature=temperature,
            previous_label=previous_label,
        )
    return "", ""


def relabel_single_cluster(
    cluster_id: int,
    run_id: int,
    *,
    label_model: str | None = None,
    skip_labelling: bool = False,
) -> tuple[str, str]:
    """Re-run labelling for one persisted cluster; returns (label, description).

    Uses higher Ollama temperature and asks for a fresh label. L2 subcluster
    names in the database are never changed when regenerating an L1 parent.
    """
    from pka.ollama_chat import resolve_chat_model

    chat_model = resolve_chat_model(label_model)
    regen_temp = cfg.cluster_regenerate_temperature
    eng = get_engine()

    with eng.connect() as con:
        row = con.execute(
            sa.select(clusters.c.level, clusters.c.label, clusters.c.is_noise).where(
                (clusters.c.cluster_id == cluster_id) & (clusters.c.run_id == run_id)
            )
        ).fetchone()
        if not row:
            raise ValueError(f"Cluster {cluster_id} not found in run {run_id}")
        if row[2]:
            raise ValueError(f"Cluster {cluster_id} is the noise bucket and is not labelled")

        level = int(row[0] or 1)
        previous_label = row[1] or ""
        doc_ids = [
            r[0]
            for r in con.execute(
                sa.select(cluster_assignments.c.document_id).where(
                    (cluster_assignments.c.cluster_id == cluster_id)
                    & (cluster_assignments.c.run_id == run_id)
                    & (cluster_assignments.c.level == level)
                )
            ).fetchall()
        ]

        label, desc = "", ""
        if level == 2:
            label, desc = _label_cluster_from_docs(
                doc_ids,
                skip_labelling,
                chat_model,
                temperature=regen_temp,
                previous_label=previous_label,
            )
        else:
            label, desc = _label_l1_db_cluster_from_children(
                con,
                cluster_id,
                run_id,
                chat_model,
                temperature=regen_temp,
                previous_label=previous_label,
            )
            if not label:
                label, desc = _label_cluster_from_docs(
                    doc_ids,
                    skip_labelling,
                    chat_model,
                    temperature=regen_temp,
                    previous_label=previous_label,
                )

    now = int(time.time())
    with eng.begin() as con:
        con.execute(
            clusters.update()
            .where(clusters.c.cluster_id == cluster_id)
            .values(label=label, description=desc, created_at=now)
        )
    log.info("Relabelled cluster #%d → %s", cluster_id, label)
    return label, desc


def relabel_run_clusters(
    run_id: int,
    label_model: str | None = None,
) -> None:
    """Replace placeholder labels with LLM labels (L2 first, then L1 from children)."""
    from pka.ollama_chat import resolve_chat_model

    eng = get_engine()
    chat_model = resolve_chat_model(label_model)
    now = int(time.time())

    updates: list[tuple[int, str, str]] = []

    with eng.connect() as con:
        l2_rows = con.execute(
            sa.select(clusters.c.cluster_id).where(
                (clusters.c.run_id == run_id) & (clusters.c.level == 2)
            )
        ).fetchall()
        for (cid,) in l2_rows:
            doc_ids = [
                r[0]
                for r in con.execute(
                    sa.select(cluster_assignments.c.document_id).where(
                        (cluster_assignments.c.cluster_id == cid)
                        & (cluster_assignments.c.run_id == run_id)
                        & (cluster_assignments.c.level == 2)
                    )
                ).fetchall()
            ]
            label, desc = _label_cluster_from_docs(doc_ids, False, chat_model)
            updates.append((cid, label, desc))

        l1_rows = con.execute(
            sa.select(clusters.c.cluster_id).where(
                (clusters.c.run_id == run_id)
                & (clusters.c.level == 1)
                # The noise bucket keeps its fixed label: it is not a topic, so
                # there is nothing for the LLM to summarise (and it is typically
                # the largest member set in the run).
                & (clusters.c.is_noise == False)  # noqa: E712 — SQLA expression
            )
        ).fetchall()
        for (cid,) in l1_rows:
            label, desc = _label_l1_db_cluster_from_children(con, cid, run_id, chat_model)
            if not label:
                doc_ids = [
                    r[0]
                    for r in con.execute(
                        sa.select(cluster_assignments.c.document_id).where(
                            (cluster_assignments.c.cluster_id == cid)
                            & (cluster_assignments.c.run_id == run_id)
                            & (cluster_assignments.c.level == 1)
                        )
                    ).fetchall()
                ]
                label, desc = _label_cluster_from_docs(doc_ids, False, chat_model)
            updates.append((cid, label, desc))

    with eng.begin() as con:
        for cid, label, desc in updates:
            con.execute(
                clusters.update()
                .where(clusters.c.cluster_id == cid)
                .values(label=label, description=desc, created_at=now)
            )

    log.info("Relabelled %d clusters for run #%d", len(updates), run_id)


# ── Step 6: persist to DB ─────────────────────────────────────────────────────


def create_run_placeholder(algorithm: str = ALGORITHM_PCA, parameters: dict | None = None) -> int:
    """Insert a run row immediately so the UI can show status=running.

    ``_finalize_run`` overwrites both fields once the run completes, so these
    only matter for what the "running" row displays in the meantime.
    """
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        res = con.execute(
            cluster_runs.insert().values(
                timestamp=now,
                algorithm=algorithm,
                parameters=json.dumps(parameters or {}),
                accepted=False,
                status="running",
            )
        )
        return res.inserted_primary_key[0]


def set_run_status(run_id: int, status: str, *, notes: str | None = None) -> None:
    values: dict = {"status": status}
    if notes is not None:
        values["notes"] = notes
    with get_engine().begin() as con:
        con.execute(cluster_runs.update().where(cluster_runs.c.run_id == run_id).values(**values))


def _cluster_centroid_blob(
    matrix: np.ndarray | None,
    doc_index: dict[int, int] | None,
    member_doc_ids: list[int],
) -> bytes | None:
    """Mean-pooled embedding of ``member_doc_ids``, or ``None`` when unavailable."""
    if matrix is None or doc_index is None:
        return None
    rows = [doc_index[d] for d in member_doc_ids if d in doc_index]
    if not rows:
        return None
    mean_vec = matrix[rows].mean(axis=0)
    return embedding_to_blob(mean_vec)


def _write_hierarchical_clusters(
    con,
    run_id: int,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    now: int,
    *,
    matrix: np.ndarray | None = None,
    doc_index: dict[int, int] | None = None,
) -> tuple[int, int, int]:
    """Persist L1/L2 clusters and assignments. Returns (n_l1, n_l2, n_assignments)."""

    def _insert_cluster(label, description, level, parent_cluster_id, centroid, is_noise=False):
        res = con.execute(
            clusters.insert().values(
                label=label,
                description=description,
                created_at=now,
                run_id=run_id,
                level=level,
                parent_cluster_id=parent_cluster_id,
                centroid=centroid,
                is_noise=is_noise,
            )
        )
        return res.inserted_primary_key[0]

    def _collect_assignments(rows, doc_ids_iter, raw_labels, db_ids, level):
        for doc_id, raw_label in zip(doc_ids_iter, raw_labels, strict=False):
            db_cid = db_ids.get(raw_label)
            if db_cid is None:
                continue
            rows.append(
                {
                    "document_id": doc_id,
                    "cluster_id": db_cid,
                    "run_id": run_id,
                    "score": None,
                    "assigned_at": now,
                    "level": level,
                }
            )

    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)
    l1_db_ids: dict[int, int] = {
        cid: _insert_cluster(
            l1_label_map.get(cid, f"Cluster {cid}"),
            l1_desc_map.get(cid, ""),
            1,
            None,
            _cluster_centroid_blob(matrix, doc_index, l1_cluster_docs.get(cid, [])),
        )
        for cid in l1_unique
    }

    # Noise (label -1) gets its own bucket rather than no row at all. Without
    # it those documents look "unassigned" to ``assign_new_docs``, which files
    # every one of them into the nearest real cluster on the next ingest — the
    # exact forcing DESIGN.md §4 says HDBSCAN exists to avoid. The bucket
    # carries no centroid, so it can never attract a document in turn.
    n_noise = int((l1_labels == -1).sum())
    if n_noise:
        l1_db_ids[-1] = _insert_cluster(
            NOISE_CLUSTER_LABEL,
            NOISE_CLUSTER_DESCRIPTION,
            1,
            None,
            None,
            is_noise=True,
        )
        log.info("Run #%d: %d noise document(s) held in the noise cluster", run_id, n_noise)

    assignment_rows: list[dict] = []
    _collect_assignments(assignment_rows, doc_ids, l1_labels.tolist(), l1_db_ids, 1)

    n_l2 = 0
    for batch in l2_batches:
        parent_db_id = l1_db_ids.get(batch.parent_l1_id)
        if parent_db_id is None:
            continue
        l2_unique = sorted(set(batch.labels.tolist()) - {-1})
        l2_cluster_docs = _build_cluster_docs(batch.doc_ids, batch.labels)
        l2_db_ids: dict[int, int] = {}
        for l2_cid in l2_unique:
            l2_db_ids[l2_cid] = _insert_cluster(
                batch.label_map.get(l2_cid, f"Subcluster {l2_cid}"),
                batch.desc_map.get(l2_cid, ""),
                2,
                parent_db_id,
                _cluster_centroid_blob(matrix, doc_index, l2_cluster_docs.get(l2_cid, [])),
            )
            n_l2 += 1

        _collect_assignments(
            assignment_rows,
            batch.doc_ids,
            batch.labels.tolist(),
            l2_db_ids,
            2,
        )

    if assignment_rows:
        con.execute(cluster_assignments.insert(), assignment_rows)

    return len(l1_unique), n_l2, len(assignment_rows)


def _run_level2_pass_core(
    doc_ids: list[int],
    l1_labels: np.ndarray,
    *,
    compute_l2_labels,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[list[L2ClusterBatch], int, int]:
    """Subcluster each L1 group; ``compute_l2_labels`` produces per-group L2 labels."""
    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    l2_batches: list[L2ClusterBatch] = []
    l2_noise = 0
    l2_skipped = 0

    for l1_cid in l1_unique:
        member_doc_ids = [doc_ids[i] for i, lbl in enumerate(l1_labels.tolist()) if lbl == l1_cid]
        n_sub = len(member_doc_ids)
        sub_mcs, sub_ms, sub_nn = adaptive_cluster_params(n_sub)
        if n_sub < sub_mcs:
            l2_skipped += 1
            continue

        if run_id is not None:
            raise_if_cancelled(run_id)

        l2_labels = compute_l2_labels(member_doc_ids, sub_mcs, sub_ms, sub_nn)
        l2_unique = sorted(set(l2_labels.tolist()) - {-1})
        l2_noise += int((l2_labels == -1).sum())

        if len(l2_unique) < 2:
            l2_skipped += 1
            continue

        l2_cluster_docs: dict[int, list[int]] = {c: [] for c in l2_unique}
        for doc_id, lbl in zip(member_doc_ids, l2_labels.tolist(), strict=False):
            if lbl != -1:
                l2_cluster_docs[lbl].append(doc_id)

        l2_label_map, l2_desc_map = _label_clusters(
            l2_cluster_docs,
            skip_labelling,
            chat_model,
            run_id,
        )
        l2_batches.append(
            L2ClusterBatch(
                parent_l1_id=l1_cid,
                doc_ids=member_doc_ids,
                labels=l2_labels,
                label_map=l2_label_map,
                desc_map=l2_desc_map,
            )
        )

    return l2_batches, l2_noise, l2_skipped


def _run_level2_pass(
    cluster_matrix: np.ndarray,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    *,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
    hdbscan_metric: str = "cosine",
) -> tuple[list[L2ClusterBatch], int, int]:
    """Run HDBSCAN inside each L1 cluster on ``cluster_matrix`` slices."""
    doc_id_to_idx = {d: i for i, d in enumerate(doc_ids)}

    def _compute(member_doc_ids, sub_mcs, sub_ms, sub_nn):
        sub_matrix = cluster_matrix[[doc_id_to_idx[d] for d in member_doc_ids]]
        return _run_hdbscan(sub_matrix, sub_mcs, sub_ms, metric=hdbscan_metric)

    return _run_level2_pass_core(
        doc_ids,
        l1_labels,
        compute_l2_labels=_compute,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )


def _run_level2_pass_legacy(
    matrix: np.ndarray,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    *,
    n_components: int,
    min_dist: float,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[list[L2ClusterBatch], int, int]:
    """Legacy L2: local UMAP + HDBSCAN per L1 cluster."""
    doc_id_to_idx = {d: i for i, d in enumerate(doc_ids)}

    def _compute(member_doc_ids, sub_mcs, sub_ms, sub_nn):
        sub_matrix = matrix[[doc_id_to_idx[d] for d in member_doc_ids]]
        sub_reduced_nd, _ = _run_umap_legacy(
            sub_matrix,
            n_components_cluster=min(n_components, max(2, len(member_doc_ids) - 1)),
            n_neighbors=sub_nn,
            min_dist=min_dist,
            compute_2d=False,
        )
        return _run_hdbscan(sub_reduced_nd, sub_mcs, sub_ms, metric="euclidean")

    return _run_level2_pass_core(
        doc_ids,
        l1_labels,
        compute_l2_labels=_compute,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )


def _run_level2_pass_agglomerative(
    Z: np.ndarray,
    data: np.ndarray,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    *,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[list[L2ClusterBatch], int, int]:
    """L2 by cutting the L1 tree deeper inside each group — no rebuild.

    An ``fcluster`` flat cluster is a contiguous dendrogram node, so splitting
    that node (``_split_node_auto``) reproduces exactly what re-running linkage
    on the group's slice would return — verified at ARI 1.0 across ward/average/
    complete. See planning/archive/AGGLOMERATIVE_CLUSTERING.md §2.4 for why this replaced
    the rebuild-per-group first draft, and for the one case (L2 reading as
    arbitrary slices of a coherent parent) where a local rebuild would still earn
    its cost.
    """
    n_leaves = len(doc_ids)
    doc_id_to_idx = {d: i for i, d in enumerate(doc_ids)}
    leader_nodes, leader_cids = leaders(Z, l1_labels)
    node_by_l1cid = dict(zip(leader_cids.tolist(), leader_nodes.tolist(), strict=False))

    def _compute(member_doc_ids, sub_mcs, sub_ms, sub_nn):
        # sub_ms / sub_nn are HDBSCAN-shaped knobs; agglomerative ignores them
        # and derives k from group size alone (§2.2c).
        l1_cid = int(l1_labels[doc_id_to_idx[member_doc_ids[0]]])
        node = node_by_l1cid[l1_cid]
        return _split_node_auto(Z, n_leaves, node, member_doc_ids, doc_id_to_idx, data)

    return _run_level2_pass_core(
        doc_ids,
        l1_labels,
        compute_l2_labels=_compute,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )


def _build_umap_records(
    doc_ids: list[int],
    l1_labels: np.ndarray,
    umap_2d: np.ndarray,
) -> list[dict]:
    return [
        {
            "doc_id": int(doc_ids[i]),
            "x": round(float(umap_2d[i, 0]), 5),
            "y": round(float(umap_2d[i, 1]), 5),
            "cluster_id": int(l1_labels[i]),
        }
        for i in range(len(doc_ids))
    ]


def _commit_run(
    write_run_row,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    umap_2d: np.ndarray,
    *,
    verb: str,
    matrix: np.ndarray | None = None,
) -> int:
    """Insert/update the run row (via ``write_run_row``) and write its clusters."""
    eng = get_engine()
    now = int(time.time())
    umap_records = _build_umap_records(doc_ids, l1_labels, umap_2d)
    doc_index = {did: i for i, did in enumerate(doc_ids)} if matrix is not None else None

    with eng.begin() as con:
        run_id = write_run_row(con, now, umap_records)
        n_l1, n_l2, n_assign = _write_hierarchical_clusters(
            con,
            run_id,
            doc_ids,
            l1_labels,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            now,
            matrix=matrix,
            doc_index=doc_index,
        )

    log.info(
        "%s run #%d (%d L1, %d L2 clusters, %d assignments, %d UMAP points)",
        verb,
        run_id,
        n_l1,
        n_l2,
        n_assign,
        len(umap_records),
    )
    return run_id


def _finalize_run(
    run_id: int,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    algorithm: str,
    params: dict,
    umap_2d: np.ndarray,
    *,
    matrix: np.ndarray | None = None,
) -> None:
    """Fill in a placeholder run row created at trigger time."""

    def _write(con, now, umap_records) -> int:
        con.execute(
            cluster_runs.update()
            .where(cluster_runs.c.run_id == run_id)
            .values(
                timestamp=now,
                algorithm=algorithm,
                parameters=json.dumps(params),
                accepted=False,
                status="finished",
                umap_points=json.dumps(umap_records),
            )
        )
        return run_id

    _commit_run(
        _write,
        doc_ids,
        l1_labels,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        umap_2d,
        verb="Finalized",
        matrix=matrix,
    )


def _persist_run(
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    algorithm: str,
    params: dict,
    umap_2d: np.ndarray,
    *,
    matrix: np.ndarray | None = None,
) -> int:
    """Write ``cluster_runs``, ``clusters``, and ``cluster_assignments``."""

    def _write(con, now, umap_records) -> int:
        res = con.execute(
            cluster_runs.insert().values(
                timestamp=now,
                algorithm=algorithm,
                parameters=json.dumps(params),
                accepted=False,
                status="finished",
                umap_points=json.dumps(umap_records),
            )
        )
        return res.inserted_primary_key[0]

    return _commit_run(
        _write,
        doc_ids,
        l1_labels,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        umap_2d,
        verb="Persisted",
        matrix=matrix,
    )


def _build_cluster_docs(
    doc_ids: list[int],
    labels: np.ndarray,
) -> dict[int, list[int]]:
    unique = sorted(set(labels.tolist()) - {-1})
    cluster_docs: dict[int, list[int]] = {c: [] for c in unique}
    for doc_id, lbl in zip(doc_ids, labels.tolist(), strict=False):
        if lbl != -1:
            cluster_docs[lbl].append(doc_id)
    return cluster_docs


def _run_pca_pipeline(
    doc_ids: list[int],
    matrix: np.ndarray,
    *,
    mcs: int,
    ms: int | None,
    nn: int,
    min_dist: float,
    pca_components: int,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
    timer: _StepTimer,
) -> tuple[
    np.ndarray,
    np.ndarray,
    dict[int, str],
    dict[int, str],
    list[L2ClusterBatch],
    int,
    int,
    int,
    float,
    dict,
]:
    """PCA → HDBSCAN L1/L2 → labels → supervised UMAP. Returns pipeline artifacts."""
    t0 = time.perf_counter()
    pca_matrix, var_sum = _run_pca(matrix, pca_components)
    timer.record("pca_ms", t0)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l1_labels = _run_hdbscan(pca_matrix, mcs, ms, metric="cosine")
    timer.record("hdbscan_l1_ms", t0)

    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    n_l1 = len(l1_unique)
    n_noise = int((l1_labels == -1).sum())
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l2_batches, l2_noise, l2_skipped = _run_level2_pass(
        pca_matrix,
        doc_ids,
        l1_labels,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
        hdbscan_metric="cosine",
    )
    timer.record("hdbscan_l2_ms", t0)

    t0 = time.perf_counter()
    l1_label_map, l1_desc_map = _label_l1_clusters(
        l1_cluster_docs,
        l2_batches,
        skip_labelling,
        chat_model,
        run_id,
    )
    timer.record("label_l1_ms", t0)

    t0 = time.perf_counter()
    reduced_2d = _run_supervised_umap(pca_matrix, l1_labels, nn, min_dist)
    timer.record("umap_viz_ms", t0)

    params = dict(
        cluster_space="pca",
        pca_components=pca_components,
        pca_variance=round(var_sum, 4),
        cluster_metric="cosine",
        viz="supervised_umap",
        min_cluster_size=mcs,
        min_samples=ms,
        n_neighbors=nn,
        min_dist=min_dist,
        cluster_selection="leaf",
        hierarchical=True,
        l2_skipped_parents=l2_skipped,
    )
    return (
        l1_labels,
        reduced_2d,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        n_l1,
        n_noise,
        l2_noise,
        var_sum,
        params,
    )


def _run_legacy_pipeline(
    doc_ids: list[int],
    matrix: np.ndarray,
    *,
    mcs: int,
    ms: int | None,
    nn: int,
    min_dist: float,
    n_components: int,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
    timer: _StepTimer,
) -> tuple[
    np.ndarray,
    np.ndarray,
    dict[int, str],
    dict[int, str],
    list[L2ClusterBatch],
    int,
    int,
    int,
    dict,
]:
    t0 = time.perf_counter()
    reduced_nd, reduced_2d = _run_umap_legacy(
        matrix,
        n_components_cluster=n_components,
        n_neighbors=nn,
        min_dist=min_dist,
    )
    timer.record("umap_ms", t0)
    assert reduced_2d is not None

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l1_labels = _run_hdbscan(reduced_nd, mcs, ms, metric="euclidean")
    timer.record("hdbscan_l1_ms", t0)

    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    n_l1 = len(l1_unique)
    n_noise = int((l1_labels == -1).sum())
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l2_batches, l2_noise, l2_skipped = _run_level2_pass_legacy(
        matrix,
        doc_ids,
        l1_labels,
        n_components=n_components,
        min_dist=min_dist,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )
    timer.record("hdbscan_l2_ms", t0)

    t0 = time.perf_counter()
    l1_label_map, l1_desc_map = _label_l1_clusters(
        l1_cluster_docs,
        l2_batches,
        skip_labelling,
        chat_model,
        run_id,
    )
    timer.record("label_l1_ms", t0)

    params = dict(
        cluster_space="legacy_umap",
        min_cluster_size=mcs,
        min_samples=ms,
        n_neighbors=nn,
        min_dist=min_dist,
        n_components=n_components,
        cluster_selection="leaf",
        hierarchical=True,
        l2_skipped_parents=l2_skipped,
    )
    return (
        l1_labels,
        reduced_2d,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        n_l1,
        n_noise,
        l2_noise,
        params,
    )


def _run_agglomerative_pipeline(
    doc_ids: list[int],
    matrix: np.ndarray,
    *,
    n_clusters: int | None,
    distance_threshold: float | None,
    linkage_method: str,
    pca_components: int,
    nn: int,
    min_dist: float,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
    timer: _StepTimer,
) -> tuple[
    np.ndarray,
    np.ndarray,
    dict[int, str],
    dict[int, str],
    list[L2ClusterBatch],
    int,
    int,
    int,
    float,
    dict,
]:
    """PCA → one linkage tree → cut for L1, cut deeper for L2 → labels → viz UMAP.

    See planning/archive/AGGLOMERATIVE_CLUSTERING.md. Mirrors ``_run_pca_pipeline``'s
    return shape so ``run_clustering`` needs only one more branch.
    """
    t0 = time.perf_counter()
    pca_matrix, var_sum = _run_pca(matrix, pca_components)
    timer.record("pca_ms", t0)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    Z, data = _build_linkage(pca_matrix, linkage_method=linkage_method, metric="cosine")
    sweep: dict[int, float] | None = None
    if n_clusters is None and distance_threshold is None:
        chosen_n_clusters, sweep = _auto_k_agglomerative(Z, data)
        l1_labels = _cut_linkage(Z, n_clusters=chosen_n_clusters)
    else:
        l1_labels = _cut_linkage(Z, n_clusters=n_clusters, distance_threshold=distance_threshold)
        chosen_n_clusters = n_clusters
    timer.record("agglomerative_l1_ms", t0)

    n_l1 = len(set(l1_labels.tolist()))
    n_noise = 0  # agglomerative partitions everything — see §3
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l2_batches, l2_noise, l2_skipped = _run_level2_pass_agglomerative(
        Z,
        data,
        doc_ids,
        l1_labels,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )
    timer.record("agglomerative_l2_ms", t0)

    t0 = time.perf_counter()
    l1_label_map, l1_desc_map = _label_l1_clusters(
        l1_cluster_docs,
        l2_batches,
        skip_labelling,
        chat_model,
        run_id,
    )
    timer.record("label_l1_ms", t0)

    t0 = time.perf_counter()
    reduced_2d = _run_supervised_umap(pca_matrix, l1_labels, nn, min_dist)
    timer.record("umap_viz_ms", t0)

    params = dict(
        cluster_space="agglomerative",
        linkage=linkage_method,
        n_clusters=chosen_n_clusters,
        distance_threshold=distance_threshold,
        pca_components=pca_components,
        pca_variance=round(var_sum, 4),
        cluster_metric="cosine",
        viz="supervised_umap",
        n_neighbors=nn,
        min_dist=min_dist,
        hierarchical=True,
        l2_skipped_parents=l2_skipped,
    )
    if sweep is not None:
        params["k_sweep"] = {str(k): round(v, 4) for k, v in sweep.items()}
    return (
        l1_labels,
        reduced_2d,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        n_l1,
        n_noise,
        l2_noise,
        var_sum,
        params,
    )


def _spawn_async_relabel(run_id: int, label_model: str | None) -> None:
    import threading

    def _worker() -> None:
        try:
            relabel_run_clusters(run_id, label_model=label_model)
        except Exception:
            log.exception("Async relabel failed for run #%d", run_id)

    threading.Thread(target=_worker, daemon=True, name=f"relabel-{run_id}").start()


# ── Public entry point ────────────────────────────────────────────────────────


def run_clustering(
    min_cluster_size: int | None = None,
    min_samples: int | None = None,
    n_neighbors: int | None = None,
    min_dist: float = 0.1,
    n_components: int = 5,
    pca_components: int | None = None,
    label_model: str | None = None,
    skip_labelling: bool = False,
    async_labelling: bool | None = None,
    cluster_space: str | None = None,
    linkage: str | None = None,
    n_clusters: int | None = None,
    distance_threshold: float | None = None,
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> ClusterRunResult:
    """Full clustering pipeline. Returns diagnostics for the UI acceptance panel."""
    from pka.ollama_chat import resolve_chat_model

    timer = _StepTimer()
    space = cluster_space or cfg.cluster_space
    pca_comp = pca_components if pca_components is not None else cfg.cluster_pca_components
    do_async = async_labelling if async_labelling is not None else cfg.cluster_async_labelling
    defer_llm = do_async and not skip_labelling

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    doc_ids, matrix = _load_document_embeddings(source_filter, run_id=run_id)
    timer.record("load_embeddings_ms", t0)
    n_docs = len(doc_ids)

    auto_mcs, auto_ms, auto_nn = adaptive_cluster_params(n_docs)
    mcs = min_cluster_size if min_cluster_size is not None else auto_mcs
    ms = min_samples if min_samples is not None else auto_ms
    nn = n_neighbors if n_neighbors is not None else auto_nn
    chat_model = resolve_chat_model(label_model)

    if space == "legacy_umap":
        algorithm = ALGORITHM_LEGACY
        (
            l1_labels,
            reduced_2d,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            n_l1,
            n_noise,
            l2_noise,
            params,
        ) = _run_legacy_pipeline(
            doc_ids,
            matrix,
            mcs=mcs,
            ms=ms,
            nn=nn,
            min_dist=min_dist,
            n_components=n_components,
            skip_labelling=skip_labelling or defer_llm,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        params["adaptive"] = min_cluster_size is None
    elif space == "agglomerative":
        algorithm = ALGORITHM_AGGLOMERATIVE
        (
            l1_labels,
            reduced_2d,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            n_l1,
            n_noise,
            l2_noise,
            _var,
            params,
        ) = _run_agglomerative_pipeline(
            doc_ids,
            matrix,
            n_clusters=n_clusters,
            distance_threshold=distance_threshold,
            linkage_method=linkage or cfg.cluster_linkage,
            pca_components=pca_comp,
            nn=nn,
            min_dist=min_dist,
            skip_labelling=skip_labelling or defer_llm,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        params["adaptive"] = n_clusters is None and distance_threshold is None
    else:
        algorithm = ALGORITHM_PCA
        (
            l1_labels,
            reduced_2d,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            n_l1,
            n_noise,
            l2_noise,
            _var,
            params,
        ) = _run_pca_pipeline(
            doc_ids,
            matrix,
            mcs=mcs,
            ms=ms,
            nn=nn,
            min_dist=min_dist,
            pca_components=pca_comp,
            skip_labelling=skip_labelling or defer_llm,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        params["adaptive"] = min_cluster_size is None

    n_l2 = sum(len(set(b.labels.tolist()) - {-1}) for b in l2_batches)
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)

    if run_id is not None:
        raise_if_cancelled(run_id)
        _finalize_run(
            run_id,
            doc_ids,
            l1_labels,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            algorithm=algorithm,
            params=params,
            umap_2d=reduced_2d,
            matrix=matrix,
        )
        persisted_id = run_id
    else:
        persisted_id = _persist_run(
            doc_ids,
            l1_labels,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            algorithm=algorithm,
            params=params,
            umap_2d=reduced_2d,
            matrix=matrix,
        )

    if defer_llm:
        _spawn_async_relabel(persisted_id, label_model)

    l1_sizes = {cid: len(l1_cluster_docs[cid]) for cid in sorted(l1_cluster_docs)}
    n_clusters_total = n_l1 + n_l2
    diagnostics = {
        "n_clusters": n_clusters_total,
        "n_l1_clusters": n_l1,
        "n_l2_clusters": n_l2,
        "n_noise": n_noise,
        "n_l2_noise": l2_noise,
        "l2_skipped_parents": params.get("l2_skipped_parents", 0),
        "cluster_sizes": l1_sizes,
        "size_min": min(l1_sizes.values()) if l1_sizes else 0,
        "size_max": max(l1_sizes.values()) if l1_sizes else 0,
        "size_mean": round(sum(l1_sizes.values()) / len(l1_sizes), 1) if l1_sizes else 0,
        "timings_ms": timer.timings_ms,
        "async_labelling": defer_llm,
    }

    return ClusterRunResult(
        run_id=persisted_id,
        n_clusters=n_clusters_total,
        n_noise=n_noise,
        cluster_labels=l1_label_map,
        cluster_descriptions=l1_desc_map,
        umap_2d=reduced_2d,
        doc_ids=doc_ids,
        assignments={did: int(lbl) for did, lbl in zip(doc_ids, l1_labels.tolist(), strict=False)},
        diagnostics=diagnostics,
    )
