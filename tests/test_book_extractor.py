"""Tests for book text extraction helpers."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestStripHtml:
    def test_removes_tags(self):
        from pka.ingestion.book_extractor import strip_html
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_collapses_whitespace(self):
        from pka.ingestion.book_extractor import strip_html
        assert "a  b" not in strip_html("<div>a    b</div>")


class TestMetadataText:
    def test_includes_title_authors_description(self):
        from pka.ingestion.book_extractor import metadata_text
        text = metadata_text(
            "My Book", "<p>A long enough description for embedding.</p>", ["Alice", "Bob"],
        )
        assert "My Book" in text
        assert "Alice" in text
        assert "description" in text


class TestExtractBookText:
    def test_unsupported_extension_returns_empty(self, tmp_path):
        from pka.ingestion.book_extractor import extract_book_text
        path = tmp_path / "book.mobi"
        path.write_bytes(b"x")
        assert extract_book_text(path) == []


class TestExtractPdf:
    def test_groups_pages_in_blocks_of_ten(self, monkeypatch, tmp_path):
        path = tmp_path / "sample.pdf"
        path.write_bytes(b"%PDF")

        class FakePage:
            def __init__(self, text: str):
                self._text = text

            def extract_text(self, **kw):
                return self._text

        class FakePdf:
            def __init__(self, pages):
                self.pages = pages

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        pages = [FakePage(f"Page {i} content here.") for i in range(15)]
        monkeypatch.setattr(
            "pdfplumber.open",
            lambda p: FakePdf(pages),
        )

        from pka.ingestion.book_extractor import extract_pdf
        groups = extract_pdf(path)
        assert len(groups) == 2
        assert groups[0]["title"] == "Pages 1–10"
        assert groups[1]["title"] == "Pages 11–15"

    def test_respects_max_pages(self, monkeypatch, tmp_path):
        path = tmp_path / "short.pdf"
        path.write_bytes(b"%PDF")

        class FakePage:
            def extract_text(self, **kw):
                return "text"

        class FakePdf:
            pages = [FakePage(), FakePage(), FakePage()]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr("pdfplumber.open", lambda p: FakePdf())

        from pka.ingestion.book_extractor import extract_pdf
        groups = extract_pdf(path, max_pages=1)
        assert len(groups) == 1


class TestExtractEpub:
    def test_extracts_chapter_text(self, tmp_path):
        from ebooklib import epub

        path = tmp_path / "book.epub"
        book = epub.EpubBook()
        book.set_identifier("test-id")
        book.set_title("Test EPUB")
        book.set_language("en")

        chapter = epub.EpubHtml(
            title="Intro",
            file_name="intro.xhtml",
            lang="en",
        )
        chapter.content = (
            "<html><body><h1>Intro</h1>"
            "<p>First chapter with enough text to extract cleanly.</p>"
            "</body></html>"
        )
        book.add_item(chapter)
        book.spine = ["nav", chapter]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(path), book, {})

        from pka.ingestion.book_extractor import extract_epub
        chapters = extract_epub(path)
        assert len(chapters) >= 1
        assert "First chapter" in chapters[0]["text"]

    def test_missing_file_returns_empty(self, tmp_path):
        from pka.ingestion.book_extractor import extract_epub
        assert extract_epub(tmp_path / "missing.epub") == []


class TestExtractPdfFallback:
    def test_pypdf_fallback_when_pdfplumber_fails(self, monkeypatch, tmp_path):
        path = tmp_path / "book.pdf"
        path.write_bytes(b"%PDF-1.4")

        monkeypatch.setattr(
            "pdfplumber.open",
            lambda p: (_ for _ in ()).throw(RuntimeError("pdfplumber fail")),
        )

        class FakePage:
            def extract_text(self):
                return "Page one from pypdf. " * 5

        class FakeReader:
            pages = [FakePage(), FakePage()]

        monkeypatch.setattr("pypdf.PdfReader", lambda p: FakeReader())

        from pka.ingestion.book_extractor import extract_pdf
        groups = extract_pdf(path)
        assert len(groups) == 1
        assert "pypdf" in groups[0]["text"]

