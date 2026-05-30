"""
Cluster lifecycle management (spec §5.4):

  - accept_run()      : mark a run as active; deactivate all others.
  - assign_new_docs() : assign documents added since last run to nearest
                        existing cluster centroid (no full re-run needed).
  - compute_drift()   : per-cluster drift score; flags clusters for split review.
  - compute_merges()  : pairwise centroid similarity; flags merge candidates.
"""
import json
import logging
import time

import numpy as np
import sqlalchemy as sa

from pka.db.queries import get_engine
from pka.db.schema import (
    cluster_runs, cluster_assignments, clusters,
    documents, chunks,
)

log = logging.getLogger(__name__)

DRIFT_THRESHOLD  = 0.60   # flag cluster for split review above this score
MERGE_THRESHOLD  = 0.85   # flag pair for merge review above this similarity


# ── Accept / reject ───────────────────────────────────────────────────────────

def accept_run(run_id: int) -> None:
    """Mark run_id as accepted. All other runs remain stored but inactive."""
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            cluster_runs.update()
            .where(cluster_runs.c.run_id == run_id)
            .values(accepted=True)
        )
    log.info("Run #%d accepted as active.", run_id)


def reject_run(run_id: int, notes: str = "") -> None:
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            cluster_runs.update()
            .where(cluster_runs.c.run_id == run_id)
            .values(accepted=False, notes=notes)
        )
    log.info("Run #%d rejected.", run_id)


def get_active_run_id() -> int | None:
    eng = get_engine()
    with eng.connect() as con:
        row = con.execute(
            sa.select(cluster_runs.c.run_id)
            .where(cluster_runs.c.accepted == True)
            .order_by(cluster_runs.c.run_id.desc())
            .limit(1)
        ).fetchone()
    return row[0] if row else None


# ── Centroid helpers ──────────────────────────────────────────────────────────

def _get_cluster_centroids(run_id: int) -> dict[int, np.ndarray]:
    """
    Compute mean embedding per cluster for the given run.
    Returns {db_cluster_id: centroid_vector}.
    """
    from pka.storage.vector_store import get_collection

    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(
                cluster_assignments.c.cluster_id,
                cluster_assignments.c.document_id,
            ).where(cluster_assignments.c.run_id == run_id)
        ).fetchall()

    if not rows:
        return {}

    # Group doc_ids by cluster
    cluster_docs: dict[int, list[int]] = {}
    for cid, did in rows:
        cluster_docs.setdefault(cid, []).append(did)

    col = get_collection()
    centroids: dict[int, np.ndarray] = {}

    for cid, doc_ids in cluster_docs.items():
        # Fetch chunk embeddings for these documents
        result = col.get(
            where    = {"document_id": {"$in": doc_ids}},
            include  = ["embeddings"],
        )
        if not result["embeddings"]:
            continue
        centroids[cid] = np.mean(result["embeddings"], axis=0).astype(np.float32)

    return centroids


# ── Assign new documents ──────────────────────────────────────────────────────

def assign_new_docs(run_id: int | None = None) -> dict:
    """
    Assign documents that have chunks but no cluster assignment in the
    active (or specified) run, using cosine similarity to cluster centroids.
    """
    active_run = run_id or get_active_run_id()
    if active_run is None:
        log.warning("No active run — run clustering first.")
        return {"assigned": 0}

    eng = get_engine()

    # Documents that already have an assignment in this run
    with eng.connect() as con:
        assigned_ids = set(
            r[0] for r in con.execute(
                sa.select(cluster_assignments.c.document_id)
                .where(cluster_assignments.c.run_id == active_run)
            ).fetchall()
        )

        # Documents that have chunks (i.e. have been embedded)
        all_chunked_ids = set(
            r[0] for r in con.execute(
                sa.select(chunks.c.document_id).distinct()
            ).fetchall()
        )

    unassigned = list(all_chunked_ids - assigned_ids)
    if not unassigned:
        log.info("No unassigned documents found.")
        return {"assigned": 0}

    log.info("%d unassigned documents — computing nearest centroids…", len(unassigned))

    centroids = _get_cluster_centroids(active_run)
    if not centroids:
        log.warning("No centroids available for run #%d", active_run)
        return {"assigned": 0}

    centroid_ids   = list(centroids.keys())
    centroid_matrix = np.stack([centroids[c] for c in centroid_ids])  # (n_clusters, dim)

    from pka.storage.vector_store import get_collection
    col = get_collection()
    now = int(time.time())
    assignment_rows = []

    for doc_id in unassigned:
        result = col.get(
            where   = {"document_id": {"$in": [doc_id]}},
            include = ["embeddings"],
        )
        if not result["embeddings"]:
            continue

        doc_vec = np.mean(result["embeddings"], axis=0).astype(np.float32)

        # Cosine similarity to each centroid
        norms = np.linalg.norm(centroid_matrix, axis=1) * np.linalg.norm(doc_vec)
        norms = np.where(norms == 0, 1e-9, norms)
        sims  = (centroid_matrix @ doc_vec) / norms
        best  = int(np.argmax(sims))

        assignment_rows.append({
            "document_id": doc_id,
            "cluster_id":  centroid_ids[best],
            "run_id":      active_run,
            "score":       float(sims[best]),
            "assigned_at": now,
        })

    if assignment_rows:
        with eng.begin() as con:
            con.execute(cluster_assignments.insert(), assignment_rows)

    log.info("Assigned %d new documents to existing clusters.", len(assignment_rows))
    return {"assigned": len(assignment_rows)}


