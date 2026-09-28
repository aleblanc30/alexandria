"""Calibre's full text is embedded once, and the copies earlier runs left can be purged.

Until the ingest job learned to skip books already done, every Calibre ingest
re-extracted every book and appended another full copy of its chunks, and of
its cached summary chunk.
"""

import pytest
import sqlalchemy as sa

from pka.config import settings
from pka.constants import FetchStatus, PdfTextLayer, Source
from pka.db.engine import get_engine
from pka.db.migrate import init_db
from pka.db.schema import chunks, documents
from pka.ingestion.book_extractor import BookExtraction
from pka.ingestion.runners.calibre import ingest_calibre_books, ingest_calibre_fulltext
from pka.purge import purge_target
from tests.test_pipeline_calibre_integration import _make_book

_BODY = " ".join(f"Sentence {i} of the chapter says something specific." for i in range(40))
_SECTIONS = [
    {"title": "Ch1", "text": _BODY, "index": 0, "page_start": 1, "page_end": 5},
    {"title": "Ch2", "text": _BODY.replace("chapter", "section"), "index": 1, "page_start": 6},
]


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


def _extract(monkeypatch, extraction: BookExtraction) -> None:
    monkeypatch.setattr(
        "pka.ingestion.runners.calibre.extract_book_report", lambda path, **kw: extraction
    )


def _book(tmp_path, source_id="B001"):
    pdf = tmp_path / f"{source_id}.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    return _make_book(source_id=source_id, preferred_path=pdf, formats=["PDF"])


def _passes(source_id: str) -> list[str | None]:
    with get_engine().connect() as con:
        return [
            r[0]
            for r in con.execute(
                sa.select(chunks.c.chunk_pass)
                .select_from(chunks.join(documents, chunks.c.document_id == documents.c.id))
                .where(documents.c.source_id == source_id)
                .order_by(chunks.c.chunk_index)
            ).fetchall()
        ]


class TestSyncEmbedsFullTextOnce:
    def _sync(self, monkeypatch, books):
        from pka.ingestion.calibre_sync import sync_calibre_ingest

        monkeypatch.setattr("pka.ingestion.calibre_sync.load_calibre_books", lambda: (books, None))
        return sync_calibre_ingest(progress_key="calibre")

    def test_a_second_ingest_adds_no_chunks(self, mock_chroma, tmp_path, monkeypatch):
        book = _book(tmp_path)
        _extract(monkeypatch, BookExtraction(_SECTIONS))

        first = self._sync(monkeypatch, [book])
        after_first = _passes(book.source_id)
        second = self._sync(monkeypatch, [book])

        assert first["fulltext"]["processed"] == 1
        assert second["fulltext"]["processed"] == 0
        assert after_first.count("fulltext") > 0
        assert _passes(book.source_id) == after_first

    def test_a_scan_is_not_re_read(self, mock_chroma, tmp_path, monkeypatch):
        book = _book(tmp_path)
        _extract(monkeypatch, BookExtraction([], status=PdfTextLayer.NONE, page_count=4))
        self._sync(monkeypatch, [book])

        reads: list = []
        monkeypatch.setattr(
            "pka.ingestion.runners.calibre.extract_book_report",
            lambda path, **kw: reads.append(path) or BookExtraction([]),
        )
        self._sync(monkeypatch, [book])

        assert reads == []
        with get_engine().connect() as con:
            status = con.execute(
                sa.select(documents.c.fetch_status).where(documents.c.source_id == book.source_id)
            ).scalar()
        assert status == str(FetchStatus.NO_TEXT_LAYER)

    def test_only_the_new_book_is_extracted(self, mock_chroma, tmp_path, monkeypatch):
        old, new = _book(tmp_path, "OLD"), _book(tmp_path, "NEW")
        _extract(monkeypatch, BookExtraction(_SECTIONS))
        self._sync(monkeypatch, [old])

        stats = self._sync(monkeypatch, [old, new])

        assert stats["fulltext"]["processed"] == 1
        assert _passes("NEW").count("fulltext") == _passes("OLD").count("fulltext")


