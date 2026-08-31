"""Cached mean-pooled document embeddings (384-d MiniLM) in SQLite."""

from __future__ import annotations

import logging

import numpy as np
import sqlalchemy as sa

from pka.db.queries import get_engine
from pka.db.schema import chunks, documents

log = logging.getLogger(__name__)

EMBEDDING_DIM = 384
_ID_BATCH_SIZE = 5_000


def embedding_to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def refresh_document_embedding(doc_id: int) -> bool:
    """Recompute mean-pooled chunk embedding for one document and persist."""
    from pka.storage.vector_store import fetch_embeddings_by_ids, fetch_records_by_ids

    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(chunks.c.vector_id).where(chunks.c.document_id == doc_id)
        ).fetchall()
    vector_ids = [r[0] for r in rows if r[0]]
    if not vector_ids:
        with eng.begin() as con:
            con.execute(
                documents.update().where(documents.c.id == doc_id).values(doc_embedding=None)
            )
        return False

    meta_page = fetch_records_by_ids(vector_ids, include=["metadatas"])
    metadatas = meta_page.get("metadatas") or []
    ids = meta_page.get("ids") or []
    if not ids:
        return False

    embeddings, _ = fetch_embeddings_by_ids(ids)
    vecs: list[list[float]] = []
    for vid, meta in zip(ids, metadatas, strict=True):
        emb = embeddings.get(vid)
        if emb is None:
            continue
        if int(meta.get("document_id", -1)) != doc_id:
            continue
        vecs.append(emb)
    if not vecs:
        return False

    mean_vec = np.mean(np.array(vecs, dtype=np.float32), axis=0)
    blob = embedding_to_blob(mean_vec)
    with eng.begin() as con:
        con.execute(documents.update().where(documents.c.id == doc_id).values(doc_embedding=blob))
    try:
        from pka.tag_training.lifecycle import apply_learned_tags_for_document

        apply_learned_tags_for_document(doc_id)
    except Exception:
        log.exception("Failed to apply learned tags to document %d", doc_id)
    return True


def load_cached_embeddings(
    doc_ids: list[int],
) -> tuple[dict[int, np.ndarray], list[int]]:
    """Return ({doc_id: vector}, missing_doc_ids)."""
    if not doc_ids:
        return {}, []
    eng = get_engine()
    row_map: dict[int, bytes | None] = {}
    with eng.connect() as con:
        # Batched: SQLite binds one variable per id and caps them at 32766.
        for i in range(0, len(doc_ids), _ID_BATCH_SIZE):
            batch = doc_ids[i : i + _ID_BATCH_SIZE]
            rows = con.execute(
                sa.select(documents.c.id, documents.c.doc_embedding).where(
                    documents.c.id.in_(batch)
                )
            ).fetchall()
            row_map.update({r[0]: r[1] for r in rows})
    found: dict[int, np.ndarray] = {}
    missing: list[int] = []
    for did in doc_ids:
        blob = row_map.get(did)
        if blob:
            found[did] = blob_to_embedding(blob)
        else:
            missing.append(did)
    return found, missing
