import pytest
from unittest.mock import MagicMock
from pka.connectors.zotero import ZoteroItem
from pka.connectors.firefox import FirefoxBookmark
from pka.db.queries import init_db, document_has_chunks, get_engine
from pka.db.schema import documents, source_tags
from pka.ingestion.runners import (
    ingest_fetched_texts,
    ingest_firefox_bookmarks,
    ingest_zotero_embed,
    ingest_zotero_items,
)
import sqlalchemy as sa


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


def _make_zotero_item(**overrides) -> ZoteroItem:
    defaults = dict(
        source_id="Z001", title="Test Paper",
        authors=["Alice"],
        abstract=(
            "An abstract with enough words to form a chunk that exceeds the "
            "minimum character threshold for embedding and persistence."
        ),
        year=2023, doi="10.1/test", url=None, item_type="journalArticle",
        collections=["CS"], tags=["test-tag"], pdf_path=None, date_added=1700000000,
    )
    return ZoteroItem(**{**defaults, **overrides})


def _make_firefox_bookmark(**overrides) -> FirefoxBookmark:
    defaults = dict(
        source_id="F001", url="https://example.com",
        title="Example", folder_path="Research/Web",
        tags=["web"], date_added=1700000000,
    )
    return FirefoxBookmark(**{**defaults, **overrides})


class TestIngestZoteroItems:
    def test_document_written_to_db(self, mock_chroma):
        ingest_zotero_items([_make_zotero_item()])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.source_id).where(documents.c.source == "zotero")
            ).fetchone()
        assert row[0] == "Z001"

    def test_tags_written_to_db(self, mock_chroma):
        ingest_zotero_items([_make_zotero_item()])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(source_tags.c.tag_string).where(source_tags.c.source == "zotero")
            ).fetchone()
        assert row[0] == "test-tag"

    def test_item_type_and_classification_tags(self, mock_chroma):
        from pka.db.schema import overlay_tags

        ingest_zotero_items([_make_zotero_item()])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.item_type).where(documents.c.source_id == "Z001")
            ).fetchone()
            assert row[0] == "journalArticle"
            tags = {
                r[0] for r in con.execute(
                    sa.select(overlay_tags.c.tag).where(
                        overlay_tags.c.document_id == con.execute(
                            sa.select(documents.c.id).where(documents.c.source_id == "Z001")
                        ).scalar()
                    )
                ).fetchall()
            }
        assert tags == {"academic", "paper"}

    def test_preprint_classification(self, mock_chroma):
        from pka.db.schema import overlay_tags

        ingest_zotero_items([_make_zotero_item(item_type="preprint")])
        with get_engine().connect() as con:
            doc_id = con.execute(
                sa.select(documents.c.id).where(documents.c.source_id == "Z001")
            ).scalar()
            tags = {
                r[0] for r in con.execute(
                    sa.select(overlay_tags.c.tag).where(
                        overlay_tags.c.document_id == doc_id
                    )
                ).fetchall()
            }
        assert tags == {"academic", "preprint"}

    def test_chunks_created(self, mock_chroma):
        item = _make_zotero_item()
        ingest_zotero_items([item])
        with get_engine().connect() as con:
            doc_id = con.execute(
                sa.select(documents.c.id).where(documents.c.source_id == "Z001")
            ).scalar()
        assert document_has_chunks(doc_id)

    def test_embeddings_sent_to_chroma(self, mock_chroma):
        store, col = mock_chroma
        ingest_zotero_items([_make_zotero_item()])
        assert col.upsert.called

    def test_skip_existing_skips_rechunking(self, mock_chroma):
        _, col = mock_chroma
        ingest_zotero_items([_make_zotero_item()])
        first_call_count = col.upsert.call_count
        ingest_zotero_items([_make_zotero_item()], skip_existing=True)
        assert col.upsert.call_count == first_call_count   # no new upserts

    def test_dry_run_skips_chroma_and_chunks(self, mock_chroma):
        _, col = mock_chroma
        ingest_zotero_items([_make_zotero_item()], dry_run=True)
        assert not col.upsert.called

    def test_item_without_text_is_skipped(self, mock_chroma):
        item = _make_zotero_item(title="", abstract=None, authors=[])
        stats = ingest_zotero_items([item])
        assert stats["skipped"] == 1

    def test_stats_returned(self, mock_chroma):
        stats = ingest_zotero_items([_make_zotero_item()])
        assert stats["processed"] == 1
        assert stats["failed"] == 0

    def test_failed_item_counted_not_raised(self, monkeypatch, mock_chroma):
        monkeypatch.setattr(
            "pka.ingestion.runners.zotero.upsert_document",
            MagicMock(side_effect=Exception("db error")),
        )
        stats = ingest_zotero_items([_make_zotero_item()])
        assert stats["failed"] == 1

    def test_short_title_with_authors_is_embedded(self, mock_chroma):
        item = _make_zotero_item(
            title="Short title",
            abstract=None,
            authors=["Ada Lovelace", "Alan Turing"],
        )
        stats = ingest_zotero_items([item])
        assert stats["processed"] == 1
        assert stats["skipped"] == 0

    def test_annotation_highlight_is_embedded(self, mock_chroma):
        item = _make_zotero_item(
            source_id="ANN001",
            title="",
            abstract=None,
            authors=[],
            item_type="annotation",
            highlight_text="A PDF highlight with enough content to embed.",
        )
        stats = ingest_zotero_embed([item])
        assert stats["processed"] == 1
        assert stats["chunks"] >= 1


