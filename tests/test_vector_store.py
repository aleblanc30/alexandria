"""Integration tests for the Chroma vector store wrapper."""

import pytest
import sqlalchemy as sa

from tests.conftest import make_document


@pytest.fixture()
def real_chroma(isolated_settings):
    import pka.storage.vector_store as vs

    vs.reset_collection()
    yield vs
    vs.reset_collection()


class TestVectorStore:
    def test_upsert_and_query(self, real_chroma):
        real_chroma.upsert_chunks(
            ids=["vec-1"],
            texts=["hello world"],
            metadatas=[{"document_id": 1, "source": "zotero", "chunk_index": 0}],
        )
        hits = real_chroma.query("hello world", n_results=1)
        assert len(hits) == 1
        assert hits[0]["text"] == "hello world"
        assert hits[0]["metadata"]["document_id"] == 1

    def test_upsert_empty_batch_is_noop(self, real_chroma):
        real_chroma.upsert_chunks([], [], [])

    def test_query_with_where_filter(self, real_chroma):
        real_chroma.upsert_chunks(
            ids=["a", "b"],
            texts=["zotero doc about cats", "firefox doc about dogs"],
            metadatas=[
                {"document_id": 1, "source": "zotero", "chunk_index": 0},
                {"document_id": 2, "source": "firefox", "chunk_index": 0},
            ],
        )
        hits = real_chroma.query(
            "dogs and firefox",
            n_results=5,
            where={"source": "firefox"},
        )
        assert all(h["metadata"]["source"] == "firefox" for h in hits)

    def test_get_collection_is_cached(self, real_chroma):
        col_a = real_chroma.get_collection()
        col_b = real_chroma.get_collection()
        assert col_a is col_b

    def test_fetch_embeddings_by_ids(self, real_chroma):
        real_chroma.upsert_chunks(
            ids=["a", "b"],
            texts=["one", "two"],
            metadatas=[
                {"document_id": 1, "source": "zotero", "chunk_index": 0},
                {"document_id": 2, "source": "zotero", "chunk_index": 0},
            ],
        )
        found, corrupt = real_chroma.fetch_embeddings_by_ids(["a", "b", "missing"])
        assert set(found.keys()) == {"a", "b"}
        assert corrupt == ["missing"]
        assert all(len(v) > 0 for v in found.values())

    def test_purge_vectors_removes_chunks(self, real_chroma):
        from pka.db.queries import get_engine, init_db, insert_chunks
        from pka.db.schema import chunks

        init_db()
        real_chroma.upsert_chunks(
            ids=["dead"],
            texts=["gone"],
            metadatas=[{"document_id": 9, "source": "zotero", "chunk_index": 0}],
        )
        insert_chunks(
            [
                {
                    "document_id": 9,
                    "chunk_index": 0,
                    "text": "gone",
                    "token_count": 1,
                    "vector_id": "dead",
                }
            ]
        )
        n = real_chroma.purge_vectors(["dead"])
        assert n == 1
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(chunks).where(chunks.c.vector_id == "dead")
            ).scalar()
        assert count == 0

    def test_vector_count_from_collection(self, real_chroma):
        real_chroma.upsert_chunks(
            ids=["v1"],
            texts=["one"],
            metadatas=[{"document_id": 1, "source": "zotero", "chunk_index": 0}],
        )
        assert real_chroma.vector_count() >= 1

    def test_vector_count_falls_back_to_sqlite(self, real_chroma, monkeypatch):
        from pka.db.queries import init_db, insert_chunks

        init_db()
        doc_id = make_document("zotero", "VC1", "Title", None, None)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "chunk text",
                    "token_count": 2,
                    "vector_id": None,
                }
            ]
        )

        def _boom():
            raise RuntimeError("chroma down")

        col = real_chroma.get_collection()
        monkeypatch.setattr(col, "count", _boom)
        monkeypatch.setattr(real_chroma, "get_collection", lambda: col)
        assert real_chroma.vector_count() == 1

    def test_drop_document_collection_clears_vectors(self, real_chroma):
        real_chroma.upsert_chunks(
            ids=["gone"],
            texts=["text"],
            metadatas=[{"document_id": 1, "source": "zotero", "chunk_index": 0}],
        )
        real_chroma.drop_document_collection()
        assert real_chroma.get_collection().count() == 0

    def test_rebuild_from_chunks(self, real_chroma):
        import sqlalchemy as sa

        from pka.db.queries import init_db, insert_chunks
        from pka.db.schema import chunks

        init_db()
        doc_id = make_document("zotero", "RB1", "Rebuild doc", None, None)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "rebuild me",
                    "token_count": 2,
                    "vector_id": None,
                }
            ]
        )
        stats = real_chroma.rebuild_from_chunks(batch_size=8)
        assert stats["chunks"] == 1
        assert stats["processed"] == 1
        from pka.db.queries import get_engine

        with get_engine().connect() as con:
            vid = con.execute(
                sa.select(chunks.c.vector_id).where(chunks.c.document_id == doc_id)
            ).scalar()
        assert vid is not None
        assert real_chroma.get_collection().count() == 1

    def test_rebuild_empty_returns_zero(self, real_chroma):
        from pka.db.queries import init_db

        init_db()
        assert real_chroma.rebuild_from_chunks() == {"chunks": 0, "processed": 0}

    def test_fetch_embedding_batch_splits_on_error(self, real_chroma, monkeypatch):
        real_chroma.upsert_chunks(
            ids=["good"],
            texts=["ok"],
            metadatas=[{"document_id": 1, "source": "zotero", "chunk_index": 0}],
        )
        col = real_chroma.get_collection()
        real_get = col.get

        def flaky_get(*args, **kwargs):
            ids = kwargs.get("ids") or (args[0] if args else [])
            if ids and len(ids) > 1:
                raise RuntimeError("batch fail")
            return real_get(*args, **kwargs)

        monkeypatch.setattr(col, "get", flaky_get)
        found, corrupt = real_chroma.fetch_embeddings_by_ids(["good", "bad-id"])
        assert "good" in found
        assert "bad-id" in corrupt


