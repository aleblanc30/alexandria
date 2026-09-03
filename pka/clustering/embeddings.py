"""Step 1: aggregate per-document embeddings for a clustering run.

Split out of ``engine.py`` (planning/M1_CLUSTERING_ENGINE_SPLIT.md).
"""

from __future__ import annotations

import logging

import numpy as np

from pka.clustering.run_progress import raise_if_cancelled

log = logging.getLogger(__name__)


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
