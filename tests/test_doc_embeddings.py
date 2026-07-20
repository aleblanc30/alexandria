"""Tests for document embedding cache (MiniLM mean-pool)."""
import numpy as np
import pytest

from pka.clustering.doc_embeddings import (
    EMBEDDING_DIM,
    blob_to_embedding,
    embedding_to_blob,
    load_cached_embeddings,
    refresh_document_embedding,
)
from pka.db.queries import init_db, insert_chunks, upsert_document


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


class TestBlobRoundTrip:
    def test_embedding_round_trip(self):
        vec = np.random.rand(EMBEDDING_DIM).astype(np.float32)
        restored = blob_to_embedding(embedding_to_blob(vec))
        np.testing.assert_allclose(restored, vec, rtol=1e-6)


class TestRefreshDocumentEmbedding:
    def test_clears_embedding_when_no_chunks(self, mock_chroma):
        doc_id = upsert_document("zotero", "NC1", "No chunks", None, None)
        assert refresh_document_embedding(doc_id) is False
        cached, missing = load_cached_embeddings([doc_id])
        assert doc_id in missing
        assert doc_id not in cached

    def test_writes_mean_pooled_embedding(self, mock_chroma):
        doc_id = upsert_document("zotero", "EM1", "Has chunks", None, None)
        insert_chunks([{
            "document_id": doc_id,
            "chunk_index": 0,
            "text": "hello world",
            "token_count": 2,
            "vector_id": "vec-em1",
        }])
        from pka.storage import vector_store as vs

        vs.upsert_chunks(
            ids=["vec-em1"],
            texts=["hello world"],
            metadatas=[{
                "document_id": doc_id,
                "source": "zotero",
                "title": "Has chunks",
                "chunk_index": 0,
            }],
        )
        assert refresh_document_embedding(doc_id) is True
        cached, missing = load_cached_embeddings([doc_id])
        assert doc_id in cached
        assert doc_id not in missing
        assert cached[doc_id].ndim == 1
        assert cached[doc_id].shape[0] > 0


class TestLoadCachedEmbeddings:
    def test_empty_doc_ids(self):
        found, missing = load_cached_embeddings([])
        assert found == {}
        assert missing == []

    def test_mixed_cached_and_missing(self, mock_chroma):
        doc_id = upsert_document("zotero", "MX1", "Mixed", None, None)
        vec = np.ones(EMBEDDING_DIM, dtype=np.float32)
        import sqlalchemy as sa

        from pka.db.queries import get_engine
        from pka.db.schema import documents

        with get_engine().begin() as con:
            con.execute(
                sa.update(documents)
                .where(documents.c.id == doc_id)
                .values(doc_embedding=embedding_to_blob(vec))
            )
        found, missing = load_cached_embeddings([doc_id, 99999])
        assert doc_id in found
        assert 99999 in missing
