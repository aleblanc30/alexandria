"""
Clustering engine and lifecycle tests.
UMAP, HDBSCAN, and Ollama are all mocked — no GPU or server required.
Chroma is replaced by the mock_chroma fixture from conftest.py.
"""

import json
import sys
import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
import sqlalchemy as sa

from pka.db.queries import get_engine, init_db
from pka.db.schema import cluster_assignments, cluster_runs, clusters
from tests.conftest import make_document

# ── Shared helpers ────────────────────────────────────────────────────────────

N_DOCS = 20
FAKE_DIM = 8


def _seed_documents(n: int = N_DOCS) -> list[int]:
    """Insert n documents and return their DB ids."""
    ids = []
    for i in range(n):
        src = ["zotero", "firefox", "calibre"][i % 3]
        did = make_document(src, f"SRC{i:03d}", f"Document {i}", None, int(time.time()) - i * 3600)
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
        "ids": list(store.keys()),
        "embeddings": [v["embedding"] for v in store.values()],
        "metadatas": [v["meta"] for v in store.values()],
        "documents": [v["text"] for v in store.values()],
    }

    import pka.storage.vector_store as vs

    monkeypatch.setattr(vs, "get_collection", lambda: col)
    return store, col


def _mock_umap(monkeypatch) -> None:
    """Replace umap.UMAP with a PCA-like identity stub."""

    class FakeUMAP:
        def __init__(self, n_components=2, **kw):
            self.n_components = n_components

        def fit_transform(self, X, y=None):
            rng = np.random.default_rng(0)
            return rng.random((len(X), self.n_components)).astype(np.float32)

    fake_umap_module = MagicMock()
    fake_umap_module.UMAP = FakeUMAP
    monkeypatch.setitem(__import__("sys").modules, "umap", fake_umap_module)


def _mock_hdbscan(monkeypatch, n_docs: int, n_clusters: int = 4) -> None:
    """Return deterministic cluster labels; first call = L1, later calls = L2 (2 clusters)."""
    state = {"call": 0}

    class FakeHDBSCAN:
        def __init__(self, **kw):
            pass

        def fit_predict(self, X):
            state["call"] += 1
            n = len(X)
            if state["call"] == 1:
                return np.array([i % n_clusters for i in range(n)], dtype=int)
            return np.array([i % 2 for i in range(n)], dtype=int)

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


def _mock_hdbscan_with_noise(monkeypatch, n_clusters: int = 3) -> None:
    """Like ``_mock_hdbscan``, but every third document comes back as noise."""
    state = {"call": 0}

    class FakeHDBSCAN:
        def __init__(self, **kw):
            pass

        def fit_predict(self, X):
            state["call"] += 1
            n = len(X)
            if state["call"] == 1:
                return np.array(
                    [-1 if i % 3 == 0 else i % n_clusters for i in range(n)],
                    dtype=int,
                )
            return np.array([i % 2 for i in range(n)], dtype=int)

    fake_mod = MagicMock()
    fake_mod.HDBSCAN = FakeHDBSCAN
    monkeypatch.setitem(__import__("sys").modules, "hdbscan", fake_mod)


@pytest.fixture()
def populated_with_noise(monkeypatch):
    """``populated``, but HDBSCAN leaves a third of the documents as noise."""
    doc_ids = _seed_documents(N_DOCS)
    _mock_chroma_with_docs(monkeypatch, doc_ids)
    _mock_umap(monkeypatch)
    _mock_hdbscan_with_noise(monkeypatch)
    _mock_llm(monkeypatch)
    return doc_ids


# ── engine.run_clustering ─────────────────────────────────────────────────────


