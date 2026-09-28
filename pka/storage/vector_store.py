"""
Chroma collection wrapper (embedded mode, persistent on disk).

Chroma stores the vectors; it does not compute them. Chunks and queries are
embedded here, by the model in :mod:`pka.storage.embedding` that the collection
was built with. The collection records that model's name in its metadata
(``embedding_model``); a collection from before the model was a setting has no
such key and was built with ``all-MiniLM-L6-v2``. When the ``embedding_model``
setting names a different model, this module keeps using the recorded one, since
vectors from two models are not comparable, and logs that ``alexandria reembed``
is needed to switch. A collection created from nothing records the setting.

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
from typing import Any

import chromadb
from chromadb.api.shared_system_client import SharedSystemClient
from chromadb.config import Settings as ChromaSettings

from pka.config import settings as cfg
from pka.storage import embedding

log = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None
_warned_mismatch = False
# Reentrant: get_collection() holds it across its call to get_client().
_client_lock = threading.RLock()
COLLECTION_NAME = "alexandria_chunks"
MODEL_KEY = "embedding_model"
_FETCH_BATCH_SIZE = 200

# Chroma hydrates metadatas/embeddings/documents with a second SQL query that
# binds one variable per matched record, and SQLITE_MAX_VARIABLE_NUMBER is
# 32766 — so a ``get`` matching more than that (or filtering on a longer id or
# ``$in`` list) fails with "Error executing plan: … too many SQL variables".
# Every read goes through the paging helpers below, well under the ceiling.
# A ``delete(ids=…)`` binds one variable per id the same way, as does the
# matching SQLite ``IN (...)``, so deletes are batched at the same size.
_GET_PAGE_SIZE = 5_000
_DELETE_BATCH_SIZE = _GET_PAGE_SIZE

# Chroma's own per-call ``upsert`` ceiling (``max_batch_size``, observed at
# 5461) raises ``chromadb.errors.InternalError`` for anything larger — seen in
# practice on a single large Firefox fetch (14584 chunks), which dropped the
# whole document's embedding. Batch below the observed ceiling with headroom.
_UPSERT_BATCH_SIZE = 5_000


def active_model_name() -> str:
    """The model the chunk collection was built with, which every embed must use."""
    global _warned_mismatch
    metadata = get_collection().metadata or {}
    name = metadata.get(MODEL_KEY) or embedding.LEGACY_MODEL
    if name != cfg.embedding_model and not _warned_mismatch:
        _warned_mismatch = True
        log.warning(
            "The chunk index was built with %s, but embedding_model is %s. Search and "
            "ingestion keep using %s until `alexandria reembed` rebuilds the index.",
            name,
            cfg.embedding_model,
            name,
        )
    return name


def active_embedder() -> embedding.Embedder:
    """The model every chunk and query is embedded with, and the chunker sizes for."""
    # Looked up on the module at call time, so the suite's fake reaches it.
    return embedding.get_embedder(active_model_name())


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
            except Exception as exc:  # noqa: BLE001 - a poisoned cache surfaces as anything
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
            # No embedding_function: every upsert and query passes vectors.
            # The metadata applies only when the collection is created; an
            # existing one keeps what it was built with.
            _collection = get_client().get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine", MODEL_KEY: cfg.embedding_model},
            )
            log.debug("Chroma collection '%s' ready", COLLECTION_NAME)
        return _collection


def vector_count() -> int:
    """Return stored vector count, falling back to SQLite chunk rows."""
    try:
        return get_collection().count()
    except Exception as exc:  # noqa: BLE001 - a Chroma outage falls back to the chunk table
        log.warning("Chroma count failed (%s); using chunk table", exc)
        import sqlalchemy as sa

        from pka.db.engine import get_engine
        from pka.db.schema import chunks

        with get_engine().connect() as con:
            return con.execute(sa.select(sa.func.count()).select_from(chunks)).scalar() or 0


def drop_document_collection() -> None:
    """Delete the document chunk collection and clear cached handles."""
    reset_collection()
    try:
        get_client().delete_collection(COLLECTION_NAME)
    except Exception as exc:  # noqa: BLE001 - deleting a collection that is not there is fine
        log.warning("Could not delete Chroma collection %s: %s", COLLECTION_NAME, exc)
    reset_collection()


def rebuild_from_chunks(*, batch_size: int = 32) -> dict[str, int]:
    """Rebuild ``alexandria_chunks`` from SQLite chunk text with the configured model.

    The dropped collection is recreated under the ``embedding_model`` setting,
    which is how an archive moves to a new model. Only the chunk vectors: the
    document vectors, tag models and clusters derived from them are
    :func:`pka.reembed.reembed`'s to redo.

    Each chunk keeps the Chroma metadata it had (``pass``, pages, section,
    synopsis provenance), read before the drop, and gains any of ``pass`` and
    its pages that only SQLite holds. A chunk the old collection cannot account
    for, or a collection too damaged to read, gets what SQLite mirrors of it.
    """
    import uuid

    import sqlalchemy as sa

    from pka.db.engine import get_engine
    from pka.db.schema import chunks, documents

    previous = _previous_metadata()
    drop_document_collection()
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(
                chunks.c.id,
                chunks.c.document_id,
                chunks.c.chunk_index,
                chunks.c.text,
                chunks.c.vector_id,
                chunks.c.chunk_pass,
                chunks.c.page_start,
                chunks.c.page_end,
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
        metadatas = [_rebuilt_metadata(r, previous.get(r.vector_id)) for r in batch]
        upsert_chunks(vector_ids, texts, metadatas)
        with eng.begin() as con:
            for row, vid in zip(batch, vector_ids, strict=False):
                con.execute(chunks.update().where(chunks.c.id == row.id).values(vector_id=vid))
        processed += len(batch)
        log.info("Rebuilt %d / %d chunk vectors", processed, total)

    return {"chunks": total, "processed": processed}


def _previous_metadata() -> dict[str, dict]:
    """Every chunk's current Chroma metadata by vector id; empty if unreadable."""
    try:
        page = fetch_records(include=["metadatas"])
    except Exception as exc:  # noqa: BLE001 - a rebuild must still run over a broken index
        log.warning("Could not read the old chunk metadata (%s); rebuilding from SQLite", exc)
        return {}
    return {vid: dict(m or {}) for vid, m in zip(page["ids"], page["metadatas"], strict=False)}


