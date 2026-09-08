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


def _candidate_document_ids(source_filter: list[str] | None) -> list[int]:
    """Documents holding at least one chunk, honouring *source_filter*, from SQLite.

    ``ingest_text_block`` writes the SQLite ``chunks`` row and the Chroma vector
    together, so ``chunks`` is a faithful index of what the vector store holds
    and answers this without touching Chroma at all (audit P-3). Served by
    ``ix_chunks_document_id`` and ``ix_documents_source``.
    """
    import sqlalchemy as sa

    from pka.db.queries import get_engine
    from pka.db.schema import chunks, documents

    q = sa.select(chunks.c.document_id).select_from(chunks).distinct()
    if source_filter:
        q = q.join(documents, documents.c.id == chunks.c.document_id).where(
            documents.c.source.in_(source_filter)
        )
    with get_engine().connect() as con:
        return sorted({r[0] for r in con.execute(q)})


def _archive_has_chunks() -> bool:
    """Whether anything has been ingested at all, for the empty-archive message."""
    import sqlalchemy as sa

    from pka.db.queries import get_engine
    from pka.db.schema import chunks

    with get_engine().connect() as con:
        return con.execute(sa.select(chunks.c.id).limit(1)).first() is not None


def _load_document_embeddings(
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> tuple[list[int], np.ndarray]:
    """Mean-pool chunk embeddings per document; prefer SQLite cache when present."""
    from pka.clustering.doc_embeddings import load_cached_embeddings, refresh_document_embedding
    from pka.storage.vector_store import (
        fetch_embeddings_by_ids,
        fetch_records_by_document_ids,
    )

    sorted_ids = _candidate_document_ids(source_filter)
    if not sorted_ids:
        # Same two messages as before, told apart the same way: nothing ingested
        # at all, versus a filter that matched none of what was.
        if not _archive_has_chunks():
            raise ValueError("Vector store is empty — run ingestion first.")
        raise ValueError("No embeddings found after filtering.")

    if run_id is not None:
        raise_if_cancelled(run_id)

    cached, missing = load_cached_embeddings(sorted_ids)

    if missing:
        # Only the documents without a cached vector are read out of Chroma.
        # Fetching metadata first, rather than embeddings in one call, keeps
        # fetch_embeddings_by_ids' per-vector recovery of corrupt records.
        page = fetch_records_by_document_ids(missing, include=["metadatas"])
        vector_ids = page["ids"]
        metadatas = page["metadatas"]
        log.info(
            "Loading %d vectors from Chroma (%d docs without cache)…",
            len(vector_ids),
            len(missing),
        )
        if vector_ids:
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
            if embeddings:
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

    if not cached:
        # Chunks exist but nothing resolved to a vector, cached or in Chroma.
        raise ValueError("No embeddings found after filtering.")

    doc_ids = sorted(cached.keys())
    matrix = np.stack([cached[d] for d in doc_ids], axis=0)
    log.info("Aggregated embeddings for %d documents", len(doc_ids))
    return doc_ids, matrix
