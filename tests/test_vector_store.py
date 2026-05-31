"""Integration tests for the Chroma vector store wrapper."""
import sqlalchemy as sa
import pytest


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
        from pka.db.queries import get_engine, insert_chunks, init_db
        from pka.db.schema import chunks

        init_db()
        real_chroma.upsert_chunks(
            ids=["dead"],
            texts=["gone"],
            metadatas=[{"document_id": 9, "source": "zotero", "chunk_index": 0}],
        )
        insert_chunks([{
            "document_id": 9,
            "chunk_index": 0,
            "text": "gone",
            "token_count": 1,
            "vector_id": "dead",
        }])
        n = real_chroma.purge_vectors(["dead"])
        assert n == 1
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(chunks)
                .where(chunks.c.vector_id == "dead")
            ).scalar()
        assert count == 0
