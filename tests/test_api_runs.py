"""``/runs`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

import time
from unittest.mock import MagicMock

import sqlalchemy as sa

from pka.db.schema import cluster_assignments, cluster_runs, clusters
from tests.api_seed import seed_docs, seed_run

# ── Runs ──────────────────────────────────────────────────────────────────────


class TestRuns:
    def test_list_runs_empty(self, client):
        r = client.get("/runs")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_runs_after_seed(self, client):
        ids = seed_docs(2)
        seed_run(ids)
        r = client.get("/runs")
        assert len(r.json()) >= 1

    def test_accept_run(self, client):
        ids = seed_docs(2)
        run_id = seed_run(ids)
        r = client.post(f"/runs/{run_id}/reject", params={"notes": "too fragmented"})
        assert r.status_code == 204
        r2 = client.post(f"/runs/{run_id}/accept")
        assert r2.status_code == 204

    def test_accept_older_run_switches_active(self, client):
        """Design §4.2.2: rollback = changing which run_id is marked active."""
        ids = seed_docs(2)
        old_run = seed_run(ids)
        new_run = seed_run(ids)
        assert client.post(f"/runs/{new_run}/accept").status_code == 204
        assert client.post(f"/runs/{old_run}/accept").status_code == 204
        runs = {r["run_id"]: r for r in client.get("/runs").json()}
        assert runs[old_run]["accepted"] is True
        assert runs[new_run]["accepted"] is False

    def test_accept_failed_run_409(self, client):
        from pka.db.queries import get_engine

        with get_engine().begin() as con:
            res = con.execute(
                cluster_runs.insert().values(
                    timestamp=int(time.time()),
                    algorithm="HDBSCAN",
                    parameters="{}",
                    accepted=False,
                    status="failed",
                )
            )
            run_id = res.inserted_primary_key[0]
        assert client.post(f"/runs/{run_id}/accept").status_code == 409
        assert client.post(f"/runs/{run_id}/reject").status_code == 409

    def test_diagnostics_200(self, client):
        ids = seed_docs(4)
        run_id = seed_run(ids, n_clusters=2)
        r = client.get(f"/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        data = r.json()
        assert "n_clusters" in data
        assert "drift_flags" in data
        assert "merge_suggestions" in data

    def test_diagnostics_404(self, client):
        assert client.get("/runs/99999/diagnostics").status_code == 404

    def test_list_runs_excludes_noise_bucket_from_n_clusters(self, client):
        ids = seed_docs(4)
        run_id = seed_run(ids[:2], n_clusters=2, noise_doc_ids=ids[2:])
        row = next(r for r in client.get("/runs").json() if r["run_id"] == run_id)
        assert row["n_clusters"] == 2  # the two real clusters, not the bucket too
        assert row["n_noise"] == 2  # the documents held in the bucket

    def test_diagnostics_excludes_noise_bucket_from_cluster_sizes(self, client):
        ids = seed_docs(4)
        run_id = seed_run(ids[:2], n_clusters=2, noise_doc_ids=ids[2:])
        data = client.get(f"/runs/{run_id}/diagnostics").json()
        assert data["n_clusters"] == 2
        assert data["n_noise"] == 2
        assert len(data["cluster_sizes"]) == 2  # the noise bucket's size is not one of them

    def test_trigger_run_queued(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        monkeypatch.setattr(
            "pka.clustering.engine.run_clustering",
            lambda params=None, **kw: MagicMock(run_id=99, n_clusters=3, n_noise=1),
        )
        r = client.post("/runs/trigger")
        assert r.status_code == 202
        data = r.json()
        assert data["status"] == "queued"
        assert isinstance(data["run_id"], int)

    def test_trigger_run_body_reaches_run_clustering(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        calls = []
        monkeypatch.setattr(
            "pka.clustering.engine.run_clustering",
            lambda params=None, **kw: (
                calls.append((params, kw)),
                MagicMock(run_id=99, n_clusters=3, n_noise=1),
            )[1],
        )
        body = {
            "cluster_space": "legacy_umap",
            "min_cluster_size": 10,
            "min_samples": 5,
            "n_neighbors": 20,
            "min_dist": 0.2,
            "n_components": 8,
            "skip_labelling": True,
            "async_labelling": True,
        }
        r = client.post("/runs/trigger", json=body)
        assert r.status_code == 202
        assert len(calls) == 1
        params, kw = calls[0]
        assert params.cluster_space == "legacy_umap"
        assert params.min_cluster_size == 10
        assert params.min_samples == 5
        assert params.n_neighbors == 20
        assert params.min_dist == 0.2
        assert params.n_components == 8
        assert params.skip_labelling is True
        assert params.async_labelling is True

        run_row = next(x for x in client.get("/runs").json() if x["run_id"] == kw["run_id"])
        assert run_row["algorithm"] == "HDBSCAN-hierarchical"

    def test_trigger_run_rejects_out_of_range_params(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post("/runs/trigger", json={"min_cluster_size": 1})
        assert r.status_code == 422
        r = client.post("/runs/trigger", json={"min_dist": 1.5})
        assert r.status_code == 422
        r = client.post("/runs/trigger", json={"cluster_space": "kmeans"})
        assert r.status_code == 422

    def test_trigger_run_accepts_agglomerative_cluster_space(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        calls = []
        monkeypatch.setattr(
            "pka.clustering.engine.run_clustering",
            lambda params=None, **kw: (
                calls.append((params, kw)),
                MagicMock(run_id=99, n_clusters=3, n_noise=0),
            )[1],
        )
        body = {"cluster_space": "agglomerative", "linkage": "average", "n_clusters": 6}
        r = client.post("/runs/trigger", json=body)
        assert r.status_code == 202
        assert len(calls) == 1
        params, kw = calls[0]
        assert params.cluster_space == "agglomerative"
        assert params.linkage == "average"
        assert params.n_clusters == 6
        assert params.distance_threshold is None

        run_row = next(x for x in client.get("/runs").json() if x["run_id"] == kw["run_id"])
        assert run_row["algorithm"] == "agglomerative-hierarchical"

    def test_trigger_run_rejects_n_clusters_below_two(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post("/runs/trigger", json={"cluster_space": "agglomerative", "n_clusters": 1})
        assert r.status_code == 422

    def test_trigger_run_rejects_n_clusters_and_distance_threshold_together(
        self, client, monkeypatch
    ):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post(
            "/runs/trigger",
            json={
                "cluster_space": "agglomerative",
                "n_clusters": 6,
                "distance_threshold": 1.5,
            },
        )
        assert r.status_code == 422

    def test_trigger_run_rejects_bad_linkage(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post(
            "/runs/trigger", json={"cluster_space": "agglomerative", "linkage": "centroid"}
        )
        assert r.status_code == 422

    def test_list_runs_includes_status(self, client):
        ids = seed_docs(2)
        run_id = seed_run(ids)
        r = client.get("/runs")
        assert r.status_code == 200
        row = next(x for x in r.json() if x["run_id"] == run_id)
        assert row["status"] == "finished"

    def _seed_running_run(self) -> int:
        """A row in the exact state ``trigger_run`` leaves before its worker starts."""
        from pka.db.queries import get_engine

        with get_engine().begin() as con:
            res = con.execute(
                cluster_runs.insert().values(
                    timestamp=int(time.time()),
                    algorithm="HDBSCAN",
                    parameters="{}",
                    accepted=False,
                    status="running",
                )
            )
        return res.inserted_primary_key[0]

    def test_cancel_run_flags_live_worker(self, client):
        """With a worker alive, cancel sets the flag for it to observe."""
        from pka.clustering.run_progress import begin, check_cancel, finish

        run_id = self._seed_running_run()
        begin(run_id)
        try:
            cancel = client.post(f"/runs/{run_id}/cancel")
            assert cancel.status_code == 202
            assert cancel.json()["status"] == "cancel_requested"
            assert check_cancel(run_id)
        finally:
            finish(run_id)

    def test_cancel_run_reconciles_dead_worker(self, client):
        """No worker left to observe the flag, so the row is cleared outright."""
        run_id = self._seed_running_run()

        cancel = client.post(f"/runs/{run_id}/cancel")
        assert cancel.status_code == 202
        assert cancel.json()["status"] == "reconciled"

        row = next(x for x in client.get("/runs").json() if x["run_id"] == run_id)
        assert row["status"] == "failed"

    def test_cancelled_run_no_longer_blocks_trigger(self, client, monkeypatch):
        """The payoff: clearing a stranded run unblocks the next one."""
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        monkeypatch.setattr(
            "pka.clustering.engine.run_clustering",
            lambda params=None, **kw: MagicMock(run_id=kw["run_id"], n_clusters=1, n_noise=0),
        )

        stranded = self._seed_running_run()
        assert client.post("/runs/trigger").status_code == 409

        client.post(f"/runs/{stranded}/cancel")
        assert client.post("/runs/trigger").status_code == 202

    def test_cancel_run_not_running(self, client):
        ids = seed_docs(2)
        run_id = seed_run(ids)
        r = client.post(f"/runs/{run_id}/cancel")
        assert r.status_code == 409

    def test_trigger_run_rejects_empty_chroma(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 0
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post("/runs/trigger")
        assert r.status_code == 400

    def test_trigger_run_rejects_too_few_vectors(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 3
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post("/runs/trigger")
        assert r.status_code == 400

    def test_delete_run(self, client):
        from pka.db.queries import get_engine

        ids = seed_docs(4)
        run_id = seed_run(ids, n_clusters=2, with_l2=True)
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update().where(cluster_runs.c.run_id == run_id).values(accepted=False)
            )

        r = client.delete(f"/runs/{run_id}")
        assert r.status_code == 204

        assert client.get(f"/runs/{run_id}/diagnostics").status_code == 404
        with get_engine().connect() as con:
            n_assign = con.execute(
                sa.select(sa.func.count())
                .select_from(cluster_assignments)
                .where(cluster_assignments.c.run_id == run_id)
            ).scalar()
            n_clusters = con.execute(
                sa.select(sa.func.count()).select_from(clusters).where(clusters.c.run_id == run_id)
            ).scalar()
        assert n_assign == 0
        assert n_clusters == 0

    def test_delete_run_404(self, client):
        assert client.delete("/runs/99999").status_code == 404

    def test_delete_run_running_409(self, client):
        from pka.db.queries import get_engine

        ids = seed_docs(2)
        run_id = seed_run(ids)
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update()
                .where(cluster_runs.c.run_id == run_id)
                .values(status="running", accepted=False)
            )
        r = client.delete(f"/runs/{run_id}")
        assert r.status_code == 409

    def test_delete_run_accepted_requires_force(self, client):
        ids = seed_docs(2)
        run_id = seed_run(ids)  # seed_run always creates accepted=True

        r = client.delete(f"/runs/{run_id}")
        assert r.status_code == 409

        r = client.delete(f"/runs/{run_id}?force=true")
        assert r.status_code == 204
