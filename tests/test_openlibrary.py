"""Open Library lookup ladder (DESIGN.md §3.2)."""
from __future__ import annotations

import httpx
import pytest

from pka.config import settings as cfg
from pka.ingestion import openlibrary as ol


@pytest.fixture(autouse=True)
def _isolate_lookups(monkeypatch):
    """Fresh cache per test, and never actually sleep on the rate limiter."""
    ol.reset_cache()
    monkeypatch.setattr(ol._limiter, "wait", lambda url: None)
    yield
    ol.reset_cache()


@pytest.fixture
def lookup_on(monkeypatch):
    monkeypatch.setattr(cfg, "external_lookup_enabled", True)


class TestNormalizeIsbn:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("9780306406157", "9780306406157"),
            ("978-0-306-40615-7", "9780306406157"),
            ("  978 0 306 40615 7 ", "9780306406157"),
            ("0306406152", "0306406152"),
            ("0-306-40615-2", "0306406152"),
            ("043942089x", "043942089X"),      # trailing check char folds to upper
            ("97803064061", None),             # 11 chars — neither form
            ("97803064061579", None),          # 14 chars
            ("978030640615X", None),           # X is only legal in ISBN-10
            ("", None),
            (None, None),
        ],
    )
    def test_normalize(self, raw, expected):
        assert ol.normalize_isbn(raw) == expected


class TestIsbnChecksum:
    @pytest.mark.parametrize(
        "isbn",
        ["9780306406157", "978-0-306-40615-7", "0306406152", "043942089X"],
    )
    def test_valid(self, isbn):
        assert ol.isbn_checksum_valid(isbn) is True

    @pytest.mark.parametrize(
        "isbn",
        [
            "9780306406175",   # last two digits transposed
            "9780306406158",   # wrong check digit
            "0306406153",      # wrong ISBN-10 check digit
            "not-an-isbn",
            "",
        ],
    )
    def test_invalid(self, isbn):
        assert ol.isbn_checksum_valid(isbn) is False

    def test_transposition_is_caught(self):
        """The common OCR/VLM failure mode must not reach the network."""
        assert ol.isbn_checksum_valid("9780306406157") is True
        assert ol.isbn_checksum_valid("9780306406175") is False


class TestTitleMatching:
    @pytest.mark.parametrize(
        "extracted,canonical",
        [
            ("Dune", "Dune"),
            ("dune", "DUNE"),
            ("Dune", "Dune: Book One"),            # canonical carries a subtitle
            ("Dune: Book One", "Dune"),            # and the reverse
            ("The Dispossessed", "Dispossessed"),  # leading article dropped
            ("Gödel, Escher, Bach", "Godel Escher Bach"),
        ],
    )
    def test_match(self, extracted, canonical):
        assert ol.titles_match(extracted, canonical) is True

    @pytest.mark.parametrize(
        "extracted,canonical",
        [
            ("Dune", "Neuromancer"),
            ("", "Dune"),
            ("Dune", ""),
            ("A", "Anything At All"),   # too thin to match on containment
            ("ab", "abstract algebra"),
        ],
    )
    def test_no_match(self, extracted, canonical):
        assert ol.titles_match(extracted, canonical) is False


class TestAuthorMatching:
    def test_shared_surname(self):
        assert ol.authors_match(["Frank Herbert"], ["Frank Herbert"]) is True

    def test_surname_only_is_enough(self):
        assert ol.authors_match(["F. Herbert"], ["Frank Herbert"]) is True

    def test_no_extracted_authors_is_not_a_mismatch(self):
        """Spine-only shelf photos yield a title alone; that must not reject."""
        assert ol.authors_match([], ["Frank Herbert"]) is True

    def test_disjoint_authors_reject(self):
        assert ol.authors_match(["Ursula Le Guin"], ["Frank Herbert"]) is False


class TestTrimToSentences:
    def test_trims_to_limit(self):
        text = "One thing happened. Two things happened. Three things happened. Four. Five."
        out = ol.trim_to_sentences(text, 2)
        assert out.startswith("One thing happened.")
        assert "Three" not in out

    def test_short_text_untouched(self):
        assert ol.trim_to_sentences("Just one.", 4) == "Just one."

    def test_empty(self):
        assert ol.trim_to_sentences("", 4) == ""


class TestGating:
    def test_disabled_by_default_makes_no_request(self, monkeypatch):
        """external_lookup_enabled is the single enforcement point for §1.1."""
        called = []
        monkeypatch.setattr(ol, "_get_json", lambda *a, **k: called.append(1))
        assert cfg.external_lookup_enabled is False
        assert ol.lookup_book(title="Dune", isbn="9780306406157") is None
        assert called == []


class TestLookupByIsbn:
    def test_description_on_the_edition(self, monkeypatch):
        monkeypatch.setattr(
            ol, "_get_json",
            lambda path, params=None: {"title": "Dune", "description": "A desert planet."},
        )
        out = ol.lookup_by_isbn("9780306406157")
        assert out is not None
        assert out.title == "Dune"
        assert out.description == "A desert planet."
        assert out.resolved_by == "isbn"
        assert out.isbn == "9780306406157"

    def test_falls_through_to_the_work_record(self, monkeypatch):
        pages = {
            "/isbn/9780306406157.json": {"title": "Dune", "works": [{"key": "/works/OL1W"}]},
            "/works/OL1W.json": {"description": {"value": "A desert planet."}},
        }
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: pages.get(path))
        out = ol.lookup_by_isbn("9780306406157")
        assert out is not None
        assert out.description == "A desert planet."
        assert out.work_key == "/works/OL1W"

    def test_bad_checksum_never_requests(self, monkeypatch):
        called = []
        monkeypatch.setattr(ol, "_get_json", lambda *a, **k: called.append(1))
        assert ol.lookup_by_isbn("9780306406175") is None
        assert called == []

    def test_no_description_anywhere(self, monkeypatch):
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: {"title": "Dune"})
        assert ol.lookup_by_isbn("9780306406157") is None


