"""Run the clustering pipeline and print diagnostics.

Usage::

    alexandria clustering
    alexandria clustering --accept          # auto-accept the run
    alexandria clustering --skip-labelling  # TF-IDF labels only
    alexandria clustering --async-labelling # TF-IDF first, LLM in background
    alexandria clustering --incremental     # assign new docs to the active run
    alexandria clustering --cluster-space legacy_umap
    alexandria clustering --cluster-space agglomerative --linkage ward
    alexandria clustering --cluster-space agglomerative --n-clusters 12
    alexandria clustering --min-cluster-size 10 --n-neighbors 20
    alexandria clustering --assign-new      # assign unassigned docs only
    alexandria clustering --drift           # print drift report
    alexandria clustering --merges          # print merge suggestions
"""

from __future__ import annotations

import argparse
import logging
import sys

from pka.cli._logging import setup_logging
from pka.clustering.engine import run_clustering
from pka.clustering.lifecycle import (
    accept_run,
    assign_new_docs,
    compute_drift,
    compute_merge_suggestions,
    get_active_run_id,
    run_incremental_clustering,
)
from pka.clustering.types import ClusterParams
from pka.db.queries import init_db

log = logging.getLogger("run_clustering")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria clustering")
    parser.add_argument("--min-cluster-size", type=int, default=None)
    parser.add_argument("--min-samples", type=int, default=None)
    parser.add_argument("--n-neighbors", type=int, default=None)
    parser.add_argument("--min-dist", type=float, default=0.1)
    parser.add_argument(
        "--n-components",
        type=int,
        default=5,
        help="Legacy UMAP clustering dims (cluster-space=legacy_umap)",
    )
    parser.add_argument("--pca-components", type=int, default=None)
    parser.add_argument(
        "--cluster-space",
        type=str,
        default=None,
        choices=["pca", "legacy_umap", "agglomerative"],
    )
    parser.add_argument(
        "--linkage",
        type=str,
        default=None,
        choices=["ward", "average", "complete", "single"],
        help="Agglomerative linkage method (cluster-space=agglomerative)",
    )
    k_group = parser.add_mutually_exclusive_group()
    k_group.add_argument(
        "--n-clusters",
        type=int,
        default=None,
        help="Explicit L1 cluster count (agglomerative); default is an auto silhouette sweep",
    )
    k_group.add_argument(
        "--distance-threshold",
        type=float,
        default=None,
        help="Cut the agglomerative tree at this distance instead of a fixed count",
    )
    parser.add_argument("--label-model", type=str, default=None)
    parser.add_argument("--skip-labelling", action="store_true")
    parser.add_argument(
        "--async-labelling",
        action="store_true",
        help="Persist TF-IDF labels first; LLM relabel in background",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Assign new docs to the active run (reports drift, never re-clusters)",
    )
    parser.add_argument(
        "--accept", action="store_true", help="Auto-accept this run without manual review"
    )
    parser.add_argument(
        "--assign-new",
        action="store_true",
        help="Assign unassigned docs to existing clusters (no re-run)",
    )
    parser.add_argument(
        "--drift", action="store_true", help="Print drift report for the active run"
    )
    parser.add_argument(
        "--merges", action="store_true", help="Print merge suggestions for the active run"
    )
    args = parser.parse_args(argv)

    setup_logging()
    init_db()

    # ── Lifecycle-only modes ──────────────────────────────────────────────────
    if args.assign_new:
        stats = assign_new_docs()
        log.info("Assigned %d new documents.", stats["assigned"])
        return 0

    if args.drift:
        active = get_active_run_id()
        if active is None:
            log.error("No active run. Run clustering first.")
            sys.exit(1)
        for entry in compute_drift(active):
            flag = " ← SPLIT?" if entry["flagged"] else ""
            log.info(
                "  [%s] drift=%.3f  recent=%d%s",
                entry["label"],
                entry["drift_score"],
                entry["n_recent"],
                flag,
            )
        return 0

    if args.merges:
        active = get_active_run_id()
        if active is None:
            log.error("No active run. Run clustering first.")
            sys.exit(1)
        for s in compute_merge_suggestions(active):
            log.info("  MERGE? '%s' + '%s'  sim=%.3f", s["label_a"], s["label_b"], s["similarity"])
        return 0

    params = ClusterParams(
        min_cluster_size=args.min_cluster_size,
        min_samples=args.min_samples,
        n_neighbors=args.n_neighbors,
        min_dist=args.min_dist,
        n_components=args.n_components,
        pca_components=args.pca_components,
        cluster_space=args.cluster_space,
        linkage=args.linkage,
        n_clusters=args.n_clusters,
        distance_threshold=args.distance_threshold,
        label_model=args.label_model,
        skip_labelling=args.skip_labelling,
        # argparse gives False for an unset store_true flag; ``None`` is what
        # means "use the configured default", so don't force it off.
        async_labelling=args.async_labelling or None,
    )

    if args.incremental:
        log.info("Starting incremental clustering update…")
        summary = run_incremental_clustering(params)
        log.info(
            "Incremental result: action=%s run_id=%s assigned=%s flagged=%s",
            summary["action"],
            summary["run_id"],
            summary["assigned"],
            summary["flagged"],
        )
        result = summary.get("result")
        if result is None:
            if args.accept and summary.get("run_id"):
                accept_run(summary["run_id"])
            return 0
    else:
        log.info("Starting clustering pipeline…")
        result = run_clustering(params)

    log.info("Run #%d complete:", result.run_id)
    log.info("  Clusters : %d", result.n_clusters)
    log.info("  Noise    : %d", result.n_noise)
    log.info(
        "  Sizes    : min=%d  max=%d  mean=%.1f",
        result.diagnostics["size_min"],
        result.diagnostics["size_max"],
        result.diagnostics["size_mean"],
    )
    if result.diagnostics.get("timings_ms"):
        log.info("  Timings  : %s", result.diagnostics["timings_ms"])
    log.info("Cluster labels:")
    for cid, label in result.cluster_labels.items():
        desc = result.cluster_descriptions.get(cid, "")
        size = result.diagnostics["cluster_sizes"].get(cid, 0)
        log.info("  [%d] %-30s (%d docs)  %s", cid, label, size, desc)

    if args.accept:
        accept_run(result.run_id)
        log.info("Run #%d accepted as active.", result.run_id)
    else:
        log.info(
            "Run #%d stored but NOT accepted. "
            "Review diagnostics and run with --accept, or accept via UI.",
            result.run_id,
        )
    return 0


if __name__ == "__main__":
    main()