def _rebuilt_metadata(row: Any, previous: dict | None) -> dict:
    """The old metadata of a rebuilt chunk, plus any key only SQLite mirrors."""
    meta = dict(previous) if previous else {}
    mirrored = {"pass": row.chunk_pass, "page_start": row.page_start, "page_end": row.page_end}
    for key, value in mirrored.items():
        if value is not None:
            meta.setdefault(key, value)
    meta.update(
        document_id=row.document_id,
        source=row.source,
        title=row.title or "",
        chunk_index=row.chunk_index,
    )
    return meta


def reset_collection() -> None:
    """Drop the cached client and collection — used by the test suite."""
    global _client, _collection, _warned_mismatch
    with _client_lock:
        _client = None
        _collection = None
        _warned_mismatch = False


def upsert_chunks(
    ids: list[str],
    texts: list[str],
    metadatas: list[dict],
) -> list[list[float]]:
    """Upsert chunk documents and return the embeddings that were stored.

    Embedded here, by the collection's model, and returned so the document
    mean-pool does not have to read them straight back out. The embedder hands
    back native Python floats, which Chroma requires.
    """
    if not ids:
        return []
    col = get_collection()
    embeddings = active_embedder().embed_documents(texts)
    for i in range(0, len(ids), _UPSERT_BATCH_SIZE):
        window = slice(i, i + _UPSERT_BATCH_SIZE)
        col.upsert(
            ids=ids[window],
            documents=texts[window],
            metadatas=metadatas[window],
            embeddings=embeddings[window],
        )
    log.debug("Upserted %d chunks to Chroma", len(ids))
    return embeddings


def _empty_page(include: list[str]) -> dict[str, list]:
    return {"ids": [], **{key: [] for key in include}}


