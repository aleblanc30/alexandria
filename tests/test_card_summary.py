"""Tests for card summary text helpers."""

from pka.card_summary import (
    body_excerpt,
    clean_summary_text,
    preprint_card_summary,
    truncate_summary,
)


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

    def test_strips_markup(self):
        """Every card path runs through here, on the read side too, so stored
        rows with markup in them are cleaned without re-ingestion."""
        assert truncate_summary("<p>Plain <em>prose</em>.</p>") == "Plain prose ."


class TestCleanSummaryText:
    """Abstracts arrive as JATS or HTML from Crossref, Zotero and Calibre."""

    def test_strips_tags(self):
        raw = "<p>We report the discovery.</p> <em>Really.</em>"
        assert clean_summary_text(raw).split() == ["We", "report", "the", "discovery.", "Really."]

    def test_drops_a_leading_abstract_heading(self):
        """Stripping its tags alone would glue "Abstract" to the first sentence."""
        raw = "<h3>Abstract</h3> <p>We report the discovery.</p>"
        assert clean_summary_text(raw).split() == ["We", "report", "the", "discovery."]
        jats = "<jats:title>Abstract</jats:title><jats:p>Body text.</jats:p>"
        assert clean_summary_text(jats).split() == ["Body", "text."]

    def test_keeps_a_summary_that_merely_starts_with_the_word(self):
        assert clean_summary_text("Abstract algebra for beginners.").startswith("Abstract algebra")

    def test_strips_namespaced_and_attributed_tags(self):
        raw = '<jats:p>Body</jats:p> <a href="http://x/y">link</a>'
        assert "jats" not in clean_summary_text(raw)
        assert "href" not in clean_summary_text(raw)

    def test_unescapes_entities(self):
        assert "Smith & Jones" in clean_summary_text("Smith &amp; Jones")

    def test_leaves_comparisons_alone(self):
        """A bare ``<`` in prose is not a tag; ``p < 0.05`` must survive."""
        assert clean_summary_text("we found p < 0.05 overall") == "we found p < 0.05 overall"

    def test_escaped_markup_stays_visible(self):
        """Tags go before entities are unescaped, so text an author escaped on
        purpose is not stripped as if it were markup."""
        assert "<p>" in clean_summary_text("the &lt;p&gt; element")


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
