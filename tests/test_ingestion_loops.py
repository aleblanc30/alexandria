"""Tests for ingestion loop helpers and source registry."""
from pka.constants import Source
from pka.ingestion.loops import run_embed_loop, run_metadata_loop
from pka.ingestion.registry import get_source_handlers, require_handlers


class TestRunMetadataLoop:
    def test_skips_known_items(self):
        known = {"a": 1}
        stats = run_metadata_loop(
            [{"id": "a"}, {"id": "b"}],
            known=known,
            get_source_id=lambda x: x["id"],
            persist=lambda x: "processed",
        )
        assert stats["skipped"] == 1
        assert stats["processed"] == 1

    def test_stops_on_cancel(self):
        from pka.ingestion import progress as sp

        sp.begin("firefox")
        sp.request_cancel("firefox")
        stats = run_metadata_loop(
            [{"id": "x"}],
            known={},
            get_source_id=lambda x: x["id"],
            persist=lambda x: "processed",
            progress_key="firefox",
        )
        assert stats.get("stopped") == "cancel"

    def test_failed_persist_increments_failed(self):
        def _boom(_item):
            raise ValueError("persist failed")

        stats = run_metadata_loop(
            [{"id": "bad"}],
            known={},
            get_source_id=lambda x: x["id"],
            persist=_boom,
        )
        assert stats["failed"] == 1


class TestRunEmbedLoop:
    def test_processes_and_counts_chunks(self):
        stats = run_embed_loop(
            [{"id": "1"}],
            should_skip=lambda x: False,
            process=lambda x: (True, 3),
        )
        assert stats["processed"] == 1
        assert stats["chunks"] == 3

    def test_exception_increments_failed(self):
        def _boom(_item):
            raise RuntimeError("embed failed")

        stats = run_embed_loop(
            [{"id": "1"}],
            should_skip=lambda x: False,
            process=_boom,
        )
        assert stats["failed"] == 1


class TestSourceRegistry:
    def test_all_sources_have_handlers(self):
        handlers = get_source_handlers()
        for src in Source:
            assert src in handlers
            h = handlers[src]
            assert callable(h.sync_metadata)
            assert callable(h.sync_ingest)
            assert callable(h.sync_full)

    def test_require_handlers_unknown_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Unknown source"):
            require_handlers("not-a-source")