class TestRunClustering:
    def test_returns_cluster_run_result(self, populated):
        from pka.clustering.engine import ClusterRunResult, run_clustering

        result = run_clustering(min_cluster_size=2)
        assert isinstance(result, ClusterRunResult)

    def test_run_id_persisted_to_db(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.run_id).where(cluster_runs.c.run_id == result.run_id)
            ).fetchone()
        assert row is not None

    def test_correct_cluster_count(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        assert result.diagnostics["n_l1_clusters"] == 4
        assert result.diagnostics["n_l2_clusters"] >= 2

    def test_cluster_rows_written(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count())
                .select_from(clusters)
                .where(clusters.c.run_id == result.run_id)
            ).scalar()
            l1 = con.execute(
                sa.select(sa.func.count())
                .select_from(clusters)
                .where((clusters.c.run_id == result.run_id) & (clusters.c.level == 1))
            ).scalar()
            l2 = con.execute(
                sa.select(sa.func.count())
                .select_from(clusters)
                .where((clusters.c.run_id == result.run_id) & (clusters.c.level == 2))
            ).scalar()
        assert l1 == 4
        assert l2 >= 2
        assert count == l1 + l2

    def test_assignments_written_for_all_docs(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            l1_count = con.execute(
                sa.select(sa.func.count())
                .select_from(cluster_assignments)
                .where(
                    (cluster_assignments.c.run_id == result.run_id)
                    & (cluster_assignments.c.level == 1)
                )
            ).scalar()
            l2_count = con.execute(
                sa.select(sa.func.count())
                .select_from(cluster_assignments)
                .where(
                    (cluster_assignments.c.run_id == result.run_id)
                    & (cluster_assignments.c.level == 2)
                )
            ).scalar()
        assert l1_count == N_DOCS
        assert l2_count == N_DOCS

    def test_l2_clusters_have_parent(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(clusters.c.parent_cluster_id).where(
                    (clusters.c.run_id == result.run_id) & (clusters.c.level == 2)
                )
            ).fetchall()
        assert rows
        assert all(r[0] is not None for r in rows)

    def test_run_not_accepted_by_default(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.accepted).where(cluster_runs.c.run_id == result.run_id)
            ).fetchone()
        assert not row[0]  # SQLite stores booleans as 0/1

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

    def test_cancel_during_multi_worker_labelling_does_not_block(self, monkeypatch):
        """
        A stop request during L2 labelling must not wait for every already-
        dispatched worker in the batch to finish (regression for the "Stop"
        button appearing to do nothing while other clusters keep labelling).
        """
        from pka.clustering import engine
        from pka.clustering.run_progress import ClusterRunCancelled, begin, request_cancel

        run_id = 999
        begin(run_id)
        monkeypatch.setattr(engine.cfg, "cluster_label_workers", 4)
        monkeypatch.setattr(
            engine,
            "sample_cluster_documents_for_clusters",
            lambda con, cluster_docs: {cid: [] for cid in cluster_docs},
        )

        release = threading.Event()

        def fake_label_one_cluster(cid, samples, skip_labelling, chat_model):
            if cid == 0:
                request_cancel(run_id)
            else:
                release.wait(2.0)
            return cid, f"label-{cid}", ""

        monkeypatch.setattr(engine, "_label_one_cluster", fake_label_one_cluster)
        cluster_docs = {cid: [cid] for cid in range(4)}

        start = time.perf_counter()
        with pytest.raises(ClusterRunCancelled):
            engine._label_clusters(
                cluster_docs, skip_labelling=False, chat_model=None, run_id=run_id
            )
        elapsed = time.perf_counter() - start

        release.set()  # let the still-running workers finish so no thread leaks
        assert elapsed < 1.0

    def test_umap_2d_shape(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        assert result.umap_2d.shape == (N_DOCS, 2)

    def test_diagnostics_keys_present(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        for key in (
            "n_clusters",
            "n_l1_clusters",
            "n_l2_clusters",
            "n_noise",
            "cluster_sizes",
            "size_max",
            "size_mean",
            "timings_ms",
        ):
            assert key in result.diagnostics

    def test_timings_ms_has_load_step(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        timings = result.diagnostics["timings_ms"]
        assert "load_embeddings_ms" in timings
        assert timings["load_embeddings_ms"] >= 0

    def test_parameters_stored_as_json(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=3)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.parameters, cluster_runs.c.algorithm).where(
                    cluster_runs.c.run_id == result.run_id
                )
            ).fetchone()
        params = json.loads(row[0])
        assert params["min_cluster_size"] == 3
        assert params["hierarchical"] is True
        assert params["cluster_space"] == "pca"
        assert row[1] == "HDBSCAN-hierarchical-pca"

    def test_empty_vector_store_raises(self, monkeypatch):
        col = MagicMock()
        col.count.return_value = 0
        col.get.return_value = {"ids": [], "embeddings": [], "metadatas": [], "documents": []}
        import pka.storage.vector_store as vs

        monkeypatch.setattr(vs, "get_collection", lambda: col)
        from pka.clustering.engine import run_clustering

        with pytest.raises(ValueError, match="empty"):
            run_clustering()


# ── engine.run_clustering(cluster_space="agglomerative") ─────────────────────
#
# Unlike HDBSCAN, agglomerative needs no mock: it's sklearn/scipy, already a
# hard dependency, so these run the real clusterer over the fixture — see
# planning/archive/AGGLOMERATIVE_CLUSTERING.md §7.


@pytest.fixture()
def populated_agglomerative(monkeypatch):
    """Seed DB + mock Chroma + mock viz UMAP + LLM. Real scipy clusterer."""
    doc_ids = _seed_documents(N_DOCS)
    _mock_chroma_with_docs(monkeypatch, doc_ids)
    _mock_umap(monkeypatch)
    _mock_llm(monkeypatch)
    return doc_ids


class TestRunClusteringAgglomerative:
    def test_run_persisted(self, populated_agglomerative):
        from pka.clustering.engine import run_clustering

        result = run_clustering(cluster_space="agglomerative", n_clusters=4)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.run_id).where(cluster_runs.c.run_id == result.run_id)
            ).fetchone()
        assert row is not None

    def test_n_clusters_yields_exact_l1_count(self, populated_agglomerative):
        from pka.clustering.engine import run_clustering

        result = run_clustering(cluster_space="agglomerative", n_clusters=4)
        assert result.diagnostics["n_l1_clusters"] == 4

        result5 = run_clustering(cluster_space="agglomerative", n_clusters=5)
        assert result5.diagnostics["n_l1_clusters"] == 5

    def test_cluster_rows_written_with_l2_parents(self, populated_agglomerative):
        from pka.clustering.engine import run_clustering

        result = run_clustering(cluster_space="agglomerative", n_clusters=4)
        with get_engine().connect() as con:
            l1 = con.execute(
                sa.select(sa.func.count())
                .select_from(clusters)
                .where((clusters.c.run_id == result.run_id) & (clusters.c.level == 1))
            ).scalar()
            l2_rows = con.execute(
                sa.select(clusters.c.parent_cluster_id).where(
                    (clusters.c.run_id == result.run_id) & (clusters.c.level == 2)
                )
            ).fetchall()
        assert l1 == 4
        assert l2_rows
        assert all(r[0] is not None for r in l2_rows)

    def test_no_noise_and_full_coverage(self, populated_agglomerative):
        """§3 invariant: agglomerative assigns every document; n_noise is 0."""
        from pka.clustering.engine import run_clustering

        result = run_clustering(cluster_space="agglomerative", n_clusters=4)
        assert result.n_noise == 0
        assert result.diagnostics["n_noise"] == 0
        assert -1 not in result.assignments.values()
        with get_engine().connect() as con:
            l1_count = con.execute(
                sa.select(sa.func.count())
                .select_from(cluster_assignments)
                .where(
                    (cluster_assignments.c.run_id == result.run_id)
                    & (cluster_assignments.c.level == 1)
                )
            ).scalar()
        assert l1_count == N_DOCS

    def test_l2_is_exact_refinement_of_l1(self, populated_agglomerative):
        """Every L2 cluster's members belong to the one L1 cluster it is
        recorded as a child of — the tree-cut invariant from §2.4/§7: L2 is a
        deeper cut of the same tree, not an independently rebuilt clustering.
        """
        from pka.clustering.engine import run_clustering

        result = run_clustering(cluster_space="agglomerative", n_clusters=4)
        with get_engine().connect() as con:
            l1_rows = con.execute(
                sa.select(
                    cluster_assignments.c.cluster_id, cluster_assignments.c.document_id
                ).where(
                    (cluster_assignments.c.run_id == result.run_id)
                    & (cluster_assignments.c.level == 1)
                )
            ).fetchall()
            l2_rows = con.execute(
                sa.select(
                    cluster_assignments.c.cluster_id, cluster_assignments.c.document_id
                ).where(
                    (cluster_assignments.c.run_id == result.run_id)
                    & (cluster_assignments.c.level == 2)
                )
            ).fetchall()
            parent_by_l2 = dict(
                con.execute(
                    sa.select(clusters.c.cluster_id, clusters.c.parent_cluster_id).where(
                        (clusters.c.run_id == result.run_id) & (clusters.c.level == 2)
                    )
                ).fetchall()
            )

        l1_by_doc = {doc_id: cid for cid, doc_id in l1_rows}
        l2_members: dict[int, set[int]] = {}
        for l2_cid, doc_id in l2_rows:
            l2_members.setdefault(l2_cid, set()).add(doc_id)

        assert l2_members  # the fixture must actually exercise L2
        for l2_cid, members in l2_members.items():
            parent_cid = parent_by_l2[l2_cid]
            assert {l1_by_doc[d] for d in members} == {parent_cid}

    def test_invalid_linkage_rejected(self, populated_agglomerative):
        from pka.clustering.engine import run_clustering

        with pytest.raises(ValueError, match="linkage"):
            run_clustering(cluster_space="agglomerative", linkage="centroid", n_clusters=4)

    def test_n_clusters_and_distance_threshold_conflict(self, populated_agglomerative):
        from pka.clustering.engine import run_clustering

        with pytest.raises(ValueError, match="Exactly one"):
            run_clustering(cluster_space="agglomerative", n_clusters=4, distance_threshold=1.0)

    def test_auto_k_sweep_clamped_to_corpus_and_recorded(self, populated_agglomerative):
        """A 20-doc fixture must not propose a k the corpus can't support, and
        the winning sweep must be recorded in ``params`` (§2.2c) so a bad
        auto-pick is diagnosable rather than invisible.
        """
        from pka.clustering.engine import run_clustering

        result = run_clustering(cluster_space="agglomerative")
        assert 2 <= result.diagnostics["n_l1_clusters"] <= N_DOCS - 1
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.parameters).where(cluster_runs.c.run_id == result.run_id)
            ).fetchone()
        params = json.loads(row[0])
        assert "k_sweep" in params
        assert all(int(k) <= N_DOCS - 1 for k in params["k_sweep"])


