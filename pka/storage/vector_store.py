"""
Chroma collection wrapper (embedded mode, persistent on disk).

Text chunks use Chroma's default embedding function (Sentence Transformers
``all-MiniLM-L6-v2``). Pass documents only on upsert; use :func:`query` with
natural-language text at search time.

A single module-level client/collection pair is cached for the lifetime of
the process. Tests should reset it via :func:`reset_collection`.

Creation is serialized under ``_client_lock``, and every Chroma client in the
process comes from :func:`get_client` — including the CLIP one in
``image_pipeline``. Chroma caches one *system* per persist path
(``SharedSystemClient._identifier_to_system``) but guards only its refcounts
with a lock, not that cache: ``_create_system_if_not_exists`` publishes the
system into the dict *before* ``start()`` populates the Rust bindings. So two
threads building a client for the same path at once give the second one a
``ServerAPI`` whose ``bindings`` attribute does not exist yet
(``AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'``), and
its failure handler then releases the refcount the first thread has not taken
yet — stopping and popping the half-started system, which leaves the first
thread with ``KeyError: 'data\\chroma'`` and every later client in that process
broken until it restarts. Ingestion embeds through ``asyncio.to_thread`` from a
pool of fetch workers, so concurrent first-touch is the normal case, not a rare
one.
"""

from __future__ import annotations

import logging
import threading

import chromadb
from chromadb.api.shared_system_client import SharedSystemClient
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None
_embedding_fn: DefaultEmbeddingFunction | None = None
# Reentrant: get_collection() holds it across its call to get_client().
_client_lock = threading.RLock()
COLLECTION_NAME = "alexandria_chunks"
_FETCH_BATCH_SIZE = 200


def _get_embedding_function() -> DefaultEmbeddingFunction:
    global _embedding_fn
    if _embedding_fn is None:
        _embedding_fn = DefaultEmbeddingFunction()
    return _embedding_fn


def _new_client() -> chromadb.ClientAPI:
    cfg.chroma_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(cfg.chroma_dir),
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def get_client() -> chromadb.ClientAPI:
    """The process-wide Chroma client. Every caller must come through here."""
    global _client
    with _client_lock:
        if _client is None:
            try:
                _client = _new_client()
            except Exception as exc:
                # A system left half-started or stopped in Chroma's per-path
                # cache poisons every later client in the process. Dropping that
                # cache is Chroma's own supported way out; retried once, because
                # a second failure is a real problem with the store.
                log.warning("Chroma client init failed (%s); clearing its cache", exc)
                SharedSystemClient.clear_system_cache()
                _client = _new_client()
            log.debug("Chroma client initialised at %s", cfg.chroma_dir)
        return _client


def get_collection() -> chromadb.Collection:
    global _collection
    with _client_lock:
        if _collection is None:
            _collection = get_client().get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
                embedding_function=_get_embedding_function(),
            )
            log.debug("Chroma collection '%s' ready", COLLECTION_NAME)
        return _collection


def vector_count() -> int:
    """Return stored vector count, falling back to SQLite chunk rows."""
    try:
        return get_collection().count()
    except Exception as exc:
        log.warning("Chroma count failed (%s); using chunk table", exc)
        import sqlalchemy as sa

        from pka.db.queries import get_engine
        from pka.db.schema import chunks

        with get_engine().connect() as con:
            return con.execute(sa.select(sa.func.count()).select_from(chunks)).scalar() or 0


def drop_document_collection() -> None:
    """Delete the document chunk collection and clear cached handles."""
    reset_collection()
    try:
        get_client().delete_collection(COLLECTION_NAME)
    except Exception as exc:
        log.warning("Could not delete Chroma collection %s: %s", COLLECTION_NAME, exc)
    reset_collection()


def rebuild_from_chunks(*, batch_size: int = 32) -> dict[str, int]:
    """Rebuild ``alexandria_chunks`` from SQLite chunk text (Chroma embeds in-process)."""
    import uuid

    import sqlalchemy as sa

    from pka.db.queries import get_engine
    from pka.db.schema import chunks, documents

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
        vector_ids = [str(uuid.uuid4()) for _ in batch]
        metadatas = [
            {
                "document_id": r.document_id,
                "source": r.source,
                "title": r.title or "",
                "chunk_index": r.chunk_index,
            }
            for r in batch
        ]
        upsert_chunks(vector_ids, texts, metadatas)
        with eng.begin() as con:
            for row, vid in zip(batch, vector_ids, strict=False):
                con.execute(chunks.update().where(chunks.c.id == row.id).values(vector_id=vid))
        processed += len(batch)
        log.info("Rebuilt %d / %d chunk vectors", processed, total)

    return {"chunks": total, "processed": processed}


def reset_collection() -> None:
    """Drop the cached client and collection — used by the test suite."""
    global _client, _collection
    with _client_lock:
        _client = None
        _collection = None


def upsert_chunks(
    ids: list[str],
    texts: list[str],
    metadatas: list[dict],
) -> None:
    """Upsert chunk documents; Chroma computes embeddings from ``texts``."""
    if not ids:
        return
    get_collection().upsert(
        ids=ids,
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
        for vid, emb in zip(page["ids"], page["embeddings"], strict=False):
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
        con.execute(chunks.delete().where(chunks.c.vector_id.in_(vector_ids)))
    log.info("Purged %d corrupt chunk rows", len(vector_ids))
    return len(vector_ids)


def query(
    query_text: str,
    n_results: int = 10,
    where: dict | None = None,
) -> list[dict]:
    """Return the top-n most similar chunks for a natural-language query."""
    kwargs: dict = {"query_texts": [query_text], "n_results": n_results}
    if where:
        kwargs["where"] = where
    res = get_collection().query(**kwargs)
    out: list[dict] = []
    for i, vid in enumerate(res["ids"][0]):
        out.append(
            {
                "vector_id": vid,
                "text": res["documents"][0][i],
                "distance": res["distances"][0][i],
                "metadata": res["metadatas"][0][i],
            }
        )
    return out
