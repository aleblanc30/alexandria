"""``/images`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

import pytest

from tests.api_seed import image_document_id, seed_image

# ── Images ────────────────────────────────────────────────────────────────────


class TestImages:
    def test_list_images_empty(self, client):
        r = client.get("/images")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_images_returns_seeded(self, client):
        seed_image()
        r = client.get("/images")
        assert len(r.json()) == 1
        assert r.json()[0]["filename"] == "slide.png"
        assert "ml" in r.json()[0]["tags"]

    def test_list_images_filter_by_type(self, client):
        seed_image()
        r = client.get("/images?image_type=slide")
        assert len(r.json()) == 1
        r2 = client.get("/images?image_type=poster")
        assert r2.json() == []

    def test_get_image_by_id(self, client):
        image_id = seed_image()
        r = client.get(f"/images/{image_id}")
        assert r.status_code == 200
        assert r.json()["image_type"] == "slide"

    def test_get_image_404(self, client):
        assert client.get("/images/99999").status_code == 404

    def test_get_image_file_serves_bytes(self, client, tmp_path):
        from pka.db.queries import get_engine
        from pka.db.schema import images as images_tbl

        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        with get_engine().begin() as con:
            image_id = con.execute(
                images_tbl.insert().values(
                    path=str(img),
                    filename="pic.png",
                    image_type="slide",
                )
            ).inserted_primary_key[0]

        r = client.get(f"/images/{image_id}/file")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content == b"\x89PNG\r\n\x1a\nfake"

    def test_get_image_file_missing_on_disk(self, client):
        # seed_image points at /tmp/slide.png, which does not exist on disk.
        image_id = seed_image()
        assert client.get(f"/images/{image_id}/file").status_code == 404

    def test_get_image_file_unknown_id(self, client):
        assert client.get("/images/99999/file").status_code == 404

    def test_search_images(self, client, monkeypatch):
        seed_image()
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_text",
            lambda q, n=10: [
                {
                    "vector_id": "clip-1",
                    "filename": "slide.png",
                    "path": "/tmp/slide.png",
                    "image_type": "slide",
                    "distance": 0.15,
                }
            ],
        )
        r = client.get("/images/search?q=neural")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["similarity"] == pytest.approx(0.85)
        assert r.json()[0]["matched_by"] == "clip"

    def test_search_images_by_inferred_text(self, client, monkeypatch):
        """With CLIP off (the default) images are still found by their own text."""
        image_id = seed_image()
        doc_id = image_document_id(image_id)
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_inferred_text",
            lambda q, n=10: [
                {
                    "vector_id": "chunk-1",
                    "document_id": doc_id,
                    "distance": 0.3,
                    "text": "Neural networks overview",
                    "pass": None,
                    "filename": "slide.png",
                }
            ],
        )
        r = client.get("/images/search?q=neural")
        body = r.json()
        assert [h["id"] for h in body] == [image_id]
        assert body[0]["matched_by"] == "text"
        assert body[0]["similarity"] == pytest.approx(0.7)

    def test_search_images_merges_both_paths(self, client, monkeypatch):
        """One image found by both paths is returned once, at its better score."""
        image_id = seed_image()
        doc_id = image_document_id(image_id)
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_text",
            lambda q, n=10: [{"vector_id": "clip-1", "distance": 0.6}],
        )
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_inferred_text",
            lambda q, n=10: [
                {
                    "vector_id": "chunk-1",
                    "document_id": doc_id,
                    "distance": 0.1,
                    "text": "",
                    "pass": None,
                    "filename": "slide.png",
                }
            ],
        )
        body = client.get("/images/search?q=neural").json()
        assert len(body) == 1
        assert body[0]["matched_by"] == "clip+text"
        assert body[0]["similarity"] == pytest.approx(0.9)

    def test_search_images_mode_text_skips_clip(self, client, monkeypatch):
        called = {"clip": 0}

        def _clip(q, n=10):
            called["clip"] += 1
            return []

        monkeypatch.setattr("pka.ingestion.image_pipeline.search_images_by_text", _clip)
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_inferred_text",
            lambda q, n=10: [],
        )
        assert client.get("/images/search?q=neural&mode=text").status_code == 200
        assert called["clip"] == 0

    def test_search_images_rejects_unknown_mode(self, client):
        assert client.get("/images/search?q=neural&mode=magic").status_code == 422

    def test_search_images_skips_pending_image(self, client, monkeypatch):
        """A registered-but-not-yet-ingested image never surfaces in results."""
        image_id = seed_image()
        doc_id = image_document_id(image_id)
        from pka.db.queries import get_engine
        from pka.db.schema import images as images_tbl

        with get_engine().begin() as con:
            con.execute(
                images_tbl.update().where(images_tbl.c.id == image_id).values(indexed_at=None)
            )
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_inferred_text",
            lambda q, n=10: [
                {
                    "vector_id": "chunk-1",
                    "document_id": doc_id,
                    "distance": 0.1,
                    "text": "",
                    "pass": None,
                    "filename": "slide.png",
                }
            ],
        )
        assert client.get("/images/search?q=neural").json() == []
