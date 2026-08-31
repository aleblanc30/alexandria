"""Tests for graceful optional source access."""

import pytest

from pka.connectors.calibre import load_books
from pka.connectors.images import scan_images
from pka.ingestion.source_access import (
    calibre_available,
    images_available,
    try_load_calibre_books,
    try_scan_images,
)


class TestSourceAccess:
    def test_calibre_available_when_present(self, tmp_path, monkeypatch):
        lib = tmp_path / "books"
        lib.mkdir()
        (lib / "metadata.db").write_bytes(b"sqlite")
        monkeypatch.setattr("pka.ingestion.source_access.settings.book_archive", lib)
        ok, msg = calibre_available()
        assert ok is True
        assert msg is None

    def test_calibre_unavailable_when_missing(self, tmp_path, monkeypatch):
        lib = tmp_path / "missing"
        monkeypatch.setattr("pka.ingestion.source_access.settings.book_archive", lib)
        ok, msg = calibre_available()
        assert ok is False
        assert msg and "metadata.db" in msg

    def test_images_available_when_present(self, tmp_path, monkeypatch):
        folder = tmp_path / "images"
        folder.mkdir()
        monkeypatch.setattr("pka.ingestion.source_access.settings.image_dirs", [folder])
        ok, msg = images_available()
        assert ok is True
        assert msg is None

    def test_images_available_when_one_of_several_present(self, tmp_path, monkeypatch):
        present = tmp_path / "images"
        present.mkdir()
        missing = tmp_path / "missing"
        monkeypatch.setattr(
            "pka.ingestion.source_access.settings.image_dirs",
            [missing, present],
        )
        ok, msg = images_available()
        assert ok is True
        assert msg is None

    def test_images_unavailable_when_missing(self, tmp_path, monkeypatch):
        folder = tmp_path / "missing"
        monkeypatch.setattr("pka.ingestion.source_access.settings.image_dirs", [folder])
        ok, msg = images_available()
        assert ok is False
        assert msg and "Image folder not found" in msg

    def test_images_unavailable_when_none_configured(self, monkeypatch):
        monkeypatch.setattr("pka.ingestion.source_access.settings.image_dirs", [])
        ok, msg = images_available()
        assert ok is False
        assert msg and "No image folders configured" in msg

    def test_try_load_calibre_returns_empty_when_missing(self, tmp_path, monkeypatch):
        lib = tmp_path / "missing"
        monkeypatch.setattr("pka.ingestion.source_access.settings.book_archive", lib)
        books, reason = try_load_calibre_books()
        assert books == []
        assert reason and "metadata.db" in reason

    def test_try_scan_images_returns_empty_when_missing(self, tmp_path, monkeypatch):
        folder = tmp_path / "missing"
        monkeypatch.setattr("pka.ingestion.source_access.settings.image_dirs", [folder])
        images, reason = try_scan_images()
        assert images == []
        assert reason and "Image folder not found" in reason

    def test_connectors_still_raise_when_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_books(library_root=tmp_path / "missing", copy_path=tmp_path / "copy.db")
        with pytest.raises(FileNotFoundError):
            scan_images(tmp_path / "missing")