class TestAgglomerativeKCandidates:
    def test_clamped_below_corpus_size(self):
        from pka.clustering.engine import _agglomerative_k_candidates

        candidates = _agglomerative_k_candidates(40, k_min=8, k_max=40)
        assert max(candidates) <= 39
        assert min(candidates) >= 2

    def test_tiny_corpus_falls_back_to_two(self):
        from pka.clustering.engine import _agglomerative_k_candidates

        assert _agglomerative_k_candidates(3, k_min=8, k_max=40) == [2]


class TestSplitSubtreeMatchesRebuild:
    """§2.4's core claim: cutting the induced subtree equals rebuilding linkage
    on the group's slice. Pinned here since the whole L2 tree-reuse design rests
    on it (measured ARI 1.0 across ward/average/complete during planning).
    """

    @pytest.mark.parametrize("linkage_method", ["ward", "average", "complete"])
    def test_subtree_split_matches_independent_rebuild(self, linkage_method):
        from scipy.cluster.hierarchy import fcluster, leaders
        from scipy.cluster.hierarchy import linkage as scipy_linkage
        from sklearn.metrics import adjusted_rand_score

        from pka.clustering.engine import _build_linkage, _cut_linkage, _split_subtree

        rng = np.random.default_rng(3)
        n, k_l1, k_l2 = 300, 6, 4
        X = rng.random((n, 10)).astype(np.float32)

        Z, data = _build_linkage(X, linkage_method=linkage_method, metric="cosine")
        l1_labels = _cut_linkage(Z, n_clusters=k_l1)

        leader_nodes, leader_cids = leaders(Z, l1_labels)
        node_by_cid = dict(zip(leader_cids.tolist(), leader_nodes.tolist(), strict=False))

        for cid, node in node_by_cid.items():
            member_idx = np.where(l1_labels == cid)[0]
            if len(member_idx) < k_l2 + 1:
                continue
            groups = _split_subtree(Z, n, node, k_l2)
            assert sorted(i for g in groups for i in g) == sorted(member_idx.tolist())

            reused = np.empty(len(member_idx), dtype=int)
            pos = {idx: i for i, idx in enumerate(member_idx.tolist())}
            for gi, group in enumerate(groups):
                for leaf in group:
                    reused[pos[leaf]] = gi

            sub = data[member_idx]
            Zs = scipy_linkage(sub, method=linkage_method)
            rebuilt = fcluster(Zs, k_l2, criterion="maxclust")

            assert adjusted_rand_score(rebuilt, reused) == pytest.approx(1.0)