class TestDuplicateChunksPurge:
    """What the old behaviour left behind: whole extra runs of the same chunks."""

    @pytest.fixture
    def summaries(self, monkeypatch):
        monkeypatch.setattr(settings, "book_summary_enabled", True)
        monkeypatch.setattr(
            "pka.ingestion.summarize.summarize_text", lambda text, **kw: "What the book is about."
        )

    def _run_twice(self, monkeypatch, book, second=None):
        ingest_calibre_books([book])
        _extract(monkeypatch, BookExtraction(_SECTIONS))
        ingest_calibre_fulltext([book])
        _extract(monkeypatch, BookExtraction(second or _SECTIONS))
        ingest_calibre_fulltext([book])

    def test_repeated_runs_are_cut_back_to_one(self, mock_chroma, tmp_path, monkeypatch, summaries):
        book = _book(tmp_path)
        self._run_twice(monkeypatch, book)
        doubled = _passes(book.source_id)
        one_run = doubled.count("fulltext") // 2
        assert doubled.count("summary") == 2

        counted = purge_target("duplicate_chunks", dry_run=True)
        purged = purge_target("duplicate_chunks")

        assert counted == {"documents": 1, "fulltext_chunks": one_run, "summary_chunks": 1}
        assert purged["vectors_purged"] == one_run + 1
        after = _passes(book.source_id)
        assert after.count("fulltext") == one_run
        assert after.count("summary") == 1
        assert after.count("metadata") == doubled.count("metadata")
        assert purge_target("duplicate_chunks", dry_run=True)["documents"] == 0

    def test_the_first_copy_is_the_one_kept(self, mock_chroma, tmp_path, monkeypatch):
        book = _book(tmp_path)
        self._run_twice(monkeypatch, book)
        with get_engine().connect() as con:
            first_run = con.execute(
                sa.select(sa.func.min(chunks.c.chunk_index)).where(
                    chunks.c.chunk_pass == "fulltext"
                )
            ).scalar()

        purge_target("duplicate_chunks")

        with get_engine().connect() as con:
            kept = [
                r[0]
                for r in con.execute(
                    sa.select(chunks.c.chunk_index)
                    .where(chunks.c.chunk_pass == "fulltext")
                    .order_by(chunks.c.chunk_index)
                ).fetchall()
            ]
        assert kept == list(range(first_run, first_run + len(kept)))

    def test_runs_that_differ_are_left_alone(self, mock_chroma, tmp_path, monkeypatch):
        """A second run over different text is not a copy, so nothing is safe to cut."""
        book = _book(tmp_path)
        self._run_twice(monkeypatch, book, second=_SECTIONS[:1])
        before = _passes(book.source_id)

        assert purge_target("duplicate_chunks", dry_run=True)["fulltext_chunks"] == 0
        purge_target("duplicate_chunks")

        assert _passes(book.source_id) == before

    def test_a_single_run_has_nothing_to_purge(self, mock_chroma, tmp_path, monkeypatch):
        book = _book(tmp_path)
        ingest_calibre_books([book])
        _extract(monkeypatch, BookExtraction(_SECTIONS))
        ingest_calibre_fulltext([book])

        assert purge_target("duplicate_chunks", dry_run=True) == {
            "documents": 0,
            "fulltext_chunks": 0,
            "summary_chunks": 0,
        }

    def test_source_scope(self, mock_chroma, tmp_path, monkeypatch):
        self._run_twice(monkeypatch, _book(tmp_path))

        assert (
            purge_target("duplicate_chunks", source=Source.ZOTERO, dry_run=True)["documents"] == 0
        )
        assert (
            purge_target("duplicate_chunks", source=Source.CALIBRE, dry_run=True)["documents"] == 1
        )
