"""``/settings`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""


class TestSettings:
    def test_get_settings_returns_groups_and_capabilities(self, client, monkeypatch):
        from pka.config import settings

        # Non-empty so chat resolution never probes localhost:11434 in this test.
        monkeypatch.setattr(settings, "chat_model", "llama3")

        r = client.get("/settings")
        assert r.status_code == 200
        body = r.json()
        assert {g["name"] for g in body["groups"]} == set(
            [
                "Providers",
                "Outbound",
                "Images",
                "Fetch",
                "Embedding",
                "Chunking",
                "Tags",
                "Clustering",
                "Storage",
                "Dev",
            ]
        )
        assert {c["capability"] for c in body["capabilities"]} == {
            "chat",
            "vision",
            "gate_vision",
            "ocr",
            "image_embed",
        }

    def test_probe_local_capability(self, client):
        r = client.post("/settings/probe/image_embed")
        assert r.status_code == 200
        assert r.json()["reachable"] is True

    def test_probe_unknown_capability_400(self, client):
        r = client.post("/settings/probe/nope")
        assert r.status_code == 400
