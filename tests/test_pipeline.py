from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from pka.connectors.firefox import FirefoxBookmark
from pka.connectors.zotero import ZoteroItem
from pka.db.queries import document_has_chunks, get_engine, init_db
from pka.db.schema import documents, source_tags
from pka.ingestion.runners import (
    ingest_fetched_texts,
    ingest_firefox_bookmarks,
    ingest_zotero_embed,
    ingest_zotero_items,
)


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

    def test_card_summary_stores_abstract(self, mock_chroma):
        ingest_zotero_items([_make_zotero_item()])
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.card_summary).where(documents.c.source_id == "Z001")
            ).fetchone()
        assert "abstract with enough words" in row[0]

    def test_card_summary_updates_on_resync_without_rechunk(self, mock_chroma):
        item = _make_zotero_item()
        ingest_zotero_items([item])
        updated = _make_zotero_item(abstract="Updated abstract for card display.")
        stats = ingest_zotero_items([updated], skip_existing=True)
        assert stats["skipped"] == 1
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.card_summary).where(documents.c.source_id == "Z001")
            ).fetchone()
        assert row[0] == "Updated abstract for card display."


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

    def test_card_summary_stores_body_excerpt(self, mock_chroma):
        from pka.db.queries import upsert_document as ud

        doc_id = ud("firefox", "F010", "Page", "https://x.com", None)
        body = (
            "Intro line one with enough words.\n"
            "Intro line two continues the article.\n"
            "Third paragraph adds more context here.\n"
            "Fourth paragraph should not appear in the card excerpt."
        )
        ingest_fetched_texts({doc_id: body})
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.card_summary).where(documents.c.id == doc_id)
            ).fetchone()
        assert row[0] is not None
        assert "Intro line one" in row[0]
        assert "Intro line two" in row[0]
        assert "Fourth paragraph should not appear" not in row[0]


class TestFetchedTextEnrichment:
    """Title + card summary reach the vector index for fetched pages (DESIGN §3.2)."""

    @staticmethod
    def _records(store, doc_id) -> list[dict]:
        return [it for it in store.values() if it["meta"]["document_id"] == doc_id]

    @staticmethod
    def _new_doc(source_id: str, title: str) -> int:
        from pka.db.queries import upsert_document

        return upsert_document(
            "firefox", source_id, title, f"https://example.com/{source_id}", None,
        )

    def test_title_embedded_and_in_chunk_metadata(self, mock_chroma):
        from pka.ingestion.runners.firefox import embed_fetched_text

        store, _ = mock_chroma
        doc_id = self._new_doc("F100", "Paxos Made Simple")
        body = (
            "The protocol proceeds in two phases. A proposer picks a number and "
            "asks the acceptors to promise. Nothing here names the subject."
        )
        embed_fetched_text(doc_id, body, skip_existing=False)

        records = self._records(store, doc_id)
        assert records
        assert all(r["meta"]["title"] == "Paxos Made Simple" for r in records)
        assert any("Paxos Made Simple" in r["text"] for r in records)

    def test_thin_page_yields_one_fallback_chunk_and_doc_embedding(self, mock_chroma):
        from pka.db.schema import chunks
        from pka.ingestion.runners.firefox import embed_fetched_text

        doc_id = self._new_doc("F101", "Tiny page")
        outcome = embed_fetched_text(doc_id, "Too short.", skip_existing=False)

        assert outcome["processed"] and outcome["chunks"] == 1
        with get_engine().connect() as con:
            n_chunks = con.execute(
                sa.select(sa.func.count()).select_from(chunks)
                .where(chunks.c.document_id == doc_id)
            ).scalar()
            text, embedding = con.execute(
                sa.select(chunks.c.text, documents.c.doc_embedding)
                .select_from(chunks.join(documents, documents.c.id == chunks.c.document_id))
                .where(chunks.c.document_id == doc_id)
            ).fetchone()
        assert n_chunks == 1
        assert "Tiny page" in text
        assert embedding is not None

    def test_card_summary_contributes_to_embedded_text(self, mock_chroma):
        from pka.ingestion.runners.firefox import embed_fetched_text

        store, _ = mock_chroma
        doc_id = self._new_doc("F102", "Attention Is All You Need")
        abstract = "We revisit transformer architectures for language modelling."
        body = "Opening line of the PDF that says nothing about the contribution. " * 3
        embed_fetched_text(doc_id, body, card_summary=abstract, skip_existing=False)

        assert any(abstract in r["text"] for r in self._records(store, doc_id))

    def test_handler_overridden_title_is_the_embedded_one(self, mock_chroma):
        from pka.constants import FetchStatus
        from pka.ingestion.fetcher import FetchResult, _persist_fetch_result
        from pka.ingestion.runners.firefox import embed_fetched_text

        store, _ = mock_chroma
        doc_id = self._new_doc("F103", "Old bookmark title")
        body = "Paper body text long enough to survive the minimum chunk filter here."
        # The arXiv/bioRxiv/Amazon handlers overwrite documents.title on persist.
        _persist_fetch_result(FetchResult(
            doc_id,
            "https://arxiv.org/abs/2301.00001",
            str(FetchStatus.FETCHED),
            body,
            200,
            None,
            title="Attention Is All You Need Again",
        ))
        embed_fetched_text(doc_id, body, skip_existing=False)

        records = self._records(store, doc_id)
        assert records
        assert all(r["meta"]["title"] == "Attention Is All You Need Again" for r in records)
        assert not any("Old bookmark title" in r["text"] for r in records)

    def test_batch_path_looks_up_titles_once(self, monkeypatch, mock_chroma):
        import pka.ingestion.runners.firefox as firefox_runner

        store, _ = mock_chroma
        doc_ids = {
            self._new_doc(f"F11{i}", f"Bookmark title {i}"): i for i in range(3)
        }
        calls: list[list[int]] = []
        real = firefox_runner.document_titles

        def _spy(ids):
            calls.append(list(ids))
            return real(ids)

        monkeypatch.setattr(firefox_runner, "document_titles", _spy)
        ingest_fetched_texts({
            doc_id: (
                "Body text with several sentences. It is long enough to chunk "
                "without help from the fallback path in this test case."
            )
            for doc_id in doc_ids
        })

        assert len(calls) == 1
        assert sorted(calls[0]) == sorted(doc_ids)
        for doc_id, i in doc_ids.items():
            records = self._records(store, doc_id)
            assert records
            assert all(r["meta"]["title"] == f"Bookmark title {i}" for r in records)


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
