"""Tests for dev-mode ingestion limits."""
from pka.ingestion import dev_limits


class TestDevLimits:
    def test_take_no_limit_when_not_dev(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", False)
        items = list(range(150))
        assert dev_limits.take(items) == items
        assert dev_limits.effective_ingestion_limit() is None

    def test_take_truncates_when_dev(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", True)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit", 100)
        items = list(range(150))
        assert dev_limits.take(items) == list(range(100))
        assert dev_limits.effective_ingestion_limit() == 100

    def test_take_no_truncation_under_limit(self, monkeypatch):
        monkeypatch.setattr(dev_limits.settings, "dev", True)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit", 100)
        items = list(range(50))
        assert dev_limits.take(items) == items

    def test_zotero_metadata_respects_dev_limit(self, monkeypatch):
        from pka.ingestion import sync_progress as sp
        from pka.ingestion.zotero_sync import sync_zotero_metadata

        monkeypatch.setattr(dev_limits.settings, "dev", True)
        monkeypatch.setattr(dev_limits.settings, "dev_ingestion_limit", 100)

        items = [object() for _ in range(150)]
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