class TestAutoKAgglomerative:
    def test_recovers_planted_cluster_count_on_grid(self):
        """Silhouette sweep recovers a planted cluster count that is on the
        candidate grid — see planning/archive/AGGLOMERATIVE_CLUSTERING.md §2.2c on why
        the count must be on the grid for this assertion to mean anything.
        """
        from pka.clustering.engine import _auto_k_agglomerative, _build_linkage

        rng = np.random.default_rng(7)
        planted_k = 16
        n_per = 20
        dim = 20
        centres = rng.normal(scale=6.0, size=(planted_k, dim))
        X = np.concatenate(
            [centres[i] + rng.normal(scale=0.5, size=(n_per, dim)) for i in range(planted_k)]
        ).astype(np.float32)

        Z, data = _build_linkage(X, linkage_method="ward", metric="cosine")
        best_k, sweep = _auto_k_agglomerative(Z, data, k_min=8, k_max=40)
        assert best_k == planted_k
        assert sweep  # recorded in params so a bad pick is diagnosable


# ── lifecycle.accept_run / reject_run ─────────────────────────────────────────


class TestAcceptRejectRun:
    def _insert_run(self) -> int:
        with get_engine().begin() as con:
            res = con.execute(
                cluster_runs.insert().values(
                    timestamp=int(time.time()),
                    algorithm="HDBSCAN",
                    parameters="{}",
                    accepted=False,
                )
            )
        return res.inserted_primary_key[0]

    def test_accept_sets_flag(self):
        from pka.clustering.lifecycle import accept_run

        run_id = self._insert_run()
        accept_run(run_id)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.accepted).where(cluster_runs.c.run_id == run_id)
            ).fetchone()
        assert bool(row[0]) is True

    def test_reject_clears_flag(self):
        from pka.clustering.lifecycle import accept_run, reject_run

        run_id = self._insert_run()
        accept_run(run_id)
        reject_run(run_id, notes="too fragmented")
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.accepted).where(cluster_runs.c.run_id == run_id)
            ).fetchone()
        assert bool(row[0]) is False

    def test_accept_run_deactivates_others(self):
        """Accepting a run (even an older one) makes it the single active run."""
        from pka.clustering.lifecycle import accept_run, get_active_run_id

        r1 = self._insert_run()
        r2 = self._insert_run()
        accept_run(r2)
        accept_run(r1)
        assert get_active_run_id() == r1
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(cluster_runs.c.accepted).where(cluster_runs.c.run_id == r2)
            ).fetchone()
        assert not row[0]

    def test_get_active_run_id_none_when_none_accepted(self):
        from pka.clustering.lifecycle import get_active_run_id

        assert get_active_run_id() is None


