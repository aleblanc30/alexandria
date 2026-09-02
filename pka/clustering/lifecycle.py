"""
Cluster lifecycle management (spec §5.4):

  - accept_run()      : mark a run as active; deactivate all others.
  - assign_new_docs() : assign documents added since last run to nearest
                        existing cluster centroid (no full re-run needed).
  - compute_drift()   : per-cluster drift score; flags clusters for split review.
  - compute_merges()  : pairwise centroid similarity; flags merge candidates.
"""

import logging
import time

import numpy as np
import sqlalchemy as sa

from pka.clustering.vectors import l2_normalize_rows
from pka.config import settings as cfg
from pka.db.queries import get_engine
from pka.db.schema import (
    chunks,
    cluster_assignments,
    cluster_runs,
    clusters,
    documents,
)

log = logging.getLogger(__name__)

DRIFT_THRESHOLD = 0.60  # flag cluster for split review above this score
MERGE_THRESHOLD = 0.85  # flag pair for merge review above this similarity


# ── Accept / reject ───────────────────────────────────────────────────────────


def accept_run(run_id: int) -> None:
    """Mark run_id as the single accepted (active) run; deactivate all others."""
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            cluster_runs.update().where(cluster_runs.c.run_id != run_id).values(accepted=False)
        )
        con.execute(
            cluster_runs.update().where(cluster_runs.c.run_id == run_id).values(accepted=True)
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
            .where(cluster_runs.c.accepted == True)  # noqa: E712 — SQLA expression
            .order_by(cluster_runs.c.run_id.desc())
            .limit(1)
        ).fetchone()
    return row[0] if row else None


# ── Centroid helpers ──────────────────────────────────────────────────────────


def _embeddings_available(result: dict) -> bool:
    """True when Chroma returned at least one embedding (avoids numpy truthiness bugs)."""
    embs = result.get("embeddings")
    if embs is None:
        return False
    try:
        return len(embs) > 0
    except TypeError:
        return bool(embs)


def _doc_mean_embeddings(doc_ids: list[int]) -> dict[int, np.ndarray]:
    """Mean-pool chunk embeddings per document, batched under Chroma's variable cap."""
    from pka.storage.vector_store import fetch_records_by_document_ids

    if not doc_ids:
        return {}

    result = fetch_records_by_document_ids(doc_ids, include=["embeddings", "metadatas"])
    if not _embeddings_available(result):
        return {}

    doc_vecs: dict[int, list[np.ndarray]] = {}
    metas = result.get("metadatas") or []
    for emb, meta in zip(result["embeddings"], metas, strict=False):
        did = int((meta or {}).get("document_id", -1))
        if did == -1:
            continue
        doc_vecs.setdefault(did, []).append(np.asarray(emb, dtype=np.float32))

    return {did: np.mean(v, axis=0).astype(np.float32) for did, v in doc_vecs.items()}


def noise_cluster_id(run_id: int) -> int | None:
    """The run's noise bucket, or ``None`` for a run that has none."""
    eng = get_engine()
    with eng.connect() as con:
        row = con.execute(
            sa.select(clusters.c.cluster_id).where(
                (clusters.c.run_id == run_id) & (clusters.c.is_noise == True)  # noqa: E712 — SQLA expression
            )
        ).fetchone()
    return row[0] if row else None


def load_persisted_centroids(run_id: int, level: int = 1) -> dict[int, np.ndarray]:
    """{db_cluster_id: centroid} from ``clusters.centroid``; null rows omitted.

    The noise bucket is excluded: it is a holding pen, not a topic, so its mean
    is meaningless and must never attract a document.
    """
    from pka.clustering.doc_embeddings import blob_to_embedding

    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(clusters.c.cluster_id, clusters.c.centroid).where(
                (clusters.c.run_id == run_id)
                & (clusters.c.level == level)
                & (clusters.c.is_noise == False)  # noqa: E712 — SQLA expression
            )
        ).fetchall()
    return {cid: blob_to_embedding(blob) for cid, blob in rows if blob is not None}


def _backfill_centroids(centroids: dict[int, np.ndarray]) -> None:
    """Persist recomputed centroids for a legacy (pre-migration) run."""
    from pka.clustering.doc_embeddings import embedding_to_blob

    if not centroids:
        return
    eng = get_engine()
    with eng.begin() as con:
        for cid, vec in centroids.items():
            con.execute(
                clusters.update()
                .where(clusters.c.cluster_id == cid)
                .values(centroid=embedding_to_blob(vec))
            )