class TestIngestFirefoxBookmarks:
    def test_document_written_to_db(self):
        ingest_firefox_bookmarks([_make_firefox_bookmark()])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.source_id).where(documents.c.source == "firefox")
            ).fetchone()
        assert row[0] == "F001"

    def test_fetch_status_set_to_pending(self):
        ingest_firefox_bookmarks([_make_firefox_bookmark()])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.fetch_status).where(documents.c.source == "firefox")
            ).fetchone()
        assert row[0] == "pending"

    def test_local_file_url_marked_unfetchable(self):
        bm = _make_firefox_bookmark(url="file:///C:/Users/foo.pdf")
        ingest_firefox_bookmarks([bm])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.fetch_status).where(documents.c.source_id == "F001")
            ).fetchone()
        assert row[0] == "unfetchable"

    def test_tags_written(self):
        ingest_firefox_bookmarks([_make_firefox_bookmark()])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(source_tags.c.tag_string).where(source_tags.c.source == "firefox")
            ).fetchone()
        assert row[0] == "web"

    def test_arxiv_bookmark_classified_as_preprint(self):
        from pka.db.schema import overlay_tags

        bm = _make_firefox_bookmark(url="https://arxiv.org/abs/2301.00001")
        ingest_firefox_bookmarks([bm])
        with get_engine().connect() as con:
            doc_id = con.execute(
                sa.select(documents.c.id).where(documents.c.source_id == "F001")
            ).scalar()
            tags = {
                r[0] for r in con.execute(
                    sa.select(overlay_tags.c.tag).where(
                        overlay_tags.c.document_id == doc_id
                    )
                ).fetchall()
            }
        assert tags == {"academic", "preprint"}

    def test_resync_skips_existing_bookmark(self):
        bm = _make_firefox_bookmark()
        first = ingest_firefox_bookmarks([bm])
        assert first["processed"] == 1
        second = ingest_firefox_bookmarks([bm])
        assert second["processed"] == 0
        assert second["skipped"] == 1
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(documents).where(
                    documents.c.source == "firefox"
                )
            ).scalar()
        assert count == 1


class TestIngestFetchedTexts:
    def test_chunks_created_for_fetched_text(self, mock_chroma):
        doc_id = __import__("pka.db.queries", fromlist=["upsert_document"]).upsert_document(
            "firefox", "F002", "Page", "https://x.com", None
        )
        ingest_fetched_texts({
            doc_id: (
                "This is fetched content. It has multiple sentences with enough "
                "length to exceed the minimum chunk size for embedding."
            ),
        })
        assert document_has_chunks(doc_id)

    def test_dry_run_produces_no_chunks(self, mock_chroma):
        from pka.db.queries import upsert_document as ud
        doc_id = ud("firefox", "F003", "P", "https://y.com", None)
        ingest_fetched_texts(
            {doc_id: "Enough text to chunk. More sentences follow. And another one."},
            dry_run=True,
        )
        assert not document_has_chunks(doc_id)

    def test_empty_texts_dict_returns_zero_processed(self, mock_chroma):
        stats = ingest_fetched_texts({})
        assert stats["processed"] == 0


class TestPipelineStop:
    def test_firefox_bookmarks_stops_on_cancel(self):
        from pka.ingestion import sync_progress as sp
        from pka.ingestion.runners.firefox import ingest_firefox_bookmarks

        sp.begin("firefox")
        sp.set_phase("firefox", "metadata", 3)
        sp.request_cancel("firefox")
        bms = [_make_firefox_bookmark(source_id=f"F{i}") for i in range(3)]
        stats = ingest_firefox_bookmarks(bms, progress_key="firefox")
        assert stats.get("stopped") == "cancel"

    def test_calibre_fulltext_stops_on_pause(self, tmp_path, mock_chroma):
        from pka.connectors.calibre import CalibreBook
        from pka.ingestion import sync_progress as sp
        from pka.ingestion.runners.calibre import ingest_calibre_books, ingest_calibre_fulltext

        epub = tmp_path / "book.epub"
        epub.write_bytes(b"PK")
        book = CalibreBook(
            source_id="B1", title="Book", authors=["A"], description="Desc",
            publisher=None, series=None, series_index=None, year=2020, isbn=None,
            tags=[], formats=["EPUB"], preferred_path=epub,
            date_added=1700000000, rating=None,
        )
        ingest_calibre_books([book])

        sp.begin("calibre")
        sp.set_phase("calibre", "fulltext", 2)
        sp.request_pause("calibre")

        import pka.ingestion.runners.calibre as calibre_runner
        original = calibre_runner.extract_book_text
        calibre_runner.extract_book_text = lambda p, **kw: [
            {"title": "Ch", "text": "Section one. " * 20, "index": 0},
        ]
        try:
            stats = ingest_calibre_fulltext([book], progress_key="calibre")
        finally:
            calibre_runner.extract_book_text = original
        assert stats.get("stopped") == "pause"
