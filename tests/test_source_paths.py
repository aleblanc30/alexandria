"""Per-source path config: GET/PUT the folder or database path, native browse."""
import pytest
from fastapi.testclient import TestClient

from pka.db.queries import init_db


@pytest.fixture()
def client(empty_vector_store, monkeypatch, tmp_path):
    from pka.api import source_paths
    monkeypatch.setattr(source_paths, "ENV_FILE_PATH", tmp_path / ".env")

    init_db()
    from pka.api.main import app
    return TestClient(app, raise_server_exceptions=True)


class TestGetPath:
    def test_returns_configured_path(self, client):
        r = client.get("/ingestion/sources/zotero/path")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "zotero"
        assert body["kind"] == "file"
        assert body["exists"] is False  # isolated_settings points at a nonexistent tmp path

    def test_unknown_source_404(self, client):
        assert client.get("/ingestion/sources/nope/path").status_code == 400

    def test_reflects_existing_path(self, client, tmp_path):
        folder = tmp_path / "images"
        folder.mkdir()
        from pka.config import settings
        settings.images_dir = folder
        r = client.get("/ingestion/sources/image/path")
        assert r.json()["exists"] is True


class TestUpdatePath:
    def test_updates_running_settings(self, client, tmp_path):
        new_dir = tmp_path / "my-books"
        new_dir.mkdir()
        r = client.put("/ingestion/sources/calibre/path", json={"path": str(new_dir)})
        assert r.status_code == 200
        body = r.json()
        assert body["exists"] is True
        assert body["path"] == str(new_dir)

        from pka.config import settings
        assert settings.book_archive == new_dir

    def test_persists_to_env_file(self, client, tmp_path):
        from pka.api import source_paths
        new_dir = tmp_path / "my-images"
        client.put("/ingestion/sources/image/path", json={"path": str(new_dir)})

        contents = source_paths.ENV_FILE_PATH.read_text(encoding="utf-8")
        assert "ALEXANDRIA_IMAGES_DIR" in contents
        assert str(new_dir) in contents

    def test_rewrites_existing_key_in_place(self, client, tmp_path):
        from pka.api import source_paths
        source_paths.ENV_FILE_PATH.write_text(
            "ALEXANDRIA_DEV=1\nALEXANDRIA_IMAGES_DIR='/old/path'\nALEXANDRIA_CHAT_MODEL=foo\n",
            encoding="utf-8",
        )
        new_dir = tmp_path / "new-images"
        client.put("/ingestion/sources/image/path", json={"path": str(new_dir)})

        lines = source_paths.ENV_FILE_PATH.read_text(encoding="utf-8").splitlines()
        assert lines.count("ALEXANDRIA_DEV=1") == 1
        assert "ALEXANDRIA_CHAT_MODEL=foo" in lines
        assert sum(1 for line in lines if line.startswith("ALEXANDRIA_IMAGES_DIR=")) == 1

    def test_empty_path_rejected(self, client):
        r = client.put("/ingestion/sources/zotero/path", json={"path": "  "})
        assert r.status_code == 400

    def test_validation_error_surfaces_as_400(self, client, monkeypatch):
        from pka.api import source_paths

        def _reject(_v):
            raise ValueError("Refusing to index system path")

        monkeypatch.setattr(source_paths, "reject_system_path", _reject)
        r = client.put("/ingestion/sources/zotero/path", json={"path": "/etc/passwd"})
        assert r.status_code == 400

    def test_unknown_source_400(self, client):
        r = client.put("/ingestion/sources/nope/path", json={"path": "/tmp/x"})
        assert r.status_code == 400


class TestBrowsePath:
    def test_returns_chosen_path(self, client, monkeypatch):
        from pka.api import source_paths
        monkeypatch.setattr(source_paths, "open_native_picker", lambda src: "/chosen/dir")
        r = client.post("/ingestion/sources/firefox/browse")
        assert r.status_code == 200
        assert r.json() == {"path": "/chosen/dir"}

    def test_cancelled_dialog_returns_none(self, client, monkeypatch):
        from pka.api import source_paths
        monkeypatch.setattr(source_paths, "open_native_picker", lambda src: None)
        r = client.post("/ingestion/sources/firefox/browse")
        assert r.json() == {"path": None}

    def test_unavailable_picker_returns_501(self, client, monkeypatch):
        from pka.api import source_paths

        def _boom(src):
            raise RuntimeError("Native file picker unavailable (no display)")

        monkeypatch.setattr(source_paths, "open_native_picker", _boom)
        r = client.post("/ingestion/sources/firefox/browse")
        assert r.status_code == 501

    def test_unknown_source_400(self, client):
        assert client.post("/ingestion/sources/nope/browse").status_code == 400