def _compute_cluster_centroids(
    run_id: int,
    level: int | None = None,
) -> dict[int, np.ndarray]:
    """Recompute mean embedding per cluster from Chroma (the pre-persistence path).

    Skips the noise bucket for the same reason :func:`load_persisted_centroids`
    does — recomputing it here would also let ``_backfill_centroids`` persist
    one, turning the holding pen into an assignment target.
    """
    eng = get_engine()
    with eng.connect() as con:
        q = (
            sa.select(
                cluster_assignments.c.cluster_id,
                cluster_assignments.c.document_id,
            )
            .join(clusters, clusters.c.cluster_id == cluster_assignments.c.cluster_id)
            .where(
                (cluster_assignments.c.run_id == run_id) & (clusters.c.is_noise == False)  # noqa: E712 — SQLA expression
            )
        )
        if level is not None:
            q = q.where(cluster_assignments.c.level == level)
        rows = con.execute(q).fetchall()

    if not rows:
        return {}

    cluster_docs: dict[int, list[int]] = {}
    for cid, did in rows:
        cluster_docs.setdefault(cid, []).append(did)

    all_doc_ids = list({did for docs in cluster_docs.values() for did in docs})
    doc_means = _doc_mean_embeddings(all_doc_ids)

    centroids: dict[int, np.ndarray] = {}
    for cid, doc_ids in cluster_docs.items():
        vecs = [doc_means[d] for d in doc_ids if d in doc_means]
        if vecs:
            centroids[cid] = np.mean(vecs, axis=0).astype(np.float32)

    return centroids


def _doc_mean_embeddings_cached(doc_ids: list[int]) -> dict[int, np.ndarray]:
    """Mean-pooled document embeddings, preferring ``documents.doc_embedding``.

    Falls back to Chroma (:func:`_doc_mean_embeddings`) only for the ids the
    SQLite cache doesn't have.
    """
    from pka.clustering.doc_embeddings import load_cached_embeddings

    if not doc_ids:
        return {}
    found, missing = load_cached_embeddings(doc_ids)
    if missing:
        found.update(_doc_mean_embeddings(missing))
    return found


def _get_cluster_centroids(
    run_id: int,
    level: int = 1,
) -> dict[int, np.ndarray]:
    """
    Mean embedding per cluster for the given run and level.

    Prefers ``clusters.centroid`` (persisted at run time); falls back to
    recomputing from Chroma only for a run with no persisted centroids at
    all (pre-migration), backfilling the column so the cost is paid once.
    Returns {db_cluster_id: centroid_vector}.
    """
    persisted = load_persisted_centroids(run_id, level=level)
    if persisted:
        return persisted
    centroids = _compute_cluster_centroids(run_id, level=level)
    _backfill_centroids(centroids)
    return centroids


def _nearest_centroid(
    doc_vec: np.ndarray,
    centroids: dict[int, np.ndarray],
) -> tuple[int | None, float]:
    if not centroids:
        return None, 0.0
    centroid_ids = list(centroids.keys())
    centroid_matrix = np.stack([centroids[c] for c in centroid_ids])
    norms = np.linalg.norm(centroid_matrix, axis=1) * np.linalg.norm(doc_vec)
    norms = np.where(norms == 0, 1e-9, norms)
    sims = (centroid_matrix @ doc_vec) / norms
    best = int(np.argmax(sims))
    return centroid_ids[best], float(sims[best])


# ── Assign new documents ──────────────────────────────────────────────────────


