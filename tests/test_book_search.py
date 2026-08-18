"""Third rung of the book-synopsis ladder (DESIGN.md §3.2)."""
from __future__ import annotations

import httpx
import pytest

from pka.config import settings as cfg
from pka.ingestion import book_search as bs
from pka.ingestion import openlibrary as ol


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bs._limiter, "wait", lambda: None)
    monkeypatch.setattr(ol._limiter, "wait", lambda: None)
    ol.reset_cache()
    yield
    ol.reset_cache()


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setattr(cfg, "external_lookup_enabled", True)
    monkeypatch.setattr(cfg, "cover_search_fallback", True)
    monkeypatch.setattr(cfg, "search_provider", "google_books")


def _volumes(title="Dune", authors=("Frank Herbert",), description="A desert planet."):
    info = {"title": title, "authors": list(authors)}
    if description is not None:
        info["description"] = description
    return {"items": [{"volumeInfo": info}]}


def _respond(monkeypatch, payload, status=200):
    captured = {}

    def fake_get(url, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        return httpx.Response(
            status, json=payload, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(bs.httpx, "get", fake_get)
    return captured


class TestGating:
    def test_off_by_default(self, monkeypatch):
        called = []
        monkeypatch.setattr(bs.httpx, "get", lambda *a, **k: called.append(1))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is None
        assert called == []

    def test_fallback_flag_alone_does_nothing(self, monkeypatch):
        """§1.1: a fallback must not be what first opens a network path."""
        monkeypatch.setattr(cfg, "cover_search_fallback", True)
        monkeypatch.setattr(cfg, "external_lookup_enabled", False)
        called = []
        monkeypatch.setattr(bs.httpx, "get", lambda *a, **k: called.append(1))
        assert bs.search_synopsis("Dune") is None
        assert called == []

    def test_empty_title_never_requests(self, monkeypatch, search_on):
        called = []
        monkeypatch.setattr(bs.httpx, "get", lambda *a, **k: called.append(1))
        assert bs.search_synopsis("   ") is None
        assert called == []

    def test_unknown_provider_returns_none(self, monkeypatch, search_on):
        monkeypatch.setattr(cfg, "search_provider", "not-a-backend")
        called = []
        monkeypatch.setattr(bs.httpx, "get", lambda *a, **k: called.append(1))
        assert bs.search_synopsis("Dune") is None
        assert called == []

    def test_provider_exception_is_contained(self, monkeypatch, search_on):
        def boom(title, authors):
            raise RuntimeError("provider exploded")

        monkeypatch.setitem(bs._PROVIDERS, "google_books", boom)
        assert bs.search_synopsis("Dune") is None


class TestGoogleBooks:
    def test_verified_match_accepted(self, monkeypatch, search_on):
        _respond(monkeypatch, _volumes())
        out = bs.search_synopsis("Dune", ["Frank Herbert"])
        assert out is not None
        assert out.description == "A desert planet."
        assert out.resolved_by == "google_books"

    def test_title_mismatch_rejected(self, monkeypatch, search_on):
        _respond(monkeypatch, _volumes(title="Neuromancer", authors=("William Gibson",)))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is None

    def test_author_mismatch_rejected(self, monkeypatch, search_on):
        _respond(monkeypatch, _volumes(authors=("Someone Else",)))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is None

    def test_entry_without_description_skipped(self, monkeypatch, search_on):
        _respond(monkeypatch, _volumes(description=None))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is None

    def test_no_items_returns_none(self, monkeypatch, search_on):
        _respond(monkeypatch, {"totalItems": 0})
        assert bs.search_synopsis("Obscure Thesis On Bees") is None

    def test_query_uses_extracted_fields(self, monkeypatch, search_on):
        """Deterministic and cacheable — never a model-authored query string."""
        captured = _respond(monkeypatch, _volumes())
        bs.search_synopsis("Dune", ["Frank Herbert"])
        q = captured["params"]["q"]
        assert 'intitle:"Dune"' in q
        assert 'inauthor:"Frank Herbert"' in q

    def test_api_key_sent_only_when_configured(self, monkeypatch, search_on):
        captured = _respond(monkeypatch, _volumes())
        bs.search_synopsis("Dune", ["Frank Herbert"])
        assert "key" not in captured["params"]

        monkeypatch.setattr(cfg, "search_api_key", "abc123")
        captured = _respond(monkeypatch, _volumes())
        bs.search_synopsis("Dune", ["Frank Herbert"])
        assert captured["params"]["key"] == "abc123"

    def test_http_error_returns_none(self, monkeypatch, search_on):
        def boom(url, params=None, **kwargs):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(bs.httpx, "get", boom)
        assert bs.search_synopsis("Dune") is None

    def test_bad_status_returns_none(self, monkeypatch, search_on):
        _respond(monkeypatch, {}, status=429)
        assert bs.search_synopsis("Dune") is None


class TestLadderIntegration:
    def test_third_rung_runs_when_openlibrary_misses(self, monkeypatch, search_on):
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: {"docs": []})
        _respond(monkeypatch, _volumes())
        out = ol.lookup_book(title="Dune", authors=["Frank Herbert"])
        assert out is not None
        assert out.resolved_by == "google_books"

    def test_third_rung_skipped_when_openlibrary_hits(self, monkeypatch, search_on):
        pages = {
            "/search.json": {
                "docs": [
                    {"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"]}
                ]
            },
            "/works/OL1W.json": {"description": "From the catalogue."},
        }
        monkeypatch.setattr(ol, "_get_json", lambda path, params=None: pages.get(path))
        called = []
        monkeypatch.setattr(bs.httpx, "get", lambda *a, **k: called.append(1))
        out = ol.lookup_book(title="Dune", authors=["Frank Herbert"])
        assert out is not None
        assert out.resolved_by == "search"
        assert called == [], "search rung must not run once the catalogue resolved it"

    def test_ladder_stays_off_without_the_lookup_flag(self, monkeypatch):
        monkeypatch.setattr(cfg, "cover_search_fallback", True)
        called = []
        monkeypatch.setattr(bs.httpx, "get", lambda *a, **k: called.append(1))
        monkeypatch.setattr(ol, "_get_json", lambda *a, **k: called.append(1))
        assert ol.lookup_book(title="Dune") is None
        assert called == []


