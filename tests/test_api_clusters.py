"""``/clusters`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

import sqlalchemy as sa

from pka.db.schema import cluster_runs, clusters
from tests.api_seed import seed_docs, seed_run

# ── Clusters ──────────────────────────────────────────────────────────────────


class TestClusters:
    def test_returns_empty_without_active_run(self, client):
        r = client.get("/clusters")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_clusters_with_active_run(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2)
        r = client.get("/clusters")
        data = r.json()
        assert len(data) == 2
        for key in ("cluster_id", "label", "level", "doc_count"):
            assert key in data[0]

    def test_cluster_detail_200(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2)
        clusters_list = client.get("/clusters").json()
        cid = clusters_list[0]["cluster_id"]
        r = client.get(f"/clusters/{cid}")
        assert r.status_code == 200
        data = r.json()
        assert "top_tags" in data
        assert "label" in data

    def test_patch_cluster_label(self, client):
        ids = seed_docs(2)
        seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.patch(f"/clusters/{cid}", json={"label": "Custom Topic Name"})
        assert r.status_code == 200
        assert r.json()["label"] == "Custom Topic Name"
        listed = client.get("/clusters").json()
        assert next(c for c in listed if c["cluster_id"] == cid)["label"] == "Custom Topic Name"

    def test_regenerate_label(self, client, monkeypatch):
        ids = seed_docs(2)
        seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        monkeypatch.setattr(
            "pka.clustering.labelling._label_cluster_with_llm",
            lambda samples, model=None, **kw: ("Regenerated Topic", "A description."),
        )
        r = client.post(f"/clusters/{cid}/regenerate-label")
        assert r.status_code == 200
        assert r.json()["label"] == "Regenerated Topic"
        assert r.json()["description"] == "A description."

    def test_regenerate_label_passes_temperature(self, client, monkeypatch):
        captured: dict = {}

        def fake_chat_json(prompt, model=None, timeout=90, *, temperature=None):
            captured["temperature"] = temperature
            return {"label": "New Label", "description": "New desc"}, None

        monkeypatch.setattr("pka.ollama_chat.chat_json", fake_chat_json)
        ids = seed_docs(2)
        seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.post(f"/clusters/{cid}/regenerate-label")
        assert r.status_code == 200
        assert captured.get("temperature") == 0.85

    def test_apply_tag_uses_cluster_label(self, client):
        ids = seed_docs(3)
        seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        from pka.db.queries import get_engine

        with get_engine().begin() as con:
            con.execute(
                clusters.update()
                .where(clusters.c.cluster_id == cid)
                .values(label="Distributed Systems")
            )
        r = client.post(f"/clusters/{cid}/apply-tag", json={})
        assert r.status_code == 200
        assert r.json()["tag"] == "distributed-systems"

    def test_apply_tag_to_cluster(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.post(f"/clusters/{cid}/apply-tag", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["cluster_id"] == cid
        assert data["applied"] >= 1
        assert data["tag"]

        from pka.db.queries import get_engine
        from pka.db.schema import overlay_tags

        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(overlay_tags.c.tag, overlay_tags.c.origin).where(
                    overlay_tags.c.tag == data["tag"]
                )
            ).fetchall()
        assert len(rows) >= 1
        assert all(r[1] == "cluster_l1" for r in rows)

    def test_apply_l2_tag_uses_cluster_l2_origin(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2, with_l2=True)
        l2 = next(c for c in client.get("/clusters").json() if c["level"] == 2)
        r = client.post(
            f"/clusters/{l2['cluster_id']}/apply-tag",
            json={"tag": "subtopic-a"},
        )
        assert r.status_code == 200
        from pka.db.queries import get_engine
        from pka.db.schema import overlay_tags

        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(overlay_tags.c.origin).where(overlay_tags.c.tag == r.json()["tag"])
            ).fetchall()
        assert rows
        assert all(row[0] == "cluster_l2" for row in rows)

    def test_apply_tag_idempotent(self, client):
        ids = seed_docs(3)
        seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        first = client.post(f"/clusters/{cid}/apply-tag", json={}).json()
        second = client.post(f"/clusters/{cid}/apply-tag", json={}).json()
        assert second["applied"] == 0
        assert second["skipped"] == first["applied"]

    def test_apply_all_tags(self, client):
        ids = seed_docs(6)
        seed_run(ids, n_clusters=3)
        r = client.post("/clusters/apply-all-tags")
        assert r.status_code == 200
        data = r.json()
        assert len(data["clusters"]) == 3
        assert data["total_applied"] >= 3

    def test_apply_all_tags_no_active_run(self, client):
        assert client.post("/clusters/apply-all-tags").status_code == 404

    def test_cluster_404(self, client):
        assert client.get("/clusters/99999").status_code == 404

    def test_noise_bucket_is_flagged_and_sorted_last(self, client):
        ids = seed_docs(4)
        seed_run(ids[:2], n_clusters=2, noise_doc_ids=ids[2:])
        data = client.get("/clusters").json()
        assert [c["is_noise"] for c in data] == [False, False, True]
        noise = data[-1]
        assert noise["label"] == "Unclustered"
        assert noise["doc_count"] == 2

    def test_apply_all_tags_skips_noise_bucket(self, client):
        ids = seed_docs(4)
        seed_run(ids[:2], n_clusters=2, noise_doc_ids=ids[2:])
        r = client.post("/clusters/apply-all-tags")
        assert r.status_code == 200
        assert len(r.json()["clusters"]) == 2  # the two real clusters only

    def test_apply_tag_refused_on_noise_bucket(self, client):
        ids = seed_docs(4)
        seed_run(ids[:2], n_clusters=2, noise_doc_ids=ids[2:])
        noise_cid = next(c for c in client.get("/clusters").json() if c["is_noise"])["cluster_id"]
        r = client.post(f"/clusters/{noise_cid}/apply-tag", json={})
        assert r.status_code == 400

    def test_regenerate_label_refused_on_noise_bucket(self, client):
        ids = seed_docs(4)
        seed_run(ids[:2], n_clusters=2, noise_doc_ids=ids[2:])
        noise_cid = next(c for c in client.get("/clusters").json() if c["is_noise"])["cluster_id"]
        r = client.post(f"/clusters/{noise_cid}/regenerate-label")
        assert r.status_code == 400

    def test_cluster_documents(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.get(f"/clusters/{cid}/documents")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_scatter_points_with_umap(self, client):
        import json

        from pka.db.queries import get_engine

        ids = seed_docs(3)
        run_id = seed_run(ids, n_clusters=2)
        points = [{"doc_id": ids[0], "x": 1.5, "y": -0.5, "cluster_id": 0}]
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update()
                .where(cluster_runs.c.run_id == run_id)
                .values(accepted=True, umap_points=json.dumps(points))
            )
        r = client.get("/clusters/scatter/points")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["doc_id"] == ids[0]

    def test_scatter_empty_without_umap(self, client):
        ids = seed_docs(2)
        run_id = seed_run(ids)
        from pka.db.queries import get_engine

        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update().where(cluster_runs.c.run_id == run_id).values(accepted=True)
            )
        assert client.get("/clusters/scatter/points").json() == []
