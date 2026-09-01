"""Tests for domain extraction and frequency reporting."""

from pka.constants import FetchStatus, Source
from pka.db.queries import init_db
from pka.domains import (
    build_domain_frequency_report,
    build_domain_top_lists,
    domain_has_fetch_handler,
    extract_domain,
)
from tests.conftest import make_document


class TestExtractDomain:
    def test_https_url(self):
        assert extract_domain("https://Example.COM/path") == "example.com"

    def test_http_url(self):
        assert extract_domain("http://foo.bar/baz") == "foo.bar"

    def test_strips_www(self):
        assert extract_domain("https://www.github.com/repo") == "github.com"

    def test_missing_scheme(self):
        assert extract_domain("/local/path") is None
        assert extract_domain("example.com/page") is None

    def test_file_scheme(self):
        assert extract_domain("file:///tmp/x.html") is None

    def test_none_and_empty(self):
        assert extract_domain(None) is None
        assert extract_domain("") is None
        assert extract_domain("   ") is None


class TestDomainHasFetchHandler:
    def test_arxiv_has_handler(self):
        assert domain_has_fetch_handler("arxiv.org") is True

    def test_generic_no_handler(self):
        assert domain_has_fetch_handler("example.com") is False

    def test_wikipedia_has_handler(self):
        assert domain_has_fetch_handler("en.wikipedia.org") is True

    def test_pubmed_has_handler(self):
        assert domain_has_fetch_handler("pubmed.ncbi.nlm.nih.gov") is True

    def test_youtube_has_handler(self):
        assert domain_has_fetch_handler("www.youtube.com") is True
        assert domain_has_fetch_handler("youtu.be") is True

    def test_reddit_has_handler(self):
        assert domain_has_fetch_handler("www.reddit.com") is True
        assert domain_has_fetch_handler("redd.it") is True

    def test_search_engine_has_handler(self):
        assert domain_has_fetch_handler("google.com") is True
        assert domain_has_fetch_handler("www.bing.com") is True


class TestBuildDomainFrequencyReport:
    def test_sorts_by_count_and_aggregates_status(self):
        init_db()
        make_document(
            Source.FIREFOX,
            "f1",
            "A",
            "https://www.example.com/a",
            1,
            fetch_status=FetchStatus.FETCHED,
        )
        make_document(
            Source.FIREFOX,
            "f2",
            "B",
            "https://example.com/b",
            1,
            fetch_status=FetchStatus.PENDING,
        )
        make_document(
            Source.FIREFOX,
            "f3",
            "C",
            "https://arxiv.org/abs/2301.00001",
            1,
            fetch_status=FetchStatus.FETCHED,
        )
        make_document(
            Source.ZOTERO,
            "z1",
            "D",
            "/books/local.epub",
            1,
        )

        rows = build_domain_frequency_report(source=Source.FIREFOX)
        assert [r["domain"] for r in rows] == ["example.com", "arxiv.org"]
        assert rows[0]["count"] == 2
        assert rows[0]["by_fetch_status"] == {"fetched": 1, "pending": 1}
        assert rows[0]["has_handler"] is False
        assert rows[0]["unfetchable"] == 0
        assert rows[1]["count"] == 1
        assert rows[1]["has_handler"] is True

    def test_limit(self):
        init_db()
        make_document(Source.FIREFOX, "f1", "A", "https://a.com", 1)
        make_document(Source.FIREFOX, "f2", "B", "https://b.com", 1)

        rows = build_domain_frequency_report(limit=1)
        assert len(rows) == 1

    def test_empty_archive(self):
        init_db()
        assert build_domain_frequency_report() == []


class TestBuildDomainTopLists:
    def test_ranks_by_count_and_by_unfetchable(self):
        init_db()
        # a.com: 3 docs, 1 unfetchable
        make_document(
            Source.FIREFOX, "f1", "A1", "https://a.com/1", 1, fetch_status=FetchStatus.FETCHED
        )
        make_document(
            Source.FIREFOX, "f2", "A2", "https://a.com/2", 1, fetch_status=FetchStatus.FETCHED
        )
        make_document(
            Source.FIREFOX, "f3", "A3", "https://a.com/3", 1, fetch_status=FetchStatus.UNFETCHABLE
        )
        # b.com: 2 docs, 2 unfetchable
        make_document(
            Source.FIREFOX, "f4", "B1", "https://b.com/1", 1, fetch_status=FetchStatus.UNFETCHABLE
        )
        make_document(
            Source.FIREFOX, "f5", "B2", "https://b.com/2", 1, fetch_status=FetchStatus.UNFETCHABLE
        )
        # c.com: 1 doc, never unfetchable
        make_document(
            Source.FIREFOX, "f6", "C1", "https://c.com/1", 1, fetch_status=FetchStatus.FETCHED
        )

        result = build_domain_top_lists()
        assert [r["domain"] for r in result["top_domains"]] == ["a.com", "b.com", "c.com"]
        assert [r["domain"] for r in result["top_unfetchable"]] == ["b.com", "a.com"]
        assert result["top_unfetchable"][0]["unfetchable"] == 2
        assert result["top_unfetchable"][1]["unfetchable"] == 1

    def test_zero_unfetchable_domain_excluded_from_rejected(self):
        init_db()
        make_document(
            Source.FIREFOX, "f1", "A", "https://a.com", 1, fetch_status=FetchStatus.FETCHED
        )

        result = build_domain_top_lists()
        assert result["top_unfetchable"] == []

    def test_ties_break_on_domain_name(self):
        init_db()
        make_document(
            Source.FIREFOX, "f1", "B", "https://b.com", 1, fetch_status=FetchStatus.UNFETCHABLE
        )
        make_document(
            Source.FIREFOX, "f2", "A", "https://a.com", 1, fetch_status=FetchStatus.UNFETCHABLE
        )

        result = build_domain_top_lists()
        assert [r["domain"] for r in result["top_unfetchable"]] == ["a.com", "b.com"]

    def test_limit_truncates_both_lists_independently(self):
        init_db()
        for i in range(3):
            make_document(
                Source.FIREFOX,
                f"f{i}",
                f"D{i}",
                f"https://d{i}.com",
                1,
                fetch_status=FetchStatus.UNFETCHABLE,
            )

        result = build_domain_top_lists(limit=2)
        assert len(result["top_domains"]) == 2
        assert len(result["top_unfetchable"]) == 2

    def test_empty_archive(self):
        init_db()
        assert build_domain_top_lists() == {"top_domains": [], "top_unfetchable": []}
