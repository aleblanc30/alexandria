"""The embedding model: which one is in force, how it embeds, and moving to another.

No real model is loaded: ``sentence_transformers`` is replaced by a recorder,
and the suite's ``FakeEmbedder`` stands in wherever the vector store embeds.
"""

import logging
import sys
import types

import numpy as np
import pytest
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.storage import embedding
from pka.storage.embedding import get_embedder as real_get_embedder
from tests.conftest import make_document

E5 = "intfloat/multilingual-e5-small"


class _Recorder:
    """Stands in for ``SentenceTransformer``; records what it was asked to encode."""

    loads: list[tuple[str, str | None]] = []

    def __init__(self, name):
        import os

        _Recorder.loads.append((name, os.environ.get("HF_HUB_OFFLINE")))
        self.calls: list[tuple[list[str], bool]] = []

    def encode(self, texts, normalize_embeddings=False):
        self.calls.append((list(texts), normalize_embeddings))
        return np.ones((len(texts), 3), dtype=np.float32)


@pytest.fixture()
def fake_st(monkeypatch):
    _Recorder.loads = []
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _Recorder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(embedding, "get_embedder", real_get_embedder)
    monkeypatch.setattr(embedding, "_embedders", {})
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)
    return _Recorder


class TestEmbedders:
    def test_e5_gets_its_prefixes_and_normalised_vectors(self, fake_st):
        emb = embedding.get_embedder(E5)
        docs = emb.embed_documents(["a chunk", "another"])
        query = emb.embed_query("a question")

        calls = emb._model.calls
        assert calls[0] == (["passage: a chunk", "passage: another"], True)
        assert calls[1] == (["query: a question"], True)
        # Native floats, which Chroma requires.
        assert docs == [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
        assert type(query[0]) is float

    def test_a_model_without_prefixes_embeds_the_text_as_is(self, fake_st):
        emb = embedding.get_embedder("sentence-transformers/all-mpnet-base-v2")
        emb.embed_query("plain")
        assert emb._model.calls == [(["plain"], True)]

    def test_each_model_loads_once(self, fake_st):
        assert embedding.get_embedder(E5) is embedding.get_embedder(E5)
        assert [name for name, _ in fake_st.loads] == [E5]

    def test_loaded_offline_and_downloaded_once_when_not_cached(self, fake_st, monkeypatch):
        attempts = []

        class NotCached(_Recorder):
            def __init__(self, name):
                import os

                attempts.append(os.environ.get("HF_HUB_OFFLINE"))
                if len(attempts) == 1:
                    raise OSError("not in the local cache")
                super().__init__(name)

        sys.modules["sentence_transformers"].SentenceTransformer = NotCached
        embedding.get_embedder(E5)
        assert attempts == ["1", "0"]

    def test_hub_telemetry_is_off(self, fake_st, monkeypatch):
        import os

        monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "0")
        embedding.get_embedder(E5)
        assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"

    def test_the_legacy_model_goes_through_chromas_onnx_function(self, fake_st, monkeypatch):
        class Onnx:
            def __init__(self):
                self.seen = []

            def __call__(self, texts):
                self.seen.append(texts)
                return [np.array([0.5, 0.25], dtype=np.float32) for _ in texts]

        fn = Onnx()
        monkeypatch.setattr(
            "chromadb.utils.embedding_functions.DefaultEmbeddingFunction", lambda: fn
        )
        emb = embedding.get_embedder(embedding.LEGACY_MODEL)
        assert emb.embed_query("q") == [0.5, 0.25]
        assert fn.seen == [["q"]]  # no prefix: the vectors must match old archives
        assert fake_st.loads == []


@pytest.fixture()
def real_chroma(isolated_settings):
    import pka.storage.vector_store as vs

    vs.reset_collection()
    yield vs
    vs.reset_collection()


