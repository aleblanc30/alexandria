"""Tests for card summary text helpers."""

from pka.card_summary import body_excerpt, preprint_card_summary, truncate_summary


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
