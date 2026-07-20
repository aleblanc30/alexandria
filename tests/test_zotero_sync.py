"""Tests for Zotero sync orchestration."""
from unittest.mock import MagicMock

from pka.connectors.zotero import ZoteroItem


def _zotero_item(key: str, **overrides) -> ZoteroItem:
    defaults = dict(
        source_id=key, title="Paper", authors=[], abstract="Abstract.",
        year=2024, doi=None, url=None, item_type="journalArticle",
        collections=[], tags=[], pdf_path=None, date_added=1700000000,
        pdf_attachment_key=None,
    )
    return ZoteroItem(**{**defaults, **overrides})


class TestLoadZoteroItemsForEmbed:
    def test_all_embedded_returns_empty(self, monkeypatch, zotero_db):
        from pka.ingestion.zotero_sync import _load_zotero_items_for_embed

        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.ensure_zotero_copy",
            lambda: zotero_db,
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.load_item_keys",
            lambda **kw: ["Z1", "Z2"],
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.source_ids_with_chunks",
            lambda _src: {"Z1", "Z2"},
        )
        items, total, skipped = _load_zotero_items_for_embed(skip_existing=True)
        assert items == []
        assert total == 2
        assert skipped == 2

    def test_pending_keys_loaded(self, monkeypatch, zotero_db):
        from pka.ingestion.zotero_sync import _load_zotero_items_for_embed

        pending_item = _zotero_item("PEND01")
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.ensure_zotero_copy",
            lambda: zotero_db,
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.load_item_keys",
            lambda **kw: ["DONE01", "PEND01"],
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.source_ids_with_chunks",
            lambda _src: {"DONE01"},
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.load_items",
            lambda **kw: [pending_item],
        )
        items, total, skipped = _load_zotero_items_for_embed(skip_existing=True)
        assert len(items) == 1
        assert items[0].source_id == "PEND01"
        assert total == 2
        assert skipped == 1


class TestSyncZoteroIngest:
    def test_nothing_to_embed_short_circuits(self, monkeypatch):
        from pka.ingestion.zotero_sync import sync_zotero_ingest

        monkeypatch.setattr(
            "pka.ingestion.zotero_sync._load_zotero_items_for_embed",
            lambda **kw: ([], 0, 0),
        )
        embed_mock = MagicMock()
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.ingest_zotero_embed", embed_mock,
        )
        out = sync_zotero_ingest(progress_key="zotero")
        embed_mock.assert_not_called()
        assert out["embed"]["processed"] == 0

    def test_embed_called_for_pending_items(self, monkeypatch):
        from pka.ingestion.zotero_sync import sync_zotero_ingest

        item = _zotero_item("E1")
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync._load_zotero_items_for_embed",
            lambda **kw: ([item], 1, 0),
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.ingest_zotero_embed",
            lambda items, **kw: {
                "processed": len(items), "skipped": 0, "failed": 0, "chunks": 2,
            },
        )
        out = sync_zotero_ingest(progress_key="zotero")
        assert out["embed"]["processed"] == 1
        assert out["embed"]["chunks"] == 2


class TestSyncZoteroFull:
    def test_stops_after_metadata_cancel(self, monkeypatch):
        from pka.ingestion.zotero_sync import sync_zotero

        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.sync_zotero_metadata",
            lambda **kw: {"metadata": {}, "stopped": "cancel"},
        )
        embed_mock = MagicMock()
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.sync_zotero_ingest", embed_mock,
        )
        out = sync_zotero(progress_key="zotero")
        embed_mock.assert_not_called()
        assert out.get("stopped") == "cancel"
