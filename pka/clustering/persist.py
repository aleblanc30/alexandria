"""Step 6: write a clustering run to SQLite.

Split out of ``engine.py`` (planning/M1_CLUSTERING_ENGINE_SPLIT.md). Owns the
``cluster_runs`` / ``clusters`` / ``cluster_assignments`` writes, including the
per-run noise bucket.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, cast

import numpy as np
from sqlalchemy import Connection, CursorResult

from pka.clustering.doc_embeddings import embedding_to_blob
from pka.clustering.types import (
    ALGORITHM_PCA,
    NOISE_CLUSTER_DESCRIPTION,
    NOISE_CLUSTER_LABEL,
    L2ClusterBatch,
)
from pka.db.queries import get_engine
from pka.db.schema import cluster_assignments, cluster_runs, clusters

log = logging.getLogger(__name__)


def _inserted_id(res: Any) -> int:
    """The autoincrement PK of a just-executed INSERT.

    ``Connection.execute`` is typed as returning ``Result``, which has no
    ``inserted_primary_key``; the cast says what the runtime object actually is
    rather than leaving the whole module on mypy's ignore list.
    """
    pk = cast(CursorResult, res).inserted_primary_key
    if pk is None:  # only for a non-INSERT result — a programming error here
        raise RuntimeError("INSERT did not return a primary key")
    return int(pk[0])


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
        return _inserted_id(res)


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
    con: Connection,
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
        return _inserted_id(res)

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
        return _inserted_id(res)

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
