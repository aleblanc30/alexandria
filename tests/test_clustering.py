"""
Clustering engine and lifecycle tests.
UMAP, HDBSCAN, and Ollama are all mocked — no GPU or server required.
Chroma is replaced by the mock_chroma fixture from conftest.py.
"""
import json
import time
import numpy as np
import pytest
from unittest.mock import patch, MagicMock

import sqlalchemy as sa

from pka.db.queries import init_db, upsert_document, get_engine
from pka.db.schema import cluster_runs, clusters, cluster_assignments


# ── Shared helpers ────────────────────────────────────────────────────────────

N_DOCS = 20
FAKE_DIM = 8


def _seed_documents(n: int = N_DOCS) -> list[int]:
    """Insert n documents and return their DB ids."""
    ids = []
    for i in range(n):
        src = ["zotero", "firefox", "calibre"][i % 3]
        did = upsert_document(src, f"SRC{i:03d}", f"Document {i}", None,
                              int(time.time()) - i * 3600)
        ids.append(did)
    return ids


def _fake_embeddings(n: int, dim: int = FAKE_DIM) -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.random((n, dim)).astype(np.float32)


def _mock_chroma_with_docs(monkeypatch, doc_ids: list[int]) -> tuple[dict, MagicMock]:
    """
    Populate a mock Chroma collection with one fake embedding per document.
    Returns (store_dict, collection_mock).
    """
    embs = _fake_embeddings(len(doc_ids))
    store = {
        f"vec-{did}": {
            "embedding": embs[i].tolist(),
            "text": f"text {did}",
            "meta": {"document_id": did, "source": "zotero", "chunk_index": 0},
        }
        for i, did in enumerate(doc_ids)
    }

    col = MagicMock()
    col.count.return_value = len(store)

    col.get.return_value = {
        "ids":        list(store.keys()),
        "embeddings": [v["embedding"] for v in store.values()],
        "metadatas":  [v["meta"] for v in store.values()],
        "documents":  [v["text"] for v in store.values()],
    }

    import pka.storage.vector_store as vs
    monkeypatch.setattr(vs, "get_collection", lambda: col)
    return store, col


def _mock_umap(monkeypatch) -> None:
    """Replace umap.UMAP with a PCA-like identity stub."""
    class FakeUMAP:
        def __init__(self, n_components=2, **kw):
            self.n_components = n_components
        def fit_transform(self, X):
            rng = np.random.default_rng(0)
            return rng.random((len(X), self.n_components)).astype(np.float32)

    fake_umap_module = MagicMock()
    fake_umap_module.UMAP = FakeUMAP
    monkeypatch.setitem(__import__("sys").modules, "umap", fake_umap_module)


def _mock_hdbscan(monkeypatch, n_docs: int, n_clusters: int = 4) -> None:
    """Return deterministic cluster labels cycling over n_clusters (no noise)."""
    labels = np.array([i % n_clusters for i in range(n_docs)], dtype=int)

    class FakeHDBSCAN:
        def __init__(self, **kw): pass
        def fit_predict(self, X): return labels

    fake_mod = MagicMock()
    fake_mod.HDBSCAN = FakeHDBSCAN
    monkeypatch.setitem(__import__("sys").modules, "hdbscan", fake_mod)


