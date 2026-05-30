"""
Chroma collection wrapper (embedded mode, persistent on disk).

A single module-level client/collection pair is cached for the lifetime of
the process. Tests should reset it via :func:`reset_collection`.
"""
from __future__ import annotations

import logging

import chromadb
from chromadb.config import Settings as ChromaSettings

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None
COLLECTION_NAME = "pka_chunks"
_FETCH_BATCH_SIZE = 200


def _get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        cfg.chroma_dir.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=str(cfg.chroma_dir),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        log.debug("Chroma client initialised at %s", cfg.chroma_dir)
    return _client


def get_collection() -> chromadb.Collection:
    global _collection
    if _collection is None:
        _collection = _get_client().get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        log.debug("Chroma collection '%s' ready", COLLECTION_NAME)
    return _collection


def vector_count() -> int:
    """Return stored vector count, falling back to SQLite chunk rows."""
    try:
        return get_collection().count()
    except Exception as exc:
        log.warning("Chroma count failed (%s); using chunk table", exc)
        from pka.db.queries import get_engine
        from pka.db.schema import chunks
        import sqlalchemy as sa

        with get_engine().connect() as con:
            return con.execute(
                sa.select(sa.func.count()).select_from(chunks)
            ).scalar() or 0


def drop_document_collection() -> None:
    """Delete the document chunk collection and clear cached handles."""
    reset_collection()
    try:
        _get_client().delete_collection(COLLECTION_NAME)
    except Exception as exc:
        log.warning("Could not delete Chroma collection %s: %s", COLLECTION_NAME, exc)
    reset_collection()


def rebuild_from_chunks(*, batch_size: int = 32) -> dict[str, int]:
    """Rebuild ``pka_chunks`` from SQLite chunk text (requires Ollama)."""
    import uuid

    import sqlalchemy as sa

    from pka.db.queries import get_engine
    from pka.db.schema import chunks, documents
    from pka.ingestion.embedder import embed_batch

    drop_document_collection()
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(
                chunks.c.id,
                chunks.c.document_id,
                chunks.c.chunk_index,
                chunks.c.text,
                documents.c.source,
                documents.c.title,
            )
            .join(documents, documents.c.id == chunks.c.document_id)
            .order_by(chunks.c.id)
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {"chunks": 0, "processed": 0}

    processed = 0
    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        texts = [r.text for r in batch]
        embeddings = embed_batch(texts, batch_size=batch_size)
        vector_ids = [str(uuid.uuid4()) for _ in batch]
        metadatas = [
            {
                "document_id": r.document_id,
                "source":      r.source,
                "title":       r.title or "",
                "chunk_index": r.chunk_index,
            }
            for r in batch
        ]
        upsert_chunks(vector_ids, embeddings, texts, metadatas)
        with eng.begin() as con:
            for row, vid in zip(batch, vector_ids):
                con.execute(
                    chunks.update()
                    .where(chunks.c.id == row.id)
                    .values(vector_id=vid)
                )
        processed += len(batch)
        log.info("Rebuilt %d / %d chunk vectors", processed, total)

    return {"chunks": total, "processed": processed}


def reset_collection() -> None:
    """Drop the cached client and collection — used by the test suite."""
    global _client, _collection
    _client = None
    _collection = None


def upsert_chunks(
    ids: list[str],
    embeddings: list[list[float]],
    texts: list[str],
    metadatas: list[dict],
) -> None:
    """Upsert a batch of chunk embeddings into Chroma."""
    if not ids:
        return
    get_collection().upsert(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas,
    )
    log.debug("Upserted %d chunks to Chroma", len(ids))


def _fetch_embedding_batch(col, ids: list[str], out: dict[str, list[float]]) -> None:
    """Recursively fetch embeddings; corrupt ids are left out of ``out``."""
    if not ids:
        return
    if len(ids) == 1:
        try:
            page = col.get(ids=ids, include=["embeddings"])
            out[ids[0]] = page["embeddings"][0]
        except Exception:
            log.debug("Skipping unreadable Chroma vector %s", ids[0])
        return
    try:
        page = col.get(ids=ids, include=["embeddings"])
        for vid, emb in zip(page["ids"], page["embeddings"]):
            out[vid] = emb
    except Exception:
        mid = len(ids) // 2
        _fetch_embedding_batch(col, ids[:mid], out)
        _fetch_embedding_batch(col, ids[mid:], out)


def fetch_embeddings_by_ids(ids: list[str]) -> tuple[dict[str, list[float]], list[str]]:
    """Return ``({vector_id: embedding}, corrupt_ids)``."""
    if not ids:
        return {}, []
    col = get_collection()
    found: dict[str, list[float]] = {}
    for i in range(0, len(ids), _FETCH_BATCH_SIZE):
        _fetch_embedding_batch(col, ids[i : i + _FETCH_BATCH_SIZE], found)
    corrupt = [vid for vid in ids if vid not in found]
    return found, corrupt


def purge_vectors(vector_ids: list[str]) -> int:
    """Remove vectors from Chroma and their ``chunks`` rows."""
    if not vector_ids:
        return 0
    from pka.db.queries import get_engine
    from pka.db.schema import chunks

    try:
        get_collection().delete(ids=vector_ids)
    except Exception as exc:
        log.warning("Chroma delete failed (%s); removing chunk rows only", exc)
    with get_engine().begin() as con:
        con.execute(
            chunks.delete().where(chunks.c.vector_id.in_(vector_ids))
        )
    log.info("Purged %d corrupt chunk rows", len(vector_ids))
    return len(vector_ids)


def query(
    embedding: list[float],
    n_results: int = 10,
    where: dict | None = None,
) -> list[dict]:
    """Return the top-n most similar chunks for a query embedding."""
    kwargs: dict = {"query_embeddings": [embedding], "n_results": n_results}
    if where:
        kwargs["where"] = where
    res = get_collection().query(**kwargs)
    out: list[dict] = []
    for i, vid in enumerate(res["ids"][0]):
        out.append({
            "vector_id": vid,
            "text":      res["documents"][0][i],
            "distance":  res["distances"][0][i],
            "metadata":  res["metadatas"][0][i],
        })
    return out
