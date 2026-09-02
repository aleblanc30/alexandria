"""Unit tests for per-source sync orchestrators."""

from unittest.mock import MagicMock

from pka.connectors.calibre import CalibreBook
from pka.connectors.firefox import FirefoxBookmark
from pka.connectors.images import ImageFile
from pka.connectors.zotero import ZoteroItem
from pka.ingestion import progress as sp


def _mock_pending_counts(monkeypatch, sync_module: str, *, pending: int = 1, baseline: int = 0):
    """Stub DB-backed pending totals; sync modules import these at module scope."""
    monkeypatch.setattr(f"{sync_module}.archive_document_count", lambda _src: baseline)
    monkeypatch.setattr(f"{sync_module}.count_pending_metadata", lambda _src: pending)


def _firefox_bm() -> FirefoxBookmark:
    return FirefoxBookmark(
        source_id="F1",
        url="https://example.com",
        title="Ex",
        folder_path=None,
        tags=[],
        date_added=1700000000,
    )


def _zotero_item(**overrides) -> ZoteroItem:
    defaults = dict(
        source_id="Z1",
        title="Paper",
        authors=[],
        abstract="Abstract text.",
        year=2024,
        doi=None,
        url=None,
        item_type="journalArticle",
        collections=[],
        tags=[],
        pdf_path=None,
        date_added=1700000000,
    )
    return ZoteroItem(**{**defaults, **overrides})


def _calibre_book(tmp_path, *, with_file: bool = True) -> CalibreBook:
    path = None
    if with_file:
        path = tmp_path / "test.epub"
        path.write_bytes(b"PK")
    return CalibreBook(
        source_id="1",
        title="Book",
        authors=["Author"],
        description="Desc",
        publisher=None,
        series=None,
        series_index=None,
        year=2020,
        isbn=None,
        tags=[],
        formats=["EPUB"] if with_file else [],
        preferred_path=path,
        date_added=1700000000,
        rating=None,
    )


def _image_file(tmp_path) -> ImageFile:
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"fake")
    return ImageFile(p, p.name, 100, 100, 100, 1700000000, {})


