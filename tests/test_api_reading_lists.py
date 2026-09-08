"""``/reading-lists`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

from tests.api_seed import seed_docs

# ── Reading lists ─────────────────────────────────────────────────────────────


class TestReadingLists:
    def test_create_list(self, client):
        r = client.post("/reading-lists", json={"name": "My list"})
        assert r.status_code == 201
        assert "list_id" in r.json()

    def test_list_reading_lists(self, client):
        client.post("/reading-lists", json={"name": "A"})
        client.post("/reading-lists", json={"name": "B"})
        r = client.get("/reading-lists")
        assert len(r.json()) >= 2

    def test_add_and_retrieve_item(self, client):
        ids = seed_docs(1)
        list_id = client.post("/reading-lists", json={"name": "test"}).json()["list_id"]
        client.post(
            f"/reading-lists/{list_id}/items", json={"document_id": ids[0], "note": "read this"}
        )
        items = client.get(f"/reading-lists/{list_id}/items").json()
        assert len(items) == 1
        assert items[0]["doc_id"] == ids[0]
        assert items[0]["note"] == "read this"

    def test_remove_item(self, client):
        ids = seed_docs(1)
        list_id = client.post("/reading-lists", json={"name": "r"}).json()["list_id"]
        item_id = client.post(
            f"/reading-lists/{list_id}/items", json={"document_id": ids[0]}
        ).json()["id"]
        r = client.delete(f"/reading-lists/{list_id}/items/{item_id}")
        assert r.status_code == 204
        assert client.get(f"/reading-lists/{list_id}/items").json() == []

    def test_delete_list(self, client):
        list_id = client.post("/reading-lists", json={"name": "del"}).json()["list_id"]
        r = client.delete(f"/reading-lists/{list_id}")
        assert r.status_code == 204
        lists = client.get("/reading-lists").json()
        assert not any(item["list_id"] == list_id for item in lists)

    def test_items_order_by_position(self, client):
        ids = seed_docs(3)
        list_id = client.post("/reading-lists", json={"name": "order"}).json()["list_id"]
        for did in ids:
            client.post(f"/reading-lists/{list_id}/items", json={"document_id": did})
        items = client.get(f"/reading-lists/{list_id}/items").json()
        positions = [i["position"] for i in items]
        assert positions == sorted(positions)
