"""Tests for dev-mode ingestion limits."""
from types import SimpleNamespace

import pytest

from pka.constants import Source
from pka.ingestion import dev_limits


class TestDevLimits:
    def test_take_no_limit_when_not_dev(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", False)
        items = list(range(150))
        assert dev_limits.take(items, Source.ZOTERO) == items
        assert dev_limits.effective_ingestion_limit(Source.ZOTERO) is None

    def test_take_truncates_when_dev(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", True)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit_zotero", 100)
        items = list(range(150))
        assert dev_limits.take(items, Source.ZOTERO) == list(range(100))
        assert dev_limits.effective_ingestion_limit(Source.ZOTERO) == 100

    def test_take_no_truncation_under_limit(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", True)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit_zotero", 100)
        items = list(range(50))
        assert dev_limits.take(items, Source.ZOTERO) == items

    def test_limits_are_per_source(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", True)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit_firefox", 5)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit_image", 20)
        items = list(range(50))
        assert dev_limits.take(items, Source.FIREFOX) == list(range(5))
        assert dev_limits.take(items, Source.IMAGE) == list(range(20))

    def test_unknown_source_rejected(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", True)
        with pytest.raises(ValueError):
            dev_limits.effective_ingestion_limit("nope")

    def test_zotero_metadata_respects_dev_limit(self, monkeypatch):
        from pka.ingestion import progress as sp
        from pka.ingestion.zotero_sync import sync_zotero_metadata

        monkeypatch.setattr(dev_limits.settings, "dev", True)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit_zotero", 100)

        items = [
            SimpleNamespace(source_id=f"k{i}", pdf_attachment_key=None)
            for i in range(150)
        ]
        seen: list = []

        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.archive_document_count", lambda _src: 0,
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.count_pending_metadata", lambda _src: 0,
        )
        monkeypatch.setattr("pka.ingestion.zotero_sync.load_items", lambda: items)
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.ingest_zotero_metadata",
            lambda batch, **kw: seen.append(len(batch)) or {
                "processed": len(batch), "skipped": 0, "failed": 0,
            },
        )

        sp.reset("zotero")
        sync_zotero_metadata(progress_key="zotero")
        assert seen == [100]