class TestFirefoxSync:
    def test_no_pending_skips_fetch_and_embed(self, monkeypatch):
        _mock_pending_counts(monkeypatch, "pka.ingestion.firefox_sync", pending=1)
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.reset_unfetchable_for_fetch",
            lambda: 0,
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.load_firefox_bookmarks",
            lambda: [_firefox_bm()],
        )
        monkeypatch.setattr("pka.ingestion.firefox_sync.firefox_ingest_queue", lambda limit: [])
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.ingest_firefox_bookmarks",
            lambda bookmarks, **kw: {"processed": 1, "skipped": 0, "failed": 0},
        )
        fetch_embed_mock = MagicMock()
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.fetch_and_embed_pending",
            fetch_embed_mock,
        )

        from pka.ingestion.firefox_sync import sync_firefox

        sp.begin("firefox")
        stats = sync_firefox(progress_key="firefox")

        assert stats["fetch"]["fetched"] == 0
        assert stats["embed"]["processed"] == 0
        fetch_embed_mock.assert_not_called()

    def test_runs_fetch_and_embed(self, monkeypatch):
        _mock_pending_counts(monkeypatch, "pka.ingestion.firefox_sync", pending=1)
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.reset_unfetchable_for_fetch",
            lambda: 0,
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.load_firefox_bookmarks",
            lambda: [_firefox_bm()],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.firefox_ingest_queue",
            lambda limit: [(42, "https://example.com/page")],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.ingest_firefox_bookmarks",
            lambda bookmarks, **kw: {"processed": 1, "skipped": 0, "failed": 0},
        )

        async def fake_fetch_embed(**kw):
            return {
                "fetched": 1,
                "skipped": 0,
                "unfetchable": 0,
                "embed": {"processed": 1, "skipped": 0, "failed": 0, "chunks": 2},
            }

        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.fetch_and_embed_pending",
            fake_fetch_embed,
        )

        from pka.ingestion.firefox_sync import sync_firefox

        sp.begin("firefox")
        stats = sync_firefox(progress_key="firefox")

        assert stats["fetch"]["fetched"] == 1
        assert stats["embed"]["processed"] == 1

    def test_requeues_unfetchable_wikipedia_before_fetch(self, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.reset_unfetchable_for_fetch",
            lambda: order.append("reset") or 2,
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.load_firefox_bookmarks",
            lambda: [_firefox_bm()],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.firefox_ingest_queue",
            lambda limit: order.append("queue") or [(1, "https://en.wikipedia.org/wiki/Python")],
        )

        async def fake_fetch_embed(**kw):
            return {
                "fetched": 1,
                "skipped": 0,
                "unfetchable": 0,
                "embed": {"processed": 1, "skipped": 0, "failed": 0, "chunks": 1},
            }

        fetch_embed_mock = MagicMock(side_effect=fake_fetch_embed)
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.fetch_and_embed_pending",
            fetch_embed_mock,
        )

        from pka.ingestion.firefox_sync import sync_firefox_ingest

        stats = sync_firefox_ingest()

        assert order == ["reset", "queue"]
        fetch_embed_mock.assert_called_once()
        assert stats["fetch"]["fetched"] == 1

    def test_stops_when_cancelled(self, monkeypatch):
        _mock_pending_counts(monkeypatch, "pka.ingestion.firefox_sync", pending=1)
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.reset_unfetchable_for_fetch",
            lambda: 0,
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.load_firefox_bookmarks",
            lambda: [_firefox_bm()],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.firefox_ingest_queue",
            lambda limit: [(1, "https://example.com")],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.ingest_firefox_bookmarks",
            lambda bookmarks, **kw: {"processed": 1, "skipped": 0, "failed": 0},
        )

        async def fake_fetch_embed(**kw):
            return {
                "fetched": 0,
                "skipped": 0,
                "unfetchable": 0,
                "embed": {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0},
                "stopped": "cancel",
            }

        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.fetch_and_embed_pending",
            fake_fetch_embed,
        )

        from pka.ingestion.firefox_sync import sync_firefox

        sp.begin("firefox")
        stats = sync_firefox(progress_key="firefox")

        assert stats["stopped"] == "cancel"

    def test_embeds_before_stop_when_cancelled_mid_run(self, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.reset_unfetchable_for_fetch",
            lambda: 0,
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.load_firefox_bookmarks",
            lambda: [_firefox_bm()],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.firefox_ingest_queue",
            lambda limit: [(1, "https://example.com")],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.ingest_firefox_bookmarks",
            lambda bookmarks, **kw: {"processed": 1, "skipped": 0, "failed": 0},
        )

        async def fake_fetch_embed(**kw):
            return {
                "fetched": 1,
                "skipped": 0,
                "unfetchable": 0,
                "embed": {"processed": 1, "skipped": 0, "failed": 0, "chunks": 1},
                "stopped": "cancel",
            }

        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.fetch_and_embed_pending",
            fake_fetch_embed,
        )

        from pka.ingestion.firefox_sync import sync_firefox_ingest

        sp.begin("firefox")
        stats = sync_firefox_ingest(progress_key="firefox")

        assert stats["stopped"] == "cancel"
        assert stats["embed"]["processed"] == 1

    def test_stops_after_metadata_when_cancelled(self, monkeypatch):
        _mock_pending_counts(monkeypatch, "pka.ingestion.firefox_sync", pending=1)
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.load_firefox_bookmarks",
            lambda: [_firefox_bm()],
        )
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.ingest_firefox_bookmarks",
            lambda bookmarks, **kw: {"processed": 0, "stopped": "cancel"},
        )
        get_queue = MagicMock()
        monkeypatch.setattr("pka.ingestion.firefox_sync.firefox_ingest_queue", get_queue)

        from pka.ingestion.firefox_sync import sync_firefox

        sp.begin("firefox")
        stats = sync_firefox(progress_key="firefox")

        assert stats["stopped"] == "cancel"
        get_queue.assert_not_called()


class TestZoteroSync:
    def test_runs_embedding_phase(self, monkeypatch):
        _mock_pending_counts(monkeypatch, "pka.ingestion.zotero_sync", pending=1)
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.load_items",
            lambda: [_zotero_item()],
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync._load_zotero_items_for_embed",
            lambda skip_existing=True: ([_zotero_item()], 1, 0),
        )
        meta = MagicMock(return_value={"processed": 1, "skipped": 0, "failed": 0})
        embed = MagicMock(return_value={"processed": 1, "skipped": 0, "failed": 0, "chunks": 2})
        monkeypatch.setattr("pka.ingestion.zotero_sync.ingest_zotero_metadata", meta)
        monkeypatch.setattr("pka.ingestion.zotero_sync.ingest_zotero_embed", embed)

        from pka.ingestion.zotero_sync import sync_zotero

        sp.begin("zotero")
        stats = sync_zotero(progress_key="zotero")

        meta.assert_called_once()
        embed.assert_called_once()
        assert stats["embed"]["processed"] == 1
        snap = sp.snapshot("zotero")["zotero"]
        assert "embedding" in snap["phases"]


