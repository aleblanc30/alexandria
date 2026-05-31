"""Tests for source-vs-archive pending metadata counts."""
from pka.constants import Source
from pka.db.queries import init_db, insert_document_if_new
from pka.ingestion.pending_metadata import archive_document_count, count_pending_metadata
from pka.pipeline import ingest_firefox_bookmarks
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
