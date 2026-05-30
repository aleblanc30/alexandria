#!/usr/bin/env python
"""
Entry point: run the clustering pipeline and print diagnostics.

Usage:
    python scripts/run_clustering.py
    python scripts/run_clustering.py --accept          # auto-accept the run
    python scripts/run_clustering.py --skip-labelling  # TF-IDF labels only
    python scripts/run_clustering.py --min-cluster-size 10 --n-neighbors 20
    python scripts/run_clustering.py --assign-new      # assign unassigned docs only
    python scripts/run_clustering.py --drift           # print drift report
    python scripts/run_clustering.py --merges          # print merge suggestions
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.db.queries import init_db
from pka.clustering.engine import run_clustering
from pka.clustering.lifecycle import (
    accept_run, get_active_run_id,
    assign_new_docs, compute_drift, compute_merge_suggestions,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("run_clustering")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-cluster-size", type=int,   default=None)
    parser.add_argument("--min-samples",      type=int,   default=None)
    parser.add_argument("--n-neighbors",      type=int,   default=15)
    parser.add_argument("--min-dist",         type=float, default=0.1)
    parser.add_argument("--n-components",     type=int,   default=5)
    parser.add_argument("--label-model",      type=str,   default="llama3")
    parser.add_argument("--skip-labelling",   action="store_true")
    parser.add_argument("--accept",           action="store_true",
                        help="Auto-accept this run without manual review")
    parser.add_argument("--assign-new",       action="store_true",
                        help="Assign unassigned docs to existing clusters (no re-run)")
    parser.add_argument("--drift",            action="store_true",
                        help="Print drift report for the active run")
    parser.add_argument("--merges",           action="store_true",
                        help="Print merge suggestions for the active run")
    args = parser.parse_args()

    init_db()

    # ── Lifecycle-only modes ──────────────────────────────────────────────────
    if args.assign_new:
        stats = assign_new_docs()
        log.info("Assigned %d new documents.", stats["assigned"])
        return

    if args.drift:
        active = get_active_run_id()
        if active is None:
            log.error("No active run. Run clustering first.")
            sys.exit(1)
        for entry in compute_drift(active):
            flag = " ← SPLIT?" if entry["flagged"] else ""
            log.info("  [%s] drift=%.3f  recent=%d%s",
                     entry["label"], entry["drift_score"], entry["n_recent"], flag)
        return

    if args.merges:
        active = get_active_run_id()
        if active is None:
            log.error("No active run. Run clustering first.")
            sys.exit(1)
        for s in compute_merge_suggestions(active):
            log.info("  MERGE? '%s' + '%s'  sim=%.3f",
                     s["label_a"], s["label_b"], s["similarity"])
        return

    # ── Full clustering run ───────────────────────────────────────────────────
    log.info("Starting clustering pipeline…")
    result = run_clustering(
        min_cluster_size = args.min_cluster_size,
        min_samples      = args.min_samples,
        n_neighbors      = args.n_neighbors,
        min_dist         = args.min_dist,
        n_components     = args.n_components,
        label_model      = args.label_model,
        skip_labelling   = args.skip_labelling,
    )

    log.info("Run #%d complete:", result.run_id)
    log.info("  Clusters : %d", result.n_clusters)
    log.info("  Noise    : %d", result.n_noise)
    log.info("  Sizes    : min=%d  max=%d  mean=%.1f",
             result.diagnostics["size_min"],
             result.diagnostics["size_max"],
             result.diagnostics["size_mean"])
    log.info("Cluster labels:")
    for cid, label in result.cluster_labels.items():
        desc = result.cluster_descriptions.get(cid, "")
        size = result.diagnostics["cluster_sizes"].get(cid, 0)
        log.info("  [%d] %-30s (%d docs)  %s", cid, label, size, desc)

    if args.accept:
        accept_run(result.run_id)
        log.info("Run #%d accepted as active.", result.run_id)
    else:
        log.info("Run #%d stored but NOT accepted. "
                 "Review diagnostics and run with --accept, or accept via UI.",
                 result.run_id)


if __name__ == "__main__":
    main()
