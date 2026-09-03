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

import logging
import time

import numpy as np
from scipy.cluster.hierarchy import leaders

from pka.clustering.agglomerative import (
    _auto_k_agglomerative,
    _build_linkage,
    _cut_linkage,
    _split_node_auto,
)
from pka.clustering.embeddings import _load_document_embeddings
from pka.clustering.hdbscan_step import _run_hdbscan, adaptive_cluster_params
from pka.clustering.labelling import _label_clusters, _label_l1_clusters, relabel_run_clusters
from pka.clustering.persist import _build_cluster_docs, _finalize_run, _persist_run
from pka.clustering.reduce import _run_pca, _run_supervised_umap, _run_umap_legacy
from pka.clustering.run_progress import raise_if_cancelled
from pka.clustering.types import (
    ALGORITHM_AGGLOMERATIVE,
    ALGORITHM_LEGACY,
    ALGORITHM_PCA,
    ClusterParams,
    ClusterRunResult,
    L2ClusterBatch,
    PipelineOutput,
    _StepTimer,
)
from pka.config import settings as cfg

log = logging.getLogger(__name__)


# -- Level-2 passes: subcluster inside each L1 group ---------------------------


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


# -- Pipelines: one per ``cluster_space`` --------------------------------------


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
) -> PipelineOutput:
    """PCA -> HDBSCAN L1/L2 -> labels -> supervised UMAP. Returns pipeline artifacts."""
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
    return PipelineOutput(
        labels=l1_labels,
        reduced_2d=reduced_2d,
        label_map=l1_label_map,
        desc_map=l1_desc_map,
        l2_batches=l2_batches,
        n_l1=n_l1,
        n_noise=n_noise,
        l2_noise=l2_noise,
        extra_params=params,
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
) -> PipelineOutput:
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
    return PipelineOutput(
        labels=l1_labels,
        reduced_2d=reduced_2d,
        label_map=l1_label_map,
        desc_map=l1_desc_map,
        l2_batches=l2_batches,
        n_l1=n_l1,
        n_noise=n_noise,
        l2_noise=l2_noise,
        extra_params=params,
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
) -> PipelineOutput:
    """PCA -> one linkage tree -> cut for L1, cut deeper for L2 -> labels -> viz UMAP.

    See planning/archive/AGGLOMERATIVE_CLUSTERING.md.
    """
    t0 = time.perf_counter()
    pca_matrix, var_sum = _run_pca(matrix, pca_components)
    timer.record("pca_ms", t0)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    Z, data = _build_linkage(pca_matrix, linkage_method=linkage_method, metric="cosine")
    sweep: dict[int, float] | None = None
    # ``None`` when the tree was cut by distance instead of by count — recorded
    # as-is in ``params`` so the run says which stopping rule it actually used.
    chosen_n_clusters: int | None
    if n_clusters is None and distance_threshold is None:
        chosen_n_clusters, sweep = _auto_k_agglomerative(Z, data)
        l1_labels = _cut_linkage(Z, n_clusters=chosen_n_clusters)
    else:
        l1_labels = _cut_linkage(Z, n_clusters=n_clusters, distance_threshold=distance_threshold)
        chosen_n_clusters = n_clusters
    timer.record("agglomerative_l1_ms", t0)

    n_l1 = len(set(l1_labels.tolist()))
    n_noise = 0  # agglomerative partitions everything - see AGGLOMERATIVE_CLUSTERING.md 3
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

    params: dict = dict(
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
    return PipelineOutput(
        labels=l1_labels,
        reduced_2d=reduced_2d,
        label_map=l1_label_map,
        desc_map=l1_desc_map,
        l2_batches=l2_batches,
        n_l1=n_l1,
        n_noise=n_noise,
        l2_noise=l2_noise,
        extra_params=params,
    )


def _spawn_async_relabel(run_id: int, label_model: str | None) -> None:
    import threading

    def _worker() -> None:
        try:
            relabel_run_clusters(run_id, label_model=label_model)
        except Exception:
            log.exception("Async relabel failed for run #%d", run_id)

    threading.Thread(target=_worker, daemon=True, name=f"relabel-{run_id}").start()


# -- Public entry point --------------------------------------------------------


def run_clustering(
    params: ClusterParams | None = None,
    *,
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> ClusterRunResult:
    """Full clustering pipeline. Returns diagnostics for the UI acceptance panel.

    ``params`` carries the tuning knobs (``ClusterParams``); ``source_filter`` and
    ``run_id`` are per-call context rather than settings, so they stay separate.
    """
    from pka.ollama_chat import resolve_chat_model

    p = params or ClusterParams()
    timer = _StepTimer()
    space = p.cluster_space or cfg.cluster_space
    pca_comp = p.pca_components if p.pca_components is not None else cfg.cluster_pca_components
    do_async = p.async_labelling if p.async_labelling is not None else cfg.cluster_async_labelling
    defer_llm = do_async and not p.skip_labelling

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    doc_ids, matrix = _load_document_embeddings(source_filter, run_id=run_id)
    timer.record("load_embeddings_ms", t0)
    n_docs = len(doc_ids)

    auto_mcs, auto_ms, auto_nn = adaptive_cluster_params(n_docs)
    mcs = p.min_cluster_size if p.min_cluster_size is not None else auto_mcs
    ms = p.min_samples if p.min_samples is not None else auto_ms
    nn = p.n_neighbors if p.n_neighbors is not None else auto_nn
    chat_model = resolve_chat_model(p.label_model)
    skip_or_defer = p.skip_labelling or defer_llm

    if space == "legacy_umap":
        algorithm = ALGORITHM_LEGACY
        out = _run_legacy_pipeline(
            doc_ids,
            matrix,
            mcs=mcs,
            ms=ms,
            nn=nn,
            min_dist=p.min_dist,
            n_components=p.n_components,
            skip_labelling=skip_or_defer,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        adaptive = p.min_cluster_size is None
    elif space == "agglomerative":
        algorithm = ALGORITHM_AGGLOMERATIVE
        out = _run_agglomerative_pipeline(
            doc_ids,
            matrix,
            n_clusters=p.n_clusters,
            distance_threshold=p.distance_threshold,
            linkage_method=p.linkage or cfg.cluster_linkage,
            pca_components=pca_comp,
            nn=nn,
            min_dist=p.min_dist,
            skip_labelling=skip_or_defer,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        adaptive = p.n_clusters is None and p.distance_threshold is None
    else:
        algorithm = ALGORITHM_PCA
        out = _run_pca_pipeline(
            doc_ids,
            matrix,
            mcs=mcs,
            ms=ms,
            nn=nn,
            min_dist=p.min_dist,
            pca_components=pca_comp,
            skip_labelling=skip_or_defer,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        adaptive = p.min_cluster_size is None

    run_params = out.extra_params
    run_params["adaptive"] = adaptive

    n_l2 = sum(len(set(b.labels.tolist()) - {-1}) for b in out.l2_batches)
    l1_cluster_docs = _build_cluster_docs(doc_ids, out.labels)

    if run_id is not None:
        raise_if_cancelled(run_id)
        _finalize_run(
            run_id,
            doc_ids,
            out.labels,
            out.label_map,
            out.desc_map,
            out.l2_batches,
            algorithm=algorithm,
            params=run_params,
            umap_2d=out.reduced_2d,
            matrix=matrix,
        )
        persisted_id = run_id
    else:
        persisted_id = _persist_run(
            doc_ids,
            out.labels,
            out.label_map,
            out.desc_map,
            out.l2_batches,
            algorithm=algorithm,
            params=run_params,
            umap_2d=out.reduced_2d,
            matrix=matrix,
        )

    if defer_llm:
        _spawn_async_relabel(persisted_id, p.label_model)

    l1_sizes = {cid: len(l1_cluster_docs[cid]) for cid in sorted(l1_cluster_docs)}
    n_clusters_total = out.n_l1 + n_l2
    diagnostics = {
        "n_clusters": n_clusters_total,
        "n_l1_clusters": out.n_l1,
        "n_l2_clusters": n_l2,
        "n_noise": out.n_noise,
        "n_l2_noise": out.l2_noise,
        "l2_skipped_parents": run_params.get("l2_skipped_parents", 0),
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
        n_noise=out.n_noise,
        cluster_labels=out.label_map,
        cluster_descriptions=out.desc_map,
        umap_2d=out.reduced_2d,
        doc_ids=doc_ids,
        assignments={did: int(lbl) for did, lbl in zip(doc_ids, out.labels.tolist(), strict=False)},
        diagnostics=diagnostics,
    )