# ── persisted cluster centroids ───────────────────────────────────────────────


class TestClusterCentroids:
    def test_l1_and_l2_centroids_written(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(clusters.c.centroid).where(clusters.c.run_id == result.run_id)
            ).fetchall()
        assert rows
        assert all(r[0] is not None for r in rows)

    def test_centroid_matches_mean_of_member_embeddings(self, monkeypatch):
        from pka.clustering.doc_embeddings import blob_to_embedding
        from pka.clustering.engine import run_clustering

        doc_ids = _seed_documents(N_DOCS)
        store, _col = _mock_chroma_with_docs(monkeypatch, doc_ids)
        _mock_umap(monkeypatch)
        _mock_hdbscan(monkeypatch, N_DOCS, n_clusters=4)
        _mock_llm(monkeypatch)
        emb_by_doc = {v["meta"]["document_id"]: np.array(v["embedding"]) for v in store.values()}

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(clusters.c.cluster_id, clusters.c.centroid).where(
                    (clusters.c.run_id == result.run_id) & (clusters.c.level == 1)
                )
            ).fetchone()
            cluster_id, blob = row
            member_ids = [
                r[0]
                for r in con.execute(
                    sa.select(cluster_assignments.c.document_id).where(
                        (cluster_assignments.c.cluster_id == cluster_id)
                        & (cluster_assignments.c.level == 1)
                    )
                ).fetchall()
            ]

        expected = np.mean([emb_by_doc[d] for d in member_ids], axis=0).astype(np.float32)
        np.testing.assert_allclose(blob_to_embedding(blob), expected, rtol=1e-5, atol=1e-6)

    def test_get_cluster_centroids_uses_persisted_value_without_chroma_call(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import _get_cluster_centroids

        result = run_clustering(min_cluster_size=2)

        from pka.storage import vector_store as vs

        col = vs.get_collection()
        call_count_before = col.get.call_count
        centroids = _get_cluster_centroids(result.run_id, level=1)
        assert col.get.call_count == call_count_before
        assert centroids

    def test_legacy_run_without_centroids_falls_back_and_backfills(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import _get_cluster_centroids

        result = run_clustering(min_cluster_size=2)
        # Simulate a pre-migration run: no persisted centroids.
        with get_engine().begin() as con:
            con.execute(
                clusters.update().where(clusters.c.run_id == result.run_id).values(centroid=None)
            )

        centroids = _get_cluster_centroids(result.run_id, level=1)
        assert centroids

        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(clusters.c.centroid).where(
                    (clusters.c.run_id == result.run_id) & (clusters.c.level == 1)
                )
            ).fetchall()
        assert all(r[0] is not None for r in rows)

    def test_purge_leaves_no_centroid_rows(self, populated):
        from pka.cli.purge_cluster_runs import purge_cluster_run
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        purge_cluster_run(result.run_id)
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count())
                .select_from(clusters)
                .where(clusters.c.run_id == result.run_id)
            ).scalar()
        assert count == 0


# ── purge_cluster_runs ────────────────────────────────────────────────────────


class TestPurgeClusterRuns:
    """``--force`` is the manual escape hatch for a run wedged at ``running``."""

    def _insert_run(self, status: str = "finished", *, accepted: bool = False) -> int:
        with get_engine().begin() as con:
            res = con.execute(
                cluster_runs.insert().values(
                    timestamp=int(time.time()),
                    algorithm="HDBSCAN",
                    parameters="{}",
                    accepted=accepted,
                    status=status,
                )
            )
        return res.inserted_primary_key[0]

    def _run_ids(self) -> list[int]:
        with get_engine().connect() as con:
            return [r[0] for r in con.execute(sa.select(cluster_runs.c.run_id)).fetchall()]

    def test_running_run_refused_without_force(self):
        from pka.cli.purge_cluster_runs import purge_cluster_run

        run_id = self._insert_run("running")
        with pytest.raises(ValueError, match="still running"):
            purge_cluster_run(run_id)
        assert self._run_ids() == [run_id]

    def test_force_deletes_running_run(self):
        from pka.cli.purge_cluster_runs import purge_cluster_run

        run_id = self._insert_run("running")
        purge_cluster_run(run_id, force=True)
        assert self._run_ids() == []

    def test_purge_all_skips_running_without_force(self):
        from pka.cli.purge_cluster_runs import purge_all_cluster_runs

        stale = self._insert_run("running")
        done = self._insert_run("finished")

        totals = purge_all_cluster_runs()

        assert totals["skipped_running"] == 1
        assert self._run_ids() == [stale]
        assert done not in self._run_ids()

    def test_purge_all_force_clears_running(self):
        from pka.cli.purge_cluster_runs import purge_all_cluster_runs

        self._insert_run("running")
        self._insert_run("finished", accepted=True)

        totals = purge_all_cluster_runs(force=True)

        assert totals["skipped_running"] == 0
        assert totals["runs"] == 2
        assert self._run_ids() == []


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


