"""Tests for domain extraction and frequency reporting."""
from pka.constants import FetchStatus, Source
from pka.db.queries import init_db, upsert_document
from pka.domains import (
    build_domain_frequency_report,
    domain_has_fetch_handler,
    extract_domain,
)


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


class TestBuildDomainFrequencyReport:
    def test_sorts_by_count_and_aggregates_status(self):
        init_db()
        upsert_document(
            Source.FIREFOX, "f1", "A", "https://www.example.com/a", 1,
            fetch_status=FetchStatus.FETCHED,
        )
        upsert_document(
            Source.FIREFOX, "f2", "B", "https://example.com/b", 1,
            fetch_status=FetchStatus.PENDING,
        )
        upsert_document(
            Source.FIREFOX, "f3", "C", "https://arxiv.org/abs/2301.00001", 1,
            fetch_status=FetchStatus.FETCHED,
        )
        upsert_document(
            Source.ZOTERO, "z1", "D", "/books/local.epub", 1,
        )

        rows = build_domain_frequency_report(source=Source.FIREFOX)
        assert [r["domain"] for r in rows] == ["example.com", "arxiv.org"]
        assert rows[0]["count"] == 2
        assert rows[0]["by_fetch_status"] == {"fetched": 1, "pending": 1}
        assert rows[0]["has_handler"] is False
        assert rows[1]["count"] == 1
        assert rows[1]["has_handler"] is True

    def test_limit(self):
        init_db()
        upsert_document(Source.FIREFOX, "f1", "A", "https://a.com", 1)
        upsert_document(Source.FIREFOX, "f2", "B", "https://b.com", 1)

        rows = build_domain_frequency_report(limit=1)
        assert len(rows) == 1

    def test_empty_archive(self):
        init_db()
        assert build_domain_frequency_report() == []
