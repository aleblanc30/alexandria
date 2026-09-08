"""``/trends`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

from tests.api_seed import seed_docs, seed_run

# ── Trends ────────────────────────────────────────────────────────────────────


class TestTrends:
    def test_timeline_returns_dict(self, client):
        r = client.get("/trends/timeline")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "timeline" in data
        assert "sizes" in data

    def test_sources_over_time_returns_dict(self, client):
        seed_docs(3)
        r = client.get("/trends/sources")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_sources_contains_expected_keys(self, client):
        seed_docs(3)
        r = client.get("/trends/sources")
        sources = set(r.json().keys())
        assert sources & {"zotero", "firefox", "calibre"}

    def test_timeline_with_cluster_run(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2)
        r = client.get("/trends/timeline")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["timeline"], dict)
        assert isinstance(data["sizes"], dict)

    def test_timeline_excludes_noise_bucket(self, client):
        """The noise bucket is not a topic; it must not appear as a trend line."""
        ids = seed_docs(4)
        seed_run(ids[:2], n_clusters=2, noise_doc_ids=ids[2:])
        data = client.get("/trends/timeline").json()
        assert "Unclustered" not in data["sizes"]
        assert "Unclustered" not in data["timeline"]
        assert sum(data["sizes"].values()) >= 1
        assert any(data["timeline"].values())

    def test_timeline_kernel_values_are_floats(self, client):
        ids = seed_docs(2)
        seed_run(ids)
        r = client.get("/trends/timeline")
        data = r.json()
        for periods in data["timeline"].values():
            for value in periods.values():
                assert isinstance(value, float)

    def test_timeline_excludes_level2_clusters(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2, with_l2=True)
        data = client.get("/trends/timeline").json()
        labels = set(data["timeline"].keys())
        assert "Subcluster 0" not in labels
        assert labels <= {"Cluster 0", "Cluster 1"}