def _legacy_collection(vs):
    """A collection as every archive had it before the model was recorded."""
    vs.get_client().get_or_create_collection(vs.COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    vs.reset_collection()


class TestActiveModel:
    def test_a_new_collection_records_the_configured_model(self, real_chroma):
        assert real_chroma.active_model_name() == cfg.embedding_model
        assert real_chroma.get_collection().metadata["embedding_model"] == cfg.embedding_model

    def test_an_unrecorded_collection_keeps_the_legacy_model_and_warns_once(
        self, real_chroma, caplog
    ):
        _legacy_collection(real_chroma)
        with caplog.at_level(logging.WARNING, logger="pka.storage.vector_store"):
            assert real_chroma.active_model_name() == embedding.LEGACY_MODEL
            assert real_chroma.active_model_name() == embedding.LEGACY_MODEL
        warnings = [r for r in caplog.records if "alexandria reembed" in r.getMessage()]
        assert len(warnings) == 1

    def test_queries_and_upserts_embed_with_the_active_model(self, real_chroma, monkeypatch):
        _legacy_collection(real_chroma)
        used = []

        class Spy:
            def __init__(self, name):
                self.name = name

            def embed_documents(self, texts):
                used.append((self.name, "documents"))
                return [[0.1] * 4 for _ in texts]

            def embed_query(self, text):
                used.append((self.name, "query"))
                return [0.1] * 4

        monkeypatch.setattr(embedding, "get_embedder", Spy)
        real_chroma.upsert_chunks(
            ["v1"], ["text"], [{"document_id": 1, "source": "zotero", "chunk_index": 0}]
        )
        real_chroma.query("text", n_results=1)
        assert used == [
            (embedding.LEGACY_MODEL, "documents"),
            (embedding.LEGACY_MODEL, "query"),
        ]


def _chunked_document(vs, key: str, texts: list[str], metas: list[dict]) -> int:
    from pka.db.chunks import insert_chunks

    doc_id = make_document("calibre", key, f"Doc {key}", None, None)
    vids = [f"{key}-{i}" for i in range(len(texts))]
    vs.upsert_chunks(
        vids,
        texts,
        [
            {"document_id": doc_id, "source": "calibre", "chunk_index": i, **m}
            for i, m in enumerate(metas)
        ],
    )
    insert_chunks(
        [
            {
                "document_id": doc_id,
                "chunk_index": i,
                "text": t,
                "token_count": 1,
                "vector_id": vid,
                "chunk_pass": m.get("pass"),
            }
            for i, (t, vid, m) in enumerate(zip(texts, vids, metas, strict=True))
        ]
    )
    return doc_id


class TestRebuild:
    def test_rebuild_moves_a_legacy_archive_to_the_configured_model(self, real_chroma):
        from pka.db.migrate import init_db

        init_db()
        _legacy_collection(real_chroma)
        _chunked_document(real_chroma, "A", ["some text"], [{}])
        assert real_chroma.active_model_name() == embedding.LEGACY_MODEL

        real_chroma.rebuild_from_chunks()
        assert real_chroma.active_model_name() == cfg.embedding_model

    def test_rebuild_keeps_each_chunks_extra_metadata(self, real_chroma):
        from pka.db.migrate import init_db

        init_db()
        doc_id = _chunked_document(
            real_chroma,
            "B",
            ["chapter one text", "a synopsis"],
            [
                {"pass": "fulltext", "section_title": "One", "page_start": 3},
                {"pass": "external_synopsis", "isbn": "9780000000000"},
            ],
        )
        real_chroma.rebuild_from_chunks()

        page = real_chroma.fetch_records(include=["metadatas"])
        metas = sorted(page["metadatas"], key=lambda m: m["chunk_index"])
        assert metas[0]["section_title"] == "One"
        assert metas[0]["page_start"] == 3
        assert metas[0]["pass"] == "fulltext"
        assert metas[1]["isbn"] == "9780000000000"
        assert all(m["document_id"] == doc_id and m["title"] == "Doc B" for m in metas)

    def test_a_pass_only_sqlite_holds_is_restored(self, real_chroma):
        """Zotero metadata chunks predating ``pass`` got it in SQLite by migration only."""
        from pka.db.engine import get_engine
        from pka.db.migrate import init_db
        from pka.db.schema import chunks

        init_db()
        _chunked_document(real_chroma, "Z", ["an abstract"], [{}])
        with get_engine().begin() as con:
            con.execute(chunks.update().values(chunk_pass="metadata"))
        real_chroma.rebuild_from_chunks()
        (meta,) = real_chroma.fetch_records(include=["metadatas"])["metadatas"]
        assert meta["pass"] == "metadata"

    def test_a_chunk_unknown_to_chroma_gets_what_sqlite_mirrors(self, real_chroma):
        from pka.db.chunks import insert_chunks
        from pka.db.migrate import init_db

        init_db()
        doc_id = make_document("zotero", "C", "Doc C", None, None)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "orphan",
                    "token_count": 1,
                    "vector_id": None,
                    "chunk_pass": "fulltext",
                    "page_start": 7,
                    "page_end": 8,
                }
            ]
        )
        real_chroma.rebuild_from_chunks()
        (meta,) = real_chroma.fetch_records(include=["metadatas"])["metadatas"]
        assert meta["pass"] == "fulltext"
        assert (meta["page_start"], meta["page_end"]) == (7, 8)
        assert "resolved_by" not in meta