def _extend_page(out: dict[str, list], page: dict, include: list[str]) -> list[str]:
    ids = list(page.get("ids") or [])
    out["ids"].extend(ids)
    for key in include:
        values = page.get(key)
        if values is None:
            continue
        out[key].extend(values)
    return ids


def fetch_records(
    where: dict | None = None,
    include: list[str] | None = None,
) -> dict[str, list]:
    """``collection.get`` paged under the SQL variable ceiling (see _GET_PAGE_SIZE)."""
    include = list(include) if include is not None else ["metadatas"]
    col = get_collection()
    out = _empty_page(include)
    offset = 0
    while True:
        kwargs: dict = {"include": include, "limit": _GET_PAGE_SIZE, "offset": offset}
        if where:
            kwargs["where"] = where
        ids = _extend_page(out, col.get(**kwargs), include)
        if len(ids) < _GET_PAGE_SIZE:
            break
        offset += len(ids)
    return out


def fetch_records_by_ids(
    ids: list[str],
    include: list[str] | None = None,
) -> dict[str, list]:
    """``collection.get(ids=…)`` in batches, for id lists of any length."""
    include = list(include) if include is not None else ["metadatas"]
    out = _empty_page(include)
    if not ids:
        return out
    col = get_collection()
    for i in range(0, len(ids), _GET_PAGE_SIZE):
        page = col.get(ids=ids[i : i + _GET_PAGE_SIZE], include=include)
        _extend_page(out, page, include)
    return out


def fetch_records_by_document_ids(
    doc_ids: list[int],
    include: list[str] | None = None,
) -> dict[str, list]:
    """Every chunk record of the given documents, batching the ``$in`` filter."""
    include = list(include) if include is not None else ["metadatas"]
    out = _empty_page(include)
    if not doc_ids:
        return out
    for i in range(0, len(doc_ids), _GET_PAGE_SIZE):
        batch = doc_ids[i : i + _GET_PAGE_SIZE]
        page = fetch_records(where={"document_id": {"$in": batch}}, include=include)
        _extend_page(out, page, include)
    return out


def _fetch_embedding_batch(col, ids: list[str], out: dict[str, list[float]]) -> None:
    """Recursively fetch embeddings; corrupt ids are left out of ``out``."""
    if not ids:
        return
    if len(ids) == 1:
        try:
            page = col.get(ids=ids, include=["embeddings"])
            out[ids[0]] = page["embeddings"][0]
        except Exception:  # noqa: BLE001 - one unreadable vector is skipped, not fatal
            log.debug("Skipping unreadable Chroma vector %s", ids[0])
        return
    try:
        page = col.get(ids=ids, include=["embeddings"])
        for vid, emb in zip(page["ids"], page["embeddings"], strict=False):
            out[vid] = emb
    except Exception:  # noqa: BLE001 - a bad id fails the whole get; bisect to isolate it
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
    from pka.db.engine import get_engine
    from pka.db.schema import chunks

    col = get_collection()
    for i in range(0, len(vector_ids), _DELETE_BATCH_SIZE):
        batch = vector_ids[i : i + _DELETE_BATCH_SIZE]
        try:
            col.delete(ids=batch)
        except Exception as exc:  # noqa: BLE001 - per batch, so one failure strands no others
            # Per batch rather than all-or-nothing: one unreadable batch should
            # not strand the rest of the source's vectors in Chroma.
            log.warning("Chroma delete failed (%s); removing chunk rows only", exc)
    with get_engine().begin() as con:
        for i in range(0, len(vector_ids), _DELETE_BATCH_SIZE):
            batch = vector_ids[i : i + _DELETE_BATCH_SIZE]
            con.execute(chunks.delete().where(chunks.c.vector_id.in_(batch)))
    log.info("Purged %d corrupt chunk rows", len(vector_ids))
    return len(vector_ids)


def query(
    query_text: str,
    n_results: int = 10,
    where: dict | None = None,
) -> list[dict]:
    """Return the top-n most similar chunks for a natural-language query."""
    kwargs: dict = {
        "query_embeddings": [active_embedder().embed_query(query_text)],
        "n_results": n_results,
    }
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
