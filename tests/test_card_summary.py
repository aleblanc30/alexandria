"""Tests for card summary text helpers."""
from pka.card_summary import (
    body_excerpt,
    preprint_card_summary,
    truncate_summary,
    zotero_card_summary,
)
from pka.connectors.zotero import ZoteroItem


class TestTruncateSummary:
    def test_empty_returns_empty(self):
        assert truncate_summary(None) == ""
        assert truncate_summary("") == ""

    def test_collapses_whitespace(self):
        assert truncate_summary("line one\n\nline two") == "line one line two"

    def test_truncates_with_ellipsis(self):
        text = "word " * 80
        result = truncate_summary(text, max_len=50)
        assert len(result) <= 51
        assert result.endswith("…")


class TestBodyExcerpt:
    def test_first_lines(self):
        text = "First paragraph.\n\nSecond paragraph.\nThird line.\nFourth line."
        excerpt = body_excerpt(text, max_lines=3)
        assert "First paragraph." in excerpt
        assert "Second paragraph." in excerpt
        assert "Third line." in excerpt
        assert "Fourth line." not in excerpt

    def test_single_line_fallback(self):
        assert body_excerpt("Only one line of content here.") == "Only one line of content here."


class TestPreprintCardSummary:
    def test_uses_abstract(self):
        assert preprint_card_summary("  Paper abstract.  ") == "Paper abstract."

    def test_empty_returns_none(self):
        assert preprint_card_summary(None) is None
        assert preprint_card_summary("   ") is None


class TestZoteroCardSummary:
    def test_abstract(self):
        item = ZoteroItem(
            source_id="1", title="T", authors=[], abstract="Paper abstract.",
            year=None, doi=None, url=None, item_type="journalArticle",
            collections=[], tags=[], pdf_path=None, date_added=None,
        )
        assert zotero_card_summary(item) == "Paper abstract."

    def test_annotation_uses_highlight(self):
        item = ZoteroItem(
            source_id="1", title="", authors=[], abstract=None,
            year=None, doi=None, url=None, item_type="annotation",
            collections=[], tags=[], pdf_path=None, date_added=None,
            highlight_text="Highlighted passage.",
        )
        assert zotero_card_summary(item) == "Highlighted passage."

    def test_missing_text_returns_none(self):
        item = ZoteroItem(
            source_id="1", title="T", authors=[], abstract=None,
            year=None, doi=None, url=None, item_type="journalArticle",
            collections=[], tags=[], pdf_path=None, date_added=None,
        )
        assert zotero_card_summary(item) is None