# ── The per-run noise bucket ──────────────────────────────────────────────────


class TestNoiseCluster:
    def _noise_row(self, run_id):
        with get_engine().connect() as con:
            return (
                con.execute(
                    sa.select(clusters).where(
                        (clusters.c.run_id == run_id) & (clusters.c.is_noise == True)  # noqa: E712 — SQLA expression
                    )
                )
                .mappings()
                .fetchone()
            )

    def test_run_creates_one_noise_cluster_holding_every_noise_doc(self, populated_with_noise):
        from pka.clustering.engine import NOISE_CLUSTER_LABEL, run_clustering

        result = run_clustering(min_cluster_size=2)
        assert result.n_noise > 0

        row = self._noise_row(result.run_id)
        assert row is not None
        assert row["label"] == NOISE_CLUSTER_LABEL
        assert row["level"] == 1
        assert row["parent_cluster_id"] is None
        # No centroid: a holding pen must never attract a document.
        assert row["centroid"] is None

        with get_engine().connect() as con:
            n = con.execute(
                sa.select(sa.func.count())
                .select_from(cluster_assignments)
                .where(cluster_assignments.c.cluster_id == row["cluster_id"])
            ).scalar()
        assert n == result.n_noise

    def test_run_without_noise_creates_no_noise_cluster(self, populated):
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        assert result.n_noise == 0
        assert self._noise_row(result.run_id) is None

    def test_noise_cluster_is_excluded_from_centroids(self, populated_with_noise):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import load_persisted_centroids

        result = run_clustering(min_cluster_size=2)
        noise_cid = self._noise_row(result.run_id)["cluster_id"]

        assert noise_cid not in load_persisted_centroids(result.run_id, level=1)

    def test_noise_cluster_is_not_counted_as_a_cluster(self, populated_with_noise):
        """``n_l1_clusters`` counts topics; the bucket is not one of them."""
        from pka.clustering.engine import run_clustering

        result = run_clustering(min_cluster_size=2)
        with get_engine().connect() as con:
            n_l1_rows = con.execute(
                sa.select(sa.func.count())
                .select_from(clusters)
                .where((clusters.c.run_id == result.run_id) & (clusters.c.level == 1))
            ).scalar()
        n_l1_topics = result.diagnostics["n_l1_clusters"]
        assert n_l1_rows == n_l1_topics + 1  # the topics, plus the bucket

    def test_relabelling_the_noise_bucket_is_refused(self, populated_with_noise):
        from pka.clustering.engine import relabel_single_cluster, run_clustering

        result = run_clustering(min_cluster_size=2)
        noise_cid = self._noise_row(result.run_id)["cluster_id"]

        with pytest.raises(ValueError, match="noise"):
            relabel_single_cluster(noise_cid, result.run_id)

    def test_run_relabelling_leaves_the_noise_bucket_alone(self, populated_with_noise):
        from pka.clustering.engine import NOISE_CLUSTER_LABEL, relabel_run_clusters, run_clustering

        result = run_clustering(min_cluster_size=2, skip_labelling=True)
        relabel_run_clusters(result.run_id)

        assert self._noise_row(result.run_id)["label"] == NOISE_CLUSTER_LABEL


# ── lifecycle.assign_new_docs ─────────────────────────────────────────────────


