"""Tests for source-vs-archive pending metadata counts."""
import pka.ingestion.pending_metadata as pm
from pka.constants import Source
from pka.db.queries import init_db, insert_document_if_new
from pka.ingestion.pending_metadata import (
    archive_document_count,
    count_pending_metadata,
    invalidate_source_probes,
    source_corpus_size,
)
from pka.ingestion.runners.firefox import ingest_firefox_bookmarks
from tests.test_pipeline import _make_firefox_bookmark


def test_archive_document_count():
    init_db()
    assert archive_document_count(Source.FIREFOX) == 0
    insert_document_if_new("firefox", "bm1", "T", "http://a", None)
    assert archive_document_count(Source.FIREFOX) == 1


def test_count_pending_skips_archived_firefox(monkeypatch):
    init_db()
    bm = _make_firefox_bookmark()
    ingest_firefox_bookmarks([bm])
    monkeypatch.setattr(
        "pka.connectors.firefox.load_bookmarks",
        lambda: [bm, _make_firefox_bookmark(source_id="bm-new", url="http://new")],
    )
    assert count_pending_metadata(Source.FIREFOX) == 1


def test_probe_result_is_cached_within_ttl(monkeypatch):
    """count/corpus probes must not re-hit the source on every status poll."""
    init_db()
    calls = {"n": 0}

    def _one_bookmark():
        calls["n"] += 1
        return [_make_firefox_bookmark(source_id="bm-1", url="http://a")]

    monkeypatch.setattr("pka.connectors.firefox.load_bookmarks", _one_bookmark)

    assert source_corpus_size(Source.FIREFOX) == 1
    assert source_corpus_size(Source.FIREFOX) == 1  # served from cache
    assert calls["n"] == 1

    invalidate_source_probes(Source.FIREFOX)
    assert source_corpus_size(Source.FIREFOX) == 1  # recomputed after invalidation
    assert calls["n"] == 2


def test_probe_cache_disabled_when_ttl_zero(monkeypatch):
    init_db()
    calls = {"n": 0}

    def _one_bookmark():
        calls["n"] += 1
        return [_make_firefox_bookmark(source_id="bm-1", url="http://a")]

    monkeypatch.setattr("pka.connectors.firefox.load_bookmarks", _one_bookmark)
    monkeypatch.setattr(pm.settings, "ingestion_probe_cache_ttl_seconds", 0.0)

    source_corpus_size(Source.FIREFOX)
    source_corpus_size(Source.FIREFOX)
    assert calls["n"] == 2  # no caching, recomputed each call