# ── Drift detection ───────────────────────────────────────────────────────────

def compute_drift(run_id: int | None = None) -> list[dict]:
    """
    For each cluster in the active run, compute a drift score:
    mean cosine distance of documents added *after* the run timestamp
    from the cluster centroid.

    Returns a list of dicts sorted by drift score descending:
      [{cluster_id, label, drift_score, n_recent, flagged}, ...]
    """
    active_run = run_id or get_active_run_id()
    if active_run is None:
        return []

    eng = get_engine()
    with eng.connect() as con:
        run_row = con.execute(
            sa.select(cluster_runs.c.timestamp)
            .where(cluster_runs.c.run_id == active_run)
        ).fetchone()
        if not run_row:
            return []
        run_ts = run_row[0]

        # Cluster labels
        label_rows = con.execute(
            sa.select(clusters.c.cluster_id, clusters.c.label)
            .where(clusters.c.run_id == active_run)
        ).fetchall()
        label_map = {r[0]: r[1] for r in label_rows}

    centroids = _get_cluster_centroids(active_run)

    from pka.storage.vector_store import get_collection
    col = get_collection()
    results = []

    for cid, centroid in centroids.items():
        # Recent doc_ids: assigned to this cluster AND added after run_ts
        with eng.connect() as con:
            recent_ids = [
                r[0] for r in con.execute(
                    sa.select(cluster_assignments.c.document_id)
                    .join(documents, documents.c.id == cluster_assignments.c.document_id)
                    .where(
                        (cluster_assignments.c.cluster_id == cid) &
                        (cluster_assignments.c.run_id == active_run) &
                        (documents.c.ingested_at > run_ts)
                    )
                ).fetchall()
            ]

        if not recent_ids:
            results.append({"cluster_id": cid, "label": label_map.get(cid, ""),
                             "drift_score": 0.0, "n_recent": 0, "flagged": False})
            continue

        result = col.get(
            where   = {"document_id": {"$in": recent_ids}},
            include = ["embeddings"],
        )
        if not result["embeddings"]:
            continue

        vecs  = np.array(result["embeddings"], dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1e-9, norms)
        vecs_n = vecs / norms

        c_norm = centroid / (np.linalg.norm(centroid) + 1e-9)
        cosine_sims  = vecs_n @ c_norm
        drift_score  = float(1.0 - cosine_sims.mean())

        results.append({
            "cluster_id":  cid,
            "label":       label_map.get(cid, ""),
            "drift_score": round(drift_score, 3),
            "n_recent":    len(recent_ids),
            "flagged":     drift_score > DRIFT_THRESHOLD,
        })

    results.sort(key=lambda x: x["drift_score"], reverse=True)
    log.info("Drift computed for %d clusters; %d flagged for split review.",
             len(results), sum(1 for r in results if r["flagged"]))
    return results


# ── Merge detection ───────────────────────────────────────────────────────────

def compute_merge_suggestions(run_id: int | None = None) -> list[dict]:
    """
    Compute pairwise cosine similarity between cluster centroids.
    Returns pairs above MERGE_THRESHOLD, sorted by similarity descending:
      [{cluster_id_a, label_a, cluster_id_b, label_b, similarity}, ...]
    """
    active_run = run_id or get_active_run_id()
    if active_run is None:
        return []

    centroids = _get_cluster_centroids(active_run)
    if len(centroids) < 2:
        return []

    eng = get_engine()
    with eng.connect() as con:
        label_rows = con.execute(
            sa.select(clusters.c.cluster_id, clusters.c.label)
            .where(clusters.c.run_id == active_run)
        ).fetchall()
    label_map = {r[0]: r[1] for r in label_rows}

    cids    = list(centroids.keys())
    matrix  = np.stack([centroids[c] for c in cids]).astype(np.float32)
    norms   = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms   = np.where(norms == 0, 1e-9, norms)
    normed  = matrix / norms
    sim_mat = normed @ normed.T  # (n, n)

    suggestions = []
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            sim = float(sim_mat[i, j])
            if sim >= MERGE_THRESHOLD:
                suggestions.append({
                    "cluster_id_a": cids[i],
                    "label_a":      label_map.get(cids[i], ""),
                    "cluster_id_b": cids[j],
                    "label_b":      label_map.get(cids[j], ""),
                    "similarity":   round(sim, 3),
                })

    suggestions.sort(key=lambda x: x["similarity"], reverse=True)
    log.info("%d merge suggestions above threshold %.2f.",
             len(suggestions), MERGE_THRESHOLD)
    return suggestions