class TestAssignNewDocs:
    def _mock_get(self, store, col):
        def _get(where=None, include=None, limit=None, offset=0, **kwargs):
            doc_ids = None
            if where and "document_id" in where:
                doc_ids = set(where["document_id"]["$in"])
            items = [
                (k, v)
                for k, v in store.items()
                if doc_ids is None or v["meta"]["document_id"] in doc_ids
            ]
            items = items[offset : offset + limit] if limit is not None else items[offset:]
            return {
                "ids": [k for k, _ in items],
                "embeddings": [v["embedding"] for _, v in items],
                "metadatas": [v["meta"] for _, v in items],
                "documents": [v["text"] for _, v in items],
            }

        col.get.side_effect = _get

    def test_assigns_unassigned_docs(self, populated, monkeypatch):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, assign_new_docs
        from pka.db.queries import insert_chunks

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)

        new_id = make_document("zotero", "NEW001", "New doc", None, int(time.time()))
        insert_chunks(
            [
                {
                    "document_id": new_id,
                    "chunk_index": 0,
                    "text": "new content",
                    "token_count": 2,
                    "vector_id": f"vec-new-{new_id}",
                }
            ]
        )

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

    def test_noise_docs_are_never_refiled_into_real_clusters(
        self,
        populated_with_noise,
        monkeypatch,
    ):
        """The whole point of the noise bucket: documents HDBSCAN could not place
        keep a row of their own, so the incremental pass leaves them alone."""
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, assign_new_docs, noise_cluster_id

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        noise_cid = noise_cluster_id(result.run_id)
        assert noise_cid is not None

        with get_engine().connect() as con:
            before = set(
                r[0]
                for r in con.execute(
                    sa.select(cluster_assignments.c.document_id).where(
                        cluster_assignments.c.cluster_id == noise_cid
                    )
                ).fetchall()
            )
        assert len(before) == result.n_noise

        store, col = _mock_chroma_with_docs(monkeypatch, populated_with_noise)
        self._mock_get(store, col)
        stats = assign_new_docs(result.run_id)

        assert stats["assigned"] == 0  # nothing looks unassigned any more
        with get_engine().connect() as con:
            after = set(
                r[0]
                for r in con.execute(
                    sa.select(cluster_assignments.c.document_id).where(
                        cluster_assignments.c.cluster_id == noise_cid
                    )
                ).fetchall()
            )
            real = set(
                r[0]
                for r in con.execute(
                    sa.select(cluster_assignments.c.document_id).where(
                        (cluster_assignments.c.run_id == result.run_id)
                        & (cluster_assignments.c.level == 1)
                        & (cluster_assignments.c.cluster_id != noise_cid)
                    )
                ).fetchall()
            )
        assert after == before
        assert not (before & real)  # no noise document leaked into a real cluster

    def test_similarity_floor_sends_a_far_new_doc_to_noise(
        self,
        populated_with_noise,
        monkeypatch,
    ):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, assign_new_docs, noise_cluster_id
        from pka.config import settings
        from pka.db.queries import insert_chunks

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        noise_cid = noise_cluster_id(result.run_id)

        new_id = make_document("zotero", "FAR001", "Far doc", None, int(time.time()))
        insert_chunks(
            [
                {
                    "document_id": new_id,
                    "chunk_index": 0,
                    "text": "unrelated content",
                    "token_count": 2,
                    "vector_id": f"vec-far-{new_id}",
                }
            ]
        )
        store, col = _mock_chroma_with_docs(monkeypatch, populated_with_noise + [new_id])
        store[f"vec-far-{new_id}"] = {
            "embedding": [0.5] * FAKE_DIM,
            "text": "far",
            "meta": {"document_id": new_id, "source": "zotero", "chunk_index": 0},
        }
        self._mock_get(store, col)

        # A floor no cosine similarity can clear: every candidate is "too far".
        monkeypatch.setattr(settings, "cluster_assign_min_similarity", 1.1)
        stats = assign_new_docs(result.run_id)

        assert stats["noise"] == 1
        with get_engine().connect() as con:
            cid = con.execute(
                sa.select(cluster_assignments.c.cluster_id).where(
                    (cluster_assignments.c.document_id == new_id)
                    & (cluster_assignments.c.level == 1)
                )
            ).scalar()
        assert cid == noise_cid

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

    def test_small_corpus_minimums(self):
        from pka.clustering.engine import adaptive_cluster_params

        mcs, ms, nn = adaptive_cluster_params(5)
        assert mcs >= 2
        assert ms >= 2
        assert nn >= 2

    def test_min_cluster_size_stays_bounded_at_archive_scale(self):
        """Regression: a target-cluster-count capped at 12 used to make
        min_cluster_size scale ~linearly with n_docs (744 at 17.9k documents,
        with min_samples=372) instead of staying in a browsable absolute range.
        """
        from pka.clustering.engine import adaptive_cluster_params

        mcs, ms, _nn = adaptive_cluster_params(17_879)
        assert mcs <= 50
        assert ms <= 25

    def test_min_cluster_size_does_not_grow_past_the_cap(self):
        from pka.clustering.engine import adaptive_cluster_params

        mcs_18k, _ms, _nn = adaptive_cluster_params(18_000)
        mcs_180k, _ms, _nn = adaptive_cluster_params(180_000)
        assert mcs_18k == mcs_180k


class TestParseLlmJson:
    def test_strips_markdown_fence(self):
        from pka.clustering.engine import _parse_llm_json

        raw = '```json\n{"label": "Topic A"}\n```'
        assert _parse_llm_json(raw)["label"] == "Topic A"

    def test_regex_fallback_for_wrapped_json(self):
        from pka.clustering.engine import _parse_llm_json

        raw = 'Here is the result: {"label": "B"} thanks'
        assert _parse_llm_json(raw)["label"] == "B"

    def test_invalid_json_raises(self):
        import json

        import pytest

        from pka.clustering.engine import _parse_llm_json

        with pytest.raises(json.JSONDecodeError):
            _parse_llm_json("not json at all")