class TestProviderChain:
    def test_default_chain_is_google_books_only(self, monkeypatch):
        monkeypatch.setattr(cfg, "search_provider", "google_books")
        assert bs._provider_chain() == ["google_books"]

    def test_chain_parses_and_trims(self, monkeypatch):
        monkeypatch.setattr(cfg, "search_provider", " google_books , brave ")
        assert bs._provider_chain() == ["google_books", "brave"]

    def test_empty_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(cfg, "search_provider", "")
        assert bs._provider_chain() == ["google_books"]

    def test_second_rung_runs_only_when_first_misses(self, monkeypatch, search_on):
        monkeypatch.setattr(cfg, "search_provider", "google_books,brave")
        order = []

        def first(title, authors):
            order.append("google_books")
            return None

        def second(title, authors):
            order.append("brave")
            return ol.BookSynopsis(title=title, description="From the web.",
                                   resolved_by="brave")

        monkeypatch.setitem(bs._PROVIDERS, "google_books", first)
        monkeypatch.setitem(bs._PROVIDERS, "brave", second)
        out = bs.search_synopsis("Dune", ["Frank Herbert"])
        assert out is not None and out.resolved_by == "brave"
        assert order == ["google_books", "brave"]

    def test_first_hit_short_circuits_the_chain(self, monkeypatch, search_on):
        monkeypatch.setattr(cfg, "search_provider", "google_books,brave")
        order = []

        monkeypatch.setitem(
            bs._PROVIDERS, "google_books",
            lambda t, a: (order.append("google_books")
                          or ol.BookSynopsis(title=t, description="Catalogue.",
                                             resolved_by="google_books")),
        )
        monkeypatch.setitem(
            bs._PROVIDERS, "brave", lambda t, a: order.append("brave"),
        )
        out = bs.search_synopsis("Dune")
        assert out is not None and out.resolved_by == "google_books"
        assert order == ["google_books"]

    def test_one_bad_provider_does_not_stop_the_chain(self, monkeypatch, search_on):
        monkeypatch.setattr(cfg, "search_provider", "nonsense,brave")
        monkeypatch.setitem(
            bs._PROVIDERS, "brave",
            lambda t, a: ol.BookSynopsis(title=t, description="From the web.",
                                         resolved_by="brave"),
        )
        out = bs.search_synopsis("Dune")
        assert out is not None and out.resolved_by == "brave"

    def test_raising_provider_does_not_stop_the_chain(self, monkeypatch, search_on):
        monkeypatch.setattr(cfg, "search_provider", "google_books,brave")

        def boom(title, authors):
            raise RuntimeError("down")

        monkeypatch.setitem(bs._PROVIDERS, "google_books", boom)
        monkeypatch.setitem(
            bs._PROVIDERS, "brave",
            lambda t, a: ol.BookSynopsis(title=t, description="From the web.",
                                         resolved_by="brave"),
        )
        assert bs.search_synopsis("Dune").resolved_by == "brave"