class TestLookupByTitleAuthor:
    def _pages(self, docs, description="A desert planet."):
        return {
            "/search.json": {"docs": docs},
            "/works/OL1W.json": {"description": description},
        }

    def test_verified_match_accepted(self, monkeypatch):
        pages = self._pages(
            [{"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"]}]
        )
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: pages.get(path))
        out = ol.lookup_by_title_author("Dune", ["Frank Herbert"])
        assert out is not None
        assert out.resolved_by == "search"
        assert out.work_key == "/works/OL1W"

    def test_rank_one_with_wrong_title_is_rejected(self, monkeypatch):
        """Trust agreement, not ranking — this is the wrong-synopsis failure mode."""
        pages = self._pages(
            [{"key": "/works/OL9W", "title": "Neuromancer", "author_name": ["William Gibson"]}]
        )
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: pages.get(path))
        assert ol.lookup_by_title_author("Dune", ["Frank Herbert"]) is None

    def test_author_mismatch_is_rejected(self, monkeypatch):
        pages = self._pages(
            [{"key": "/works/OL1W", "title": "Dune", "author_name": ["Someone Else"]}]
        )
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: pages.get(path))
        assert ol.lookup_by_title_author("Dune", ["Frank Herbert"]) is None

    def test_skips_unverified_hit_and_takes_the_verified_one(self, monkeypatch):
        pages = self._pages([
            {"key": "/works/OL9W", "title": "Neuromancer", "author_name": ["William Gibson"]},
            {"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"]},
        ])
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: pages.get(path))
        out = ol.lookup_by_title_author("Dune", ["Frank Herbert"])
        assert out is not None
        assert out.work_key == "/works/OL1W"

    def test_thin_title_never_requests(self, monkeypatch):
        called = []
        monkeypatch.setattr(ol, "_get_json", lambda *a, **k: called.append(1))
        assert ol.lookup_by_title_author("A", []) is None
        assert called == []


class TestLadderAndCache:
    def test_bad_isbn_falls_through_to_search(self, monkeypatch, lookup_on):
        paths: list[str] = []

        def fake(path, params=None):
            paths.append(path)
            if path == "/search.json":
                return {
                    "docs": [
                        {"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"]}
                    ]
                }
            return {"description": "A desert planet."}

        monkeypatch.setattr(ol, "_get_json", fake)
        out = ol.lookup_book(title="Dune", authors=["Frank Herbert"], isbn="9780306406175")
        assert out is not None
        assert out.resolved_by == "search"
        # The failed-checksum ISBN must not have been requested.
        assert not any(p.startswith("/isbn/") for p in paths)

    def test_isbn_rung_wins_when_valid(self, monkeypatch, lookup_on):
        paths: list[str] = []

        def fake(path, params=None):
            paths.append(path)
            return {"title": "Dune", "description": "A desert planet."}

        monkeypatch.setattr(ol, "_get_json", fake)
        out = ol.lookup_book(title="Dune", isbn="978-0-306-40615-7")
        assert out is not None
        assert out.resolved_by == "isbn"
        assert paths == ["/isbn/9780306406157.json"]

    def test_result_is_cached(self, monkeypatch, lookup_on):
        calls = []

        def fake(path, params=None):
            calls.append(path)
            return {"title": "Dune", "description": "A desert planet."}

        monkeypatch.setattr(ol, "_get_json", fake)
        first = ol.lookup_book(title="Dune", isbn="9780306406157")
        second = ol.lookup_book(title="Dune", isbn="9780306406157")
        assert first == second
        assert len(calls) == 1

    def test_negative_result_is_cached(self, monkeypatch, lookup_on):
        """A shelf of unknown books must not re-request each one on every run."""
        calls = []

        def fake(path, params=None):
            calls.append(path)
            return {"docs": []}

        monkeypatch.setattr(ol, "_get_json", fake)
        assert ol.lookup_book(title="Obscure Thesis On Bees") is None
        assert ol.lookup_book(title="Obscure Thesis On Bees") is None
        assert len(calls) == 1

    def test_embed_text_respects_sentence_cap(self, monkeypatch, lookup_on):
        long_desc = " ".join(f"Sentence number {i} is here." for i in range(10))
        monkeypatch.setattr(
            ol, "_get_json",
            lambda path, params=None: {"title": "Dune", "description": long_desc},
        )
        monkeypatch.setattr(cfg, "summary_max_sentences", 3)
        out = ol.lookup_book(title="Dune", isbn="9780306406157")
        assert out is not None
        assert out.embed_text().count("Sentence number") == 3


class TestGetJsonErrorHandling:
    def test_http_error_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(ol.httpx, "get", boom)
        assert ol._get_json("/isbn/9780306406157.json") is None

    def test_status_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            ol.httpx, "get",
            lambda *a, **k: httpx.Response(404, request=httpx.Request("GET", "http://x")),
        )
        assert ol._get_json("/isbn/9780306406157.json") is None

    def test_non_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            ol.httpx, "get",
            lambda *a, **k: httpx.Response(
                200, text="<html>nope</html>", request=httpx.Request("GET", "http://x")
            ),
        )
        assert ol._get_json("/isbn/9780306406157.json") is None