class TestRunUmapLegacy:
    def test_legacy_umap_shapes(self, monkeypatch):
        import numpy as np

        from pka.clustering.engine import _run_umap_legacy

        matrix = np.random.rand(20, 8).astype(np.float32)
        mock_reducer = MagicMock()
        mock_reducer.fit_transform.side_effect = [
            np.random.rand(20, 5).astype(np.float32),
            np.random.rand(20, 2).astype(np.float32),
        ]
        mock_umap = MagicMock()
        mock_umap.UMAP.return_value = mock_reducer
        monkeypatch.setitem(sys.modules, "umap", mock_umap)

        reduced_nd, reduced_2d = _run_umap_legacy(matrix, compute_2d=True)
        assert reduced_nd.shape == (20, 5)
        assert reduced_2d is not None
        assert reduced_2d.shape == (20, 2)


class TestIncrementalClustering:
    def test_no_active_run_triggers_full(self, populated):
        from pka.clustering.lifecycle import run_incremental_clustering

        out = run_incremental_clustering(min_cluster_size=2)
        assert out["action"] == "full_run"
        assert out["result"] is not None

    def test_assign_only_when_active_and_no_drift(self, populated):
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, run_incremental_clustering

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)
        out = run_incremental_clustering(min_cluster_size=2)
        assert out["action"] == "assign_only"
        assert out["assigned"] == 0

    def test_drift_is_reported_never_acted_on(self, populated, monkeypatch):
        """DESIGN.md §4: drift flags clusters for review, but never re-clusters."""
        from pka.clustering import lifecycle
        from pka.clustering.engine import run_clustering
        from pka.clustering.lifecycle import accept_run, run_incremental_clustering

        result = run_clustering(min_cluster_size=2)
        accept_run(result.run_id)

        monkeypatch.setattr(
            lifecycle,
            "compute_drift",
            lambda *a, **kw: [
                {"cluster_id": 1, "label": "x", "drift_score": 0.9, "n_recent": 3, "flagged": True}
            ],
        )
        # A full re-cluster would go through here; nothing may call it.
        monkeypatch.setattr(
            "pka.clustering.engine.run_clustering",
            lambda **kw: pytest.fail("drift must not trigger a re-cluster"),
        )

        out = run_incremental_clustering(min_cluster_size=2)

        assert out["action"] == "assign_only"
        assert out["run_id"] == result.run_id  # still the accepted run
        assert out["flagged"] == 1  # surfaced for the operator
        assert out["result"] is None


class TestEmbeddingsAvailable:
    def test_numpy_array_not_ambiguous(self):
        from pka.clustering.lifecycle import _embeddings_available

        assert _embeddings_available({"embeddings": np.random.rand(3, 8).astype(np.float32)})
        assert not _embeddings_available({"embeddings": np.array([])})
        assert not _embeddings_available({})
        assert not _embeddings_available({"embeddings": None})


class TestClusterLabellingSamples:
    def test_sample_cluster_documents_uses_card_summary(self):
        from pka.db.queries import get_engine, sample_cluster_documents, update_card_summary

        doc_id = make_document("zotero", "Z001", "Paper Title", None, int(time.time()))
        update_card_summary(doc_id, "Abstract about neural networks.")
        with get_engine().connect() as con:
            samples = sample_cluster_documents(con, [doc_id])
        assert len(samples) == 1
        assert samples[0][0] == "Paper Title"
        assert "neural" in samples[0][1].lower()

    def test_label_cluster_prompt_includes_excerpt(self, monkeypatch):
        from pka.clustering.engine import _label_cluster_with_llm

        captured: list[str] = []

        def fake_chat_json(prompt, model=None, timeout=90, *, temperature=None):
            captured.append(prompt)
            return {"label": "AI Topic", "description": "Desc."}, None

        monkeypatch.setattr("pka.ollama_chat.chat_json", fake_chat_json)
        _label_cluster_with_llm([("Title One", "Excerpt about graphs.")])
        assert "Excerpt: Excerpt about graphs" in captured[0]
        assert "Title: Title One" in captured[0]

    def test_l1_from_l2_children(self, monkeypatch):
        import numpy as np

        from pka.clustering.engine import L2ClusterBatch, _label_l1_clusters

        monkeypatch.setattr(
            "pka.clustering.engine._label_parent_from_children_with_llm",
            lambda labels, descs, model=None: ("Parent Topic", "Parent desc."),
        )
        l2_batches = [
            L2ClusterBatch(
                parent_l1_id=0,
                doc_ids=[1, 2],
                labels=np.array([0, 1]),
                label_map={0: "Child A", 1: "Child B"},
                desc_map={0: "a", 1: "b"},
            ),
        ]
        label_map, desc_map = _label_l1_clusters(
            {0: [1, 2]},
            l2_batches,
            skip_labelling=False,
            chat_model=None,
            run_id=None,
        )
        assert label_map[0] == "Parent Topic"
        assert desc_map[0] == "Parent desc."
