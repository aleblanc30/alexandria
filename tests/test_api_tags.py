"""``/tags`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

from tests.api_seed import seed_docs, seed_run

# ── Tags ──────────────────────────────────────────────────────────────────────


class TestTags:
    def test_returns_list(self, client):
        r = client.get("/tags")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_filter_by_origin(self, client):
        ids = seed_docs(1)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["manual-tag"], "remove": []})
        r = client.get("/tags?origin=manual")
        tags = [t["tag"] for t in r.json()]
        assert "manual-tag" in tags

    def test_query_filter(self, client):
        ids = seed_docs(1)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["unique-xyz"], "remove": []})
        r = client.get("/tags?q=unique-xyz")
        assert any("unique-xyz" in t["tag"] for t in r.json())

    def test_filter_by_document_source(self, client):
        from pka.db.queries import insert_source_tags

        ids = seed_docs(3)
        insert_source_tags(ids[0], ["zotero-only"], source="zotero")
        insert_source_tags(ids[1], ["firefox-only"], source="firefox")

        r = client.get("/tags", params=[("sources", "firefox"), ("origin", "source")])
        tags = [t["tag"] for t in r.json()]
        assert "firefox-only" in tags
        assert "zotero-only" not in tags

    def test_filter_by_selected_tags_shows_cooccurring_only(self, client):
        from pka.db.queries import insert_source_tags

        ids = seed_docs(3)
        insert_source_tags(ids[0], ["ml", "python"], source="zotero")
        insert_source_tags(ids[1], ["ml"], source="firefox")
        insert_source_tags(ids[2], ["python"], source="calibre")

        all_source = [t["tag"] for t in client.get("/tags?origin=source").json()]
        assert set(all_source) >= {"ml", "python"}

        scoped = client.get("/tags", params=[("origin", "source"), ("source_tags", "python")])
        scoped_tags = [t["tag"] for t in scoped.json()]
        assert "python" in scoped_tags
        assert "ml" in scoped_tags
        assert len(scoped_tags) == 2

        ml_only = client.get("/tags", params=[("origin", "source"), ("source_tags", "ml")])
        ml_tags = [t["tag"] for t in ml_only.json()]
        assert set(ml_tags) == {"ml", "python"}

        restored = [t["tag"] for t in client.get("/tags?origin=source").json()]
        assert set(restored) >= {"ml", "python"}

    def test_filter_by_cluster_l1_l2_origin(self, client):
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2, with_l2=True)
        l1 = next(c for c in client.get("/clusters").json() if c["level"] == 1)
        l2 = next(c for c in client.get("/clusters").json() if c["level"] == 2)
        l1_tag = client.post(
            f"/clusters/{l1['cluster_id']}/apply-tag",
            json={"tag": "topic-l1"},
        ).json()["tag"]
        l2_tag = client.post(
            f"/clusters/{l2['cluster_id']}/apply-tag",
            json={"tag": "topic-l2"},
        ).json()["tag"]

        l1_tags = [t["tag"] for t in client.get("/tags?origin=cluster_l1").json()]
        l2_tags = [t["tag"] for t in client.get("/tags?origin=cluster_l2").json()]
        assert l1_tag in l1_tags
        assert l2_tag in l2_tags
        assert l2_tag not in l1_tags