class TestBrave:
    @pytest.fixture
    def brave_on(self, monkeypatch, search_on):
        monkeypatch.setattr(cfg, "search_provider", "brave")
        monkeypatch.setattr(cfg, "search_api_key", "brave-key")

    def _web(self, title="Dune by Frank Herbert | Goodreads", desc="A desert planet epic."):
        return {"web": {"results": [{"title": title, "url": "http://x", "description": desc}]}}

    def test_skips_without_a_key(self, monkeypatch, search_on):
        monkeypatch.setattr(cfg, "search_provider", "brave")
        monkeypatch.setattr(cfg, "search_api_key", "")
        called = []
        monkeypatch.setattr(bs.httpx, "get", lambda *a, **k: called.append(1))
        assert bs.search_synopsis("Dune") is None
        assert called == [], "listing brave without a key must be harmless, not an error"

    def test_verified_hit(self, monkeypatch, brave_on):
        _respond(monkeypatch, self._web())
        out = bs.search_synopsis("Dune", ["Frank Herbert"])
        assert out is not None
        assert out.resolved_by == "brave"
        assert out.description == "A desert planet epic."

    def test_sends_the_subscription_token(self, monkeypatch, brave_on):
        captured = {}

        def fake_get(url, params=None, headers=None, **kwargs):
            captured["headers"] = headers
            captured["params"] = params
            return httpx.Response(200, json=self._web(), request=httpx.Request("GET", url))

        monkeypatch.setattr(bs.httpx, "get", fake_get)
        bs.search_synopsis("Dune", ["Frank Herbert"])
        assert captured["headers"]["X-Subscription-Token"] == "brave-key"
        assert '"Dune"' in captured["params"]["q"]

    def test_unrelated_page_rejected(self, monkeypatch, brave_on):
        _respond(monkeypatch, self._web(title="Neuromancer | Wikipedia", desc="Cyberpunk."))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is None

    def test_right_title_wrong_author_rejected(self, monkeypatch, brave_on):
        _respond(monkeypatch, self._web(title="Dune | SomePublisher", desc="A book."))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is None

    def test_author_may_appear_in_the_snippet(self, monkeypatch, brave_on):
        _respond(monkeypatch, self._web(title="Dune | SomePublisher",
                                        desc="Frank Herbert's desert epic."))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is not None

    def test_empty_snippet_skipped(self, monkeypatch, brave_on):
        _respond(monkeypatch, self._web(desc=""))
        assert bs.search_synopsis("Dune", ["Frank Herbert"]) is None

    def test_http_error_returns_none(self, monkeypatch, brave_on):
        _respond(monkeypatch, {}, status=401)
        assert bs.search_synopsis("Dune") is None