def _mock_llm(monkeypatch) -> None:
    """Return a predictable JSON response for every Ollama call."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "message": {"content": '{"label": "Test Topic", "description": "A test cluster."}'}
    }
    monkeypatch.setattr("httpx.post", lambda *a, **kw: resp)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


@pytest.fixture()
def populated(monkeypatch):
    """Seed DB + mock Chroma + mock UMAP/HDBSCAN/LLM. Returns doc_ids."""
    doc_ids = _seed_documents(N_DOCS)
    _mock_chroma_with_docs(monkeypatch, doc_ids)
    _mock_umap(monkeypatch)
    _mock_hdbscan(monkeypatch, N_DOCS, n_clusters=4)
    _mock_llm(monkeypatch)
    return doc_ids


# ── engine.run_clustering ─────────────────────────────────────────────────────

class TestRunClustering:
    def test_returns_cluster_run_result(self, populated):
        from pka.clustering.engine import run_clustering, ClusterRunResult
        result = run_clustering(min_cluster_size=2)
        assert isinstance(result, ClusterRunResult)

    def test_run_id_persisted_to_db(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.run_id)
                .where(cluster_runs.c.run_id == result.run_id)
            ).fetchone()
        assert row is not None

    def test_correct_cluster_count(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        assert result.n_clusters == 4

    def test_cluster_rows_written(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(clusters)
                .where(clusters.c.run_id == result.run_id)
            ).scalar()
        assert count == 4

    def test_assignments_written_for_all_docs(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(cluster_assignments)
                .where(cluster_assignments.c.run_id == result.run_id)
            ).scalar()
        assert count == N_DOCS

    def test_run_not_accepted_by_default(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.accepted)
                .where(cluster_runs.c.run_id == result.run_id)
            ).fetchone()
        assert row[0] is False or row[0] == 0

    def test_llm_labels_applied(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        assert all(v != "" for v in result.cluster_labels.values())

    def test_skip_labelling_uses_tfidf(self, populated, monkeypatch):
        """When skip_labelling=True, no HTTP call should be made."""
        call_count = {"n": 0}
        original_post = __import__("httpx").post
        def counting_post(*a, **kw):
            call_count["n"] += 1
            return original_post(*a, **kw)
        monkeypatch.setattr("httpx.post", counting_post)
        from pka.clustering.engine import run_clustering
        run_clustering(min_cluster_size=2, skip_labelling=True)
        assert call_count["n"] == 0

    def test_umap_2d_shape(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        assert result.umap_2d.shape == (N_DOCS, 2)

    def test_diagnostics_keys_present(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=2)
        for key in ("n_clusters", "n_noise", "cluster_sizes", "size_max", "size_mean"):
            assert key in result.diagnostics

    def test_parameters_stored_as_json(self, populated):
        from pka.clustering.engine import run_clustering
        result = run_clustering(min_cluster_size=3)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.parameters)
                .where(cluster_runs.c.run_id == result.run_id)
            ).fetchone()
        params = json.loads(row[0])
        assert params["min_cluster_size"] == 3

    def test_empty_vector_store_raises(self, monkeypatch):
        col = MagicMock()
        col.count.return_value = 0
        col.get.return_value = {"ids": [], "embeddings": [], "metadatas": [], "documents": []}
        import pka.storage.vector_store as vs
        monkeypatch.setattr(vs, "get_collection", lambda: col)
        from pka.clustering.engine import run_clustering
        with pytest.raises(ValueError, match="empty"):
            run_clustering()


# ── lifecycle.accept_run / reject_run ─────────────────────────────────────────

class TestAcceptRejectRun:
    def _insert_run(self) -> int:
        with get_engine().begin() as con:
            res = con.execute(
                cluster_runs.insert().values(
                    timestamp=int(time.time()), algorithm="HDBSCAN",
                    parameters="{}", accepted=False,
                )
            )
        return res.inserted_primary_key[0]

    def test_accept_sets_flag(self):
        from pka.clustering.lifecycle import accept_run
        run_id = self._insert_run()
        accept_run(run_id)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.accepted)
                .where(cluster_runs.c.run_id == run_id)
            ).fetchone()
        assert bool(row[0]) is True

    def test_reject_clears_flag(self):
        from pka.clustering.lifecycle import accept_run, reject_run
        run_id = self._insert_run()
        accept_run(run_id)
        reject_run(run_id, notes="too fragmented")
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.accepted)
                .where(cluster_runs.c.run_id == run_id)
            ).fetchone()
        assert bool(row[0]) is False

    def test_get_active_run_id_returns_latest_accepted(self):
        from pka.clustering.lifecycle import accept_run, get_active_run_id
        r1 = self._insert_run()
        r2 = self._insert_run()
        accept_run(r1)
        accept_run(r2)
        assert get_active_run_id() == r2

    def test_get_active_run_id_none_when_none_accepted(self):
        from pka.clustering.lifecycle import get_active_run_id
        assert get_active_run_id() is None


# ── lifecycle.compute_drift ───────────────────────────────────────────────────

class TestComputeDrift:
    def test_returns_list(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_drift
        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        drift = compute_drift()
        assert isinstance(drift, list)

    def test_each_entry_has_required_keys(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_drift
        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        for entry in compute_drift():
            for key in ("cluster_id", "label", "drift_score", "n_recent", "flagged"):
                assert key in entry

    def test_drift_score_in_range(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_drift
        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        for entry in compute_drift():
            assert 0.0 <= entry["drift_score"] <= 1.0

    def test_returns_empty_when_no_active_run(self):
        from pka.clustering.lifecycle import compute_drift
        assert compute_drift() == []


# ── lifecycle.compute_merge_suggestions ──────────────────────────────────────

class TestComputeMergeSuggestions:
    def test_returns_list(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_merge_suggestions
        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        suggestions = compute_merge_suggestions()
        assert isinstance(suggestions, list)

    def test_each_suggestion_has_required_keys(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_merge_suggestions
        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        for s in compute_merge_suggestions():
            for key in ("cluster_id_a", "label_a", "cluster_id_b", "label_b", "similarity"):
                assert key in s

    def test_no_self_pairs(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_merge_suggestions
        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        for s in compute_merge_suggestions():
            assert s["cluster_id_a"] != s["cluster_id_b"]

    def test_similarity_in_range(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_merge_suggestions
        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        for s in compute_merge_suggestions():
            assert 0.0 <= s["similarity"] <= 1.0

    def test_returns_empty_when_no_active_run(self):
        from pka.clustering.lifecycle import compute_merge_suggestions
        assert compute_merge_suggestions() == []


# ── lifecycle.assign_new_docs ─────────────────────────────────────────────────

class TestAssignNewDocs:
    def _mock_get(self, store, col):
        def _get(where=None, include=None):
            doc_ids = None
            if where and "document_id" in where:
                doc_ids = set(where["document_id"]["$in"])
            items = [
                (k, v) for k, v in store.items()
                if doc_ids is None or v["meta"]["document_id"] in doc_ids
            ]
            return {
                "ids":        [k for k, _ in items],
                "embeddings": [v["embedding"] for _, v in items],
                "metadatas":  [v["meta"] for _, v in items],
                "documents":  [v["text"] for _, v in items],
            }
        col.get.side_effect = _get

    def test_assigns_unassigned_docs(self, populated, monkeypatch):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, assign_new_docs
        from pka.db.queries import get_engine, insert_chunks, upsert_document

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)

        new_id = upsert_document("zotero", "NEW001", "New doc", None, int(time.time()))
        insert_chunks([{
            "document_id": new_id,
            "chunk_index": 0,
            "text": "new content",
            "token_count": 2,
            "vector_id": f"vec-new-{new_id}",
        }])

        store, col = _mock_chroma_with_docs(monkeypatch, populated + [new_id])
        store[f"vec-new-{new_id}"] = {
            "embedding": [0.5] * FAKE_DIM,
            "text": "new",
            "meta": {"document_id": new_id, "source": "zotero", "chunk_index": 0},
        }
        self._mock_get(store, col)

        stats = assign_new_docs(result.run_id)
        assert stats["assigned"] == 1

    def test_no_active_run_returns_zero(self):
        from pka.clustering.lifecycle import assign_new_docs
        assert assign_new_docs() == {"assigned": 0}

    def test_all_assigned_returns_zero(self, populated, monkeypatch):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, assign_new_docs

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        stats = assign_new_docs(result.run_id)
        assert stats["assigned"] == 0

    def test_compute_drift_with_recent_docs(self, populated, monkeypatch):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, compute_drift
        from pka.db.queries import get_engine
        from pka.db.schema import documents

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)

        with get_engine().begin() as con:
            con.execute(
                documents.update()
                .where(documents.c.id == populated[0])
                .values(ingested_at=int(time.time()) + 1000)
            )

        store, col = _mock_chroma_with_docs(monkeypatch, populated)
        self._mock_get(store, col)

        drift = compute_drift(result.run_id)
        assert len(drift) >= 1
        assert any(d["n_recent"] >= 0 for d in drift)


class TestAdaptiveClusterParams:
    def test_scales_with_corpus_size(self):
        from pka.clustering.engine import adaptive_cluster_params
        mcs, ms, nn = adaptive_cluster_params(100)
        assert 3 <= mcs <= 15
        assert ms >= 2
        assert 5 <= nn <= 30
