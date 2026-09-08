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
from pka.db.queries import init_db, insert_chunks
from tests.conftest import make_document


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
        doc_id = make_document("zotero", "NC1", "No chunks", None, None)
        assert refresh_document_embedding(doc_id) is False
        cached, missing = load_cached_embeddings([doc_id])
        assert doc_id in missing
        assert doc_id not in cached

    def test_writes_mean_pooled_embedding(self, mock_chroma):
        doc_id = make_document("zotero", "EM1", "Has chunks", None, None)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "hello world",
                    "token_count": 2,
                    "vector_id": "vec-em1",
                }
            ]
        )
        from pka.storage import vector_store as vs

        vs.upsert_chunks(
            ids=["vec-em1"],
            texts=["hello world"],
            metadatas=[
                {
                    "document_id": doc_id,
                    "source": "zotero",
                    "title": "Has chunks",
                    "chunk_index": 0,
                }
            ],
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
        doc_id = make_document("zotero", "MX1", "Mixed", None, None)
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


class TestRefreshReusesVectorsItWasGiven:
    """The chunk vectors were just computed; reading them back is a wasted trip.

    `ingest_text_block` upserts chunks to Chroma and then called
    `refresh_document_embedding`, which fetched the metadatas and the embeddings
    of those same chunks straight back out. Passing the vectors forward removes
    both round trips for a document written in one block. Audit item P-4.
    """

    @staticmethod
    def _doc_with_chunks(vector_ids: list[str]) -> int:
        doc_id = make_document("zotero", "KV1", "Known vectors", None, None)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": i,
                    "text": f"chunk {i}",
                    "token_count": 2,
                    "vector_id": vid,
                }
                for i, vid in enumerate(vector_ids)
            ]
        )
        return doc_id

    @staticmethod
    def _vec(value: float) -> list[float]:
        return [value] * EMBEDDING_DIM

    def test_complete_known_vectors_skip_chroma_entirely(self, mock_chroma):
        _store, col = mock_chroma
        doc_id = self._doc_with_chunks(["kv-1", "kv-2"])
        known = {"kv-1": self._vec(1.0), "kv-2": self._vec(3.0)}

        assert refresh_document_embedding(doc_id, known=known) is True

        col.get.assert_not_called()
        cached, _ = load_cached_embeddings([doc_id])
        np.testing.assert_allclose(cached[doc_id], np.full(EMBEDDING_DIM, 2.0), rtol=1e-6)

    def test_partial_known_vectors_fall_back_to_chroma(self, mock_chroma):
        """A second block's chunks are not in hand, so the whole set is refetched."""
        _store, col = mock_chroma
        doc_id = self._doc_with_chunks(["kv-1", "kv-2"])
        col.upsert(
            ids=["kv-1", "kv-2"],
            documents=["chunk 0", "chunk 1"],
            metadatas=[{"document_id": doc_id}, {"document_id": doc_id}],
            embeddings=[self._vec(1.0), self._vec(3.0)],
        )

        assert refresh_document_embedding(doc_id, known={"kv-1": self._vec(1.0)}) is True

        assert col.get.called
        cached, _ = load_cached_embeddings([doc_id])
        np.testing.assert_allclose(cached[doc_id], np.full(EMBEDDING_DIM, 2.0), rtol=1e-6)

    def test_known_vectors_give_the_same_result_as_reading_them_back(self, mock_chroma):
        _store, col = mock_chroma
        doc_id = self._doc_with_chunks(["kv-1", "kv-2"])
        vectors = [self._vec(1.0), self._vec(3.0)]
        col.upsert(
            ids=["kv-1", "kv-2"],
            documents=["chunk 0", "chunk 1"],
            metadatas=[{"document_id": doc_id}, {"document_id": doc_id}],
            embeddings=vectors,
        )

        refresh_document_embedding(doc_id)
        from_chroma, _ = load_cached_embeddings([doc_id])
        refresh_document_embedding(doc_id, known=dict(zip(["kv-1", "kv-2"], vectors, strict=True)))
        from_known, _ = load_cached_embeddings([doc_id])

        np.testing.assert_allclose(from_known[doc_id], from_chroma[doc_id], rtol=1e-6)