def assign_new_docs(run_id: int | None = None) -> dict:
    """
    Assign documents that have chunks but no level-1 cluster assignment in the
    active (or specified) run, using cosine similarity to cluster centroids.
    Also assigns level-2 when L2 children exist for the chosen L1 parent.
    """
    active_run = run_id or get_active_run_id()
    if active_run is None:
        log.warning("No active run — run clustering first.")
        return {"assigned": 0}

    eng = get_engine()

    with eng.connect() as con:
        assigned_ids = set(
            r[0]
            for r in con.execute(
                sa.select(cluster_assignments.c.document_id).where(
                    (cluster_assignments.c.run_id == active_run)
                    & (cluster_assignments.c.level == 1)
                )
            ).fetchall()
        )

        all_chunked_ids = set(
            r[0] for r in con.execute(sa.select(chunks.c.document_id).distinct()).fetchall()
        )

        l2_parent_rows = con.execute(
            sa.select(clusters.c.cluster_id, clusters.c.parent_cluster_id).where(
                (clusters.c.run_id == active_run) & (clusters.c.level == 2)
            )
        ).fetchall()

    unassigned = list(all_chunked_ids - assigned_ids)
    if not unassigned:
        log.info("No unassigned documents found.")
        return {"assigned": 0}

    log.info("%d unassigned documents — computing nearest centroids…", len(unassigned))

    l1_centroids = _get_cluster_centroids(active_run, level=1)
    if not l1_centroids:
        log.warning("No L1 centroids available for run #%d", active_run)
        return {"assigned": 0}

    l2_centroids = _get_cluster_centroids(active_run, level=2)
    l2_by_parent: dict[int, dict[int, np.ndarray]] = {}
    for cid, parent_id in l2_parent_rows:
        if parent_id is not None and cid in l2_centroids:
            l2_by_parent.setdefault(parent_id, {})[cid] = l2_centroids[cid]

    now = int(time.time())
    assignment_rows = []

    # Documents HDBSCAN already called noise are not in ``unassigned`` at all —
    # they hold a level-1 row in the run's noise bucket, so this pass leaves
    # them alone instead of re-filing them into a real cluster on every ingest.
    # This floor is the same rule for documents ingested *since* the run: below
    # it, nothing is a good enough match to claim membership.
    min_sim = cfg.cluster_assign_min_similarity
    noise_cid = noise_cluster_id(active_run) if min_sim > 0 else None
    n_to_noise = 0
    n_no_embedding = 0

    doc_means = _doc_mean_embeddings_cached(unassigned)
    for doc_id in unassigned:
        doc_vec = doc_means.get(doc_id)
        if doc_vec is None:
            # Chunked in SQLite but no vector in Chroma or the embedding cache.
            # Counted so a stuck pool shows up in the log instead of silently
            # reappearing as "unassigned" on every later ingest.
            n_no_embedding += 1
            continue

        l1_cid, l1_score = _nearest_centroid(doc_vec, l1_centroids)
        if l1_cid is None:
            continue

        if noise_cid is not None and l1_score < min_sim:
            assignment_rows.append(
                {
                    "document_id": doc_id,
                    "cluster_id": noise_cid,
                    "run_id": active_run,
                    "score": l1_score,
                    "assigned_at": now,
                    "level": 1,
                }
            )
            n_to_noise += 1
            continue

        assignment_rows.append(
            {
                "document_id": doc_id,
                "cluster_id": l1_cid,
                "run_id": active_run,
                "score": l1_score,
                "assigned_at": now,
                "level": 1,
            }
        )

        child_centroids = l2_by_parent.get(l1_cid, {})
        if child_centroids:
            l2_cid, l2_score = _nearest_centroid(doc_vec, child_centroids)
            if l2_cid is not None:
                assignment_rows.append(
                    {
                        "document_id": doc_id,
                        "cluster_id": l2_cid,
                        "run_id": active_run,
                        "score": l2_score,
                        "assigned_at": now,
                        "level": 2,
                    }
                )

    if assignment_rows:
        with eng.begin() as con:
            con.execute(cluster_assignments.insert(), assignment_rows)

    n_docs = len({r["document_id"] for r in assignment_rows})
    log.info(
        "Assigned %d new documents to existing clusters (%d below the %.2f "
        "similarity floor → noise, %d with no embedding).",
        n_docs - n_to_noise,
        n_to_noise,
        min_sim,
        n_no_embedding,
    )
    return {"assigned": n_docs, "noise": n_to_noise, "no_embedding": n_no_embedding}


# ── Drift detection ───────────────────────────────────────────────────────────


def compute_drift(
    run_id: int | None = None,
    *,
    centroids: dict[int, np.ndarray] | None = None,
) -> list[dict]:
    """
    For each cluster in the active run, compute a drift score:
    mean cosine distance of documents added *after* the run timestamp
    from the cluster centroid.

    ``centroids`` lets a caller that already loaded them (e.g. alongside
    :func:`compute_merge_suggestions`) skip a second load.

    Returns a list of dicts sorted by drift score descending:
      [{cluster_id, label, drift_score, n_recent, flagged}, ...]
    """
    active_run = run_id or get_active_run_id()
    if active_run is None:
        return []

    eng = get_engine()
    with eng.connect() as con:
        run_row = con.execute(
            sa.select(cluster_runs.c.timestamp).where(cluster_runs.c.run_id == active_run)
        ).fetchone()
        if not run_row:
            return []
        run_ts = run_row[0]

        # Level-1 cluster labels only (L2 sub-clusters are not merge/drift targets)
        label_rows = con.execute(
            sa.select(clusters.c.cluster_id, clusters.c.label).where(
                (clusters.c.run_id == active_run) & (clusters.c.level == 1)
            )
        ).fetchall()
        label_map = {r[0]: r[1] for r in label_rows}

    if centroids is None:
        centroids = _get_cluster_centroids(active_run, level=1)
    results = []

    # One query for all recent level-1 assignments, then one batched
    # embedding fetch — instead of two roundtrips per cluster.
    recent_by_cluster: dict[int, list[int]] = {}
    with eng.connect() as con:
        for cid, did in con.execute(
            sa.select(
                cluster_assignments.c.cluster_id,
                cluster_assignments.c.document_id,
            )
            .join(documents, documents.c.id == cluster_assignments.c.document_id)
            .where(
                (cluster_assignments.c.run_id == active_run)
                & (cluster_assignments.c.level == 1)
                & (documents.c.ingested_at > run_ts)
            )
        ):
            recent_by_cluster.setdefault(cid, []).append(did)

    all_recent = [d for ids in recent_by_cluster.values() for d in ids]
    doc_means = _doc_mean_embeddings_cached(all_recent) if all_recent else {}

    for cid, centroid in centroids.items():
        recent_ids = recent_by_cluster.get(cid, [])
        if not recent_ids:
            results.append(
                {
                    "cluster_id": cid,
                    "label": label_map.get(cid, ""),
                    "drift_score": 0.0,
                    "n_recent": 0,
                    "flagged": False,
                }
            )
            continue

        vecs_list = [doc_means[d] for d in recent_ids if d in doc_means]
        if not vecs_list:
            continue
        vecs = np.stack(vecs_list)

        vecs_n = l2_normalize_rows(vecs)

        c_norm = centroid / (np.linalg.norm(centroid) + 1e-9)
        cosine_sims = vecs_n @ c_norm
        drift_score = float(1.0 - cosine_sims.mean())

        results.append(
            {
                "cluster_id": cid,
                "label": label_map.get(cid, ""),
                "drift_score": round(drift_score, 3),
                "n_recent": len(recent_ids),
                "flagged": drift_score > DRIFT_THRESHOLD,
            }
        )

    results.sort(key=lambda x: x["drift_score"], reverse=True)
    log.info(
        "Drift computed for %d clusters; %d flagged for split review.",
        len(results),
        sum(1 for r in results if r["flagged"]),
    )
    return results