class TestCalibreSync:
    def test_runs_metadata_and_fulltext(self, monkeypatch, tmp_path):
        _mock_pending_counts(monkeypatch, "pka.ingestion.calibre_sync", pending=1)
        books = [_calibre_book(tmp_path, with_file=True)]
        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.load_calibre_books",
            lambda: (books, None),
        )
        reg = MagicMock(return_value={"processed": 1, "skipped": 0, "failed": 0})
        meta = MagicMock(return_value={"processed": 1, "skipped": 0, "failed": 0, "chunks": 1})
        full = MagicMock(return_value={"processed": 1, "skipped": 0, "failed": 0, "chunks": 5})
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_metadata", reg)
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_books", meta)
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_fulltext", full)

        from pka.ingestion.calibre_sync import sync_calibre

        sp.begin("calibre")
        stats = sync_calibre(progress_key="calibre")

        reg.assert_called_once()
        meta.assert_called_once()
        full.assert_called_once()
        assert stats["metadata"]["processed"] == 1
        assert stats["fulltext"]["processed"] == 1

    def test_skips_fulltext_when_no_files(self, monkeypatch, tmp_path):
        _mock_pending_counts(monkeypatch, "pka.ingestion.calibre_sync", pending=1)
        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.load_calibre_books",
            lambda: ([_calibre_book(tmp_path, with_file=False)], None),
        )
        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.ingest_calibre_metadata",
            lambda books, **kw: {"processed": 1, "skipped": 0, "failed": 0},
        )
        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.ingest_calibre_books",
            lambda books, **kw: {"processed": 1, "skipped": 0, "failed": 0, "chunks": 1},
        )
        full = MagicMock()
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_fulltext", full)

        from pka.ingestion.calibre_sync import sync_calibre

        sp.begin("calibre")
        stats = sync_calibre(progress_key="calibre")

        full.assert_not_called()
        assert stats["fulltext"]["processed"] == 0

    def test_stops_after_metadata_when_cancelled(self, monkeypatch, tmp_path):
        _mock_pending_counts(monkeypatch, "pka.ingestion.calibre_sync", pending=1)
        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.load_calibre_books",
            lambda: ([_calibre_book(tmp_path, with_file=True)], None),
        )
        reg = MagicMock(return_value={"processed": 0, "stopped": "cancel"})
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_metadata", reg)
        meta = MagicMock()
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_books", meta)
        full = MagicMock()
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_fulltext", full)

        from pka.ingestion.calibre_sync import sync_calibre

        sp.begin("calibre")
        stats = sync_calibre(progress_key="calibre")
        assert stats["stopped"] == "cancel"
        meta.assert_not_called()
        full.assert_not_called()


class TestImageSync:
    def test_scans_and_ingests(self, monkeypatch, tmp_path):
        _mock_pending_counts(monkeypatch, "pka.ingestion.image_sync", pending=1)
        img = _image_file(tmp_path)
        monkeypatch.setattr(
            "pka.ingestion.image_sync.load_scanned_images",
            lambda: ([img], None),
        )
        reg = MagicMock(return_value={"processed": 1, "skipped": 0, "failed": 0})
        ingest = MagicMock(return_value={"processed": 1, "skipped": 0, "failed": 0, "by_type": {}})
        monkeypatch.setattr("pka.ingestion.image_sync.register_images", reg)
        monkeypatch.setattr("pka.ingestion.image_sync.ingest_images", ingest)

        from pka.ingestion.image_sync import sync_images

        sp.begin("image")
        stats = sync_images(progress_key="image")

        reg.assert_called_once()
        ingest.assert_called_once()
        assert stats["ingest"]["processed"] == 1


class TestUnavailableSources:
    def test_calibre_metadata_unavailable(self, monkeypatch):
        _mock_pending_counts(monkeypatch, "pka.ingestion.calibre_sync", pending=0)
        reason = "Calibre metadata.db not found at /missing/metadata.db"
        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.load_calibre_books",
            lambda: ([], reason),
        )
        reg = MagicMock()
        monkeypatch.setattr("pka.ingestion.calibre_sync.ingest_calibre_metadata", reg)

        from pka.api.routers import ingestion as ing

        sp.reset("calibre")
        ing._sync_metadata("calibre")
        snap = sp.snapshot("calibre")["calibre"]
        assert snap["status"] == "done"
        reg.assert_not_called()

    def test_image_ingest_unavailable(self, monkeypatch):
        reason = "Image folder not found: /missing"
        monkeypatch.setattr(
            "pka.ingestion.image_sync.load_scanned_images",
            lambda: ([], reason),
        )
        ingest = MagicMock()
        monkeypatch.setattr("pka.ingestion.image_sync.ingest_images", ingest)

        from pka.api.routers import ingestion as ing

        sp.reset("image")
        ing._sync_ingest("image")
        snap = sp.snapshot("image")["image"]
        assert snap["status"] == "done"
        ingest.assert_not_called()


class TestPipelineStop:
    def test_zotero_stops_mid_loop(self, mock_chroma):
        from pka.ingestion.runners.zotero import ingest_zotero_items

        sp.begin("zotero")
        sp.set_phase("zotero", "embedding", 3)
        sp.request_cancel("zotero")
        items = [
            _zotero_item(),
            _zotero_item(source_id="Z2"),
            _zotero_item(source_id="Z3"),
        ]
        stats = ingest_zotero_items(items, progress_key="zotero")
        assert stats.get("stopped") == "cancel"
        assert stats["processed"] == 0