class TestReembed:
    def test_every_derived_vector_is_redone(self, real_chroma, monkeypatch):
        from pka import hooks, reembed
        from pka.db.engine import get_engine
        from pka.db.migrate import init_db
        from pka.db.schema import documents

        init_db()
        _legacy_collection(real_chroma)
        doc_ids = [
            _chunked_document(real_chroma, k, [f"text {k}", f"more {k}"], [{}, {}]) for k in "XY"
        ]
        announced = []
        monkeypatch.setattr(hooks, "document_embedded", announced.append)
        monkeypatch.setattr(reembed, "_retrain_tag_models", lambda: {"tag_models_retrained": 0})

        stats = reembed.reembed()

        assert stats["embedding_model"] == cfg.embedding_model
        assert stats["chunks"] == 4
        assert stats["documents"] == 2
        assert stats["clustering_stale"] is True
        # The learned-tag hook would score new vectors with old models.
        assert announced == []
        with get_engine().connect() as con:
            blobs = con.execute(
                sa.select(documents.c.doc_embedding).where(documents.c.id.in_(doc_ids))
            ).scalars()
            assert all(blobs)


class TestRetrainTagModels:
    @pytest.fixture()
    def sessions(self, isolated_settings):
        from pka.db.migrate import init_db
        from pka.tag_training import lifecycle
        from tests.test_tag_training import _seed_labeled_corpus

        init_db()
        pos_ids, neg_ids, extra_ids = _seed_labeled_corpus()
        labels = [{"doc_id": d, "label": 1} for d in pos_ids[:3]] + [
            {"doc_id": neg_ids[0], "label": 0}
        ]
        accepted = lifecycle.create_session("kept", labels)["session_id"]
        lifecycle.accept_session(accepted)
        labeling = lifecycle.create_session("draft", labels)["session_id"]
        return accepted, labeling, extra_ids

    def test_accepted_models_are_retrained_and_reapplied(self, sessions, monkeypatch):
        from pka import reembed
        from pka.db.engine import get_engine
        from pka.db.schema import overlay_tags
        from pka.tag_training import lifecycle

        accepted, labeling, _ = sessions
        with get_engine().begin() as con:
            con.execute(overlay_tags.delete())
        trained = []
        real_train = lifecycle.train_session
        monkeypatch.setattr(
            lifecycle, "train_session", lambda sid: trained.append(sid) or real_train(sid)
        )

        stats = reembed._retrain_tag_models()

        assert sorted(trained) == sorted([accepted, labeling])
        assert stats == {"tag_models_retrained": 2, "tag_models_dropped": []}
        with get_engine().connect() as con:
            tags = {r[0] for r in con.execute(sa.select(overlay_tags.c.tag))}
        assert tags == {"kept"}  # only the accepted model is applied

    def test_a_model_that_cannot_be_retrained_is_dropped(self, sessions, monkeypatch):
        from pka import reembed
        from pka.tag_training import lifecycle

        accepted, _, _ = sessions
        monkeypatch.setattr(
            lifecycle,
            "train_session",
            lambda sid: {"train_stats": {"error": "Need at least one positive"}},
        )
        stats = reembed._retrain_tag_models()
        assert stats["tag_models_retrained"] == 0
        assert sorted(stats["tag_models_dropped"]) == ["draft", "kept"]
        assert lifecycle.get_session(accepted)["has_model"] is False