# ── Merge detection ───────────────────────────────────────────────────────────


def compute_merge_suggestions(
    run_id: int | None = None,
    *,
    centroids: dict[int, np.ndarray] | None = None,
) -> list[dict]:
    """
    Compute pairwise cosine similarity between cluster centroids.

    ``centroids`` lets a caller that already loaded them (e.g. alongside
    :func:`compute_drift`) skip a second load.

    Returns pairs above MERGE_THRESHOLD, sorted by similarity descending:
      [{cluster_id_a, label_a, cluster_id_b, label_b, similarity}, ...]
    """
    active_run = run_id or get_active_run_id()
    if active_run is None:
        return []

    if centroids is None:
        centroids = _get_cluster_centroids(active_run, level=1)
    if len(centroids) < 2:
        return []

    eng = get_engine()
    with eng.connect() as con:
        label_rows = con.execute(
            sa.select(clusters.c.cluster_id, clusters.c.label).where(
                (clusters.c.run_id == active_run) & (clusters.c.level == 1)
            )
        ).fetchall()
    label_map = {r[0]: r[1] for r in label_rows}

    cids = list(centroids.keys())
    matrix = np.stack([centroids[c] for c in cids]).astype(np.float32)
    normed = l2_normalize_rows(matrix)
    sim_mat = normed @ normed.T  # (n, n)

    suggestions = []
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            sim = float(sim_mat[i, j])
            if sim >= MERGE_THRESHOLD:
                suggestions.append(
                    {
                        "cluster_id_a": cids[i],
                        "label_a": label_map.get(cids[i], ""),
                        "cluster_id_b": cids[j],
                        "label_b": label_map.get(cids[j], ""),
                        "similarity": round(sim, 3),
                    }
                )

    suggestions.sort(key=lambda x: x["similarity"], reverse=True)
    log.info("%d merge suggestions above threshold %.2f.", len(suggestions), MERGE_THRESHOLD)
    return suggestions


# ── Incremental update ───────────────────────────────────────────────────────


def run_incremental_clustering(
    *,
    label_model: str | None = None,
    **run_kwargs,
) -> dict:
    """
    Assign new documents to the active run; run a full clustering only when
    there is no active run to assign them to.

    Drift is measured and reported, never acted on: per ``DESIGN.md`` §4 the
    flags mark clusters an operator may want to split, and re-clustering stays
    a decision they make deliberately via ``/runs/trigger``.
    """
    from pka.clustering.engine import run_clustering

    active = get_active_run_id()
    if active is None:
        result = run_clustering(label_model=label_model, **run_kwargs)
        return {
            "action": "full_run",
            "run_id": result.run_id,
            "assigned": 0,
            "flagged": 0,
            "result": result,
        }

    stats = assign_new_docs(active)
    flagged = [d for d in compute_drift(active) if d["flagged"]]

    if flagged:
        log.info(
            "Drift flagged %d L1 cluster(s) in run #%d — review under diagnostics; "
            "re-cluster manually if the split is wanted",
            len(flagged),
            active,
        )

    log.info(
        "Incremental update: assigned %d doc(s) to run #%d",
        stats["assigned"],
        active,
    )
    return {
        "action": "assign_only",
        "run_id": active,
        "assigned": stats["assigned"],
        "flagged": len(flagged),
        "result": None,
    }