class TestSharedClient:
    """One Chroma client per process, created once.

    Chroma caches a system per persist path but does not guard that cache with a
    lock: two threads constructing a client for the same path at once leave the
    second holding a ServerAPI whose Rust bindings are not started yet, and its
    cleanup then stops the half-started system out from under the first. Every
    later client in the process fails until it restarts. Ingestion embeds from a
    pool of `asyncio.to_thread` workers, so simultaneous first touch is normal.
    """

    def test_concurrent_first_touch_creates_exactly_one_client(self, monkeypatch):
        import threading
        import time

        import pka.storage.vector_store as vs

        created = []

        def _slow_client():
            time.sleep(0.02)  # widen the window a lock has to cover
            client = object()
            created.append(client)
            return client

        vs.reset_collection()
        monkeypatch.setattr(vs, "_new_client", _slow_client)

        start = threading.Barrier(8)
        got: list[object] = []

        def _worker():
            start.wait()
            got.append(vs.get_client())

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(created) == 1
        assert got == [created[0]] * 8
        vs.reset_collection()

    def test_clip_collection_reuses_the_shared_client(self, monkeypatch):
        """The CLIP collection must not build a second client for the same path."""
        import pka.ingestion.image_pipeline as ip
        import pka.storage.vector_store as vs

        clip_collection = object()

        class _FakeClient:
            def get_or_create_collection(self, **kwargs):
                return clip_collection

        fake = _FakeClient()
        vs.reset_collection()
        ip.reset_clip_collection()
        monkeypatch.setattr(vs, "_new_client", lambda: fake)

        assert ip._get_clip_collection() is clip_collection
        assert ip._clip_client is fake
        assert vs.get_client() is fake
        vs.reset_collection()
        ip.reset_clip_collection()

    def test_a_poisoned_system_cache_is_cleared_and_retried(self, monkeypatch):
        """Recovery for a process already broken by the race above."""
        import pka.storage.vector_store as vs

        cleared: list[bool] = []
        client = object()
        attempts = []

        def _flaky_client():
            attempts.append(1)
            if len(attempts) == 1:
                raise KeyError("data\\chroma")
            return client

        vs.reset_collection()
        monkeypatch.setattr(vs, "_new_client", _flaky_client)
        monkeypatch.setattr(
            vs.SharedSystemClient,
            "clear_system_cache",
            staticmethod(lambda: cleared.append(True)),
        )

        assert vs.get_client() is client
        assert cleared == [True]
        vs.reset_collection()
