"""Tests for the ``direct.mit.edu`` handler.

The host was probed on 2026-09-03 and returns ``403`` to a non-browser client on
both an article and a book page, so there is no page to scrape and no DOI in the
path. Three URL shapes, three routes: an ``article-pdf`` filename *is* the DOI
suffix and is looked up directly; an article page is resolved by a *ranked*
Crossref query; a book page goes to Open Library by title.

Both article routes are only admissible because the URL independently supplies
volume, issue and first page to check the answer against — a derived DOI is no
more trusted than a ranked guess. The tests that matter most here are the ones
proving that check actually rejects things.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.direct_mit import (
    DirectMitArticle,
    citation_matches,
    doi_from_pdf_name,
    fetch_direct_mit,
    is_direct_mit_url,
    parse_direct_mit_url,
    slug_title,
)


def _json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


def _requested_hosts(client: AsyncMock) -> set[str]:
    return {
        httpx.URL(call.args[0] if call.args else call.kwargs["url"]).host
        for call in client.get.call_args_list
    }


_LSTM_URL = "https://direct.mit.edu/neco/article/9/8/1735/6109/Long-Short-Term-Memory"
_BOOK_URL = "https://direct.mit.edu/books/monograph/2313/The-Alignment-Problem"
_PDF_URL = "https://direct.mit.edu/neco/article-pdf/9/8/1735/813796/neco.1997.9.8.1735.pdf"


class TestParsing:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (_LSTM_URL, DirectMitArticle("neco", "9", "8", "1735", "Long Short Term Memory")),
            (
                "https://direct.mit.edu/jocn/article-abstract/23/4/978/5231/The-Neural-Substrates",
                DirectMitArticle("jocn", "23", "4", "978", "The Neural Substrates"),
            ),
        ],
    )
    def test_article_coordinates(self, url, expected):
        assert parse_direct_mit_url(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected_doi"),
        [
            (
                "https://direct.mit.edu/neco/article-pdf/9/8/1735/813796/neco.1997.9.8.1735.pdf",
                "10.1162/neco.1997.9.8.1735",
            ),
            (
                "https://direct.mit.edu/neco/article-pdf/31/11/2212/1234/neco_a_01227.pdf",
                "10.1162/neco_a_01227",
            ),
        ],
    )
    def test_pdf_filename_is_the_doi_suffix(self, url, expected_doi):
        """Legacy and modern suffix forms both fall out of the filename."""
        parsed = parse_direct_mit_url(url)
        assert isinstance(parsed, DirectMitArticle)
        assert parsed.doi == expected_doi
        assert parsed.title == ""  # this shape carries no title slug

    def test_generic_pdf_filename_is_not_a_doi_suffix(self):
        assert (
            parse_direct_mit_url(
                "https://direct.mit.edu/neco/article-pdf/31/11/2212/1234/paper.pdf"
            )
            is None
        )

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("neco.1997.9.8.1735.pdf", "10.1162/neco.1997.9.8.1735"),
            ("neco_a_01227.pdf", "10.1162/neco_a_01227"),
            ("neco_a_01227", "10.1162/neco_a_01227"),
            ("paper.pdf", None),
            ("", None),
            (".pdf", None),
        ],
    )
    def test_doi_from_pdf_name(self, name, expected):
        assert doi_from_pdf_name(name) == expected

    def test_book_yields_its_title(self):
        assert parse_direct_mit_url(_BOOK_URL) == "The Alignment Problem"

    @pytest.mark.parametrize(
        "url",
        [
            "https://direct.mit.edu/neco",
            "https://direct.mit.edu/search-results?q=memory",
            "https://direct.mit.edu/",
        ],
    )
    def test_index_pages_fall_through(self, url):
        assert parse_direct_mit_url(url) is None

    def test_is_a_separate_host_from_mitpress(self):
        assert is_direct_mit_url("https://direct.mit.edu/")
        assert not is_direct_mit_url("https://mitpress.mit.edu/")

    def test_slug_title_keeps_casing(self):
        # Title-casing would corrupt acronyms, as on ResearchGate.
        assert slug_title("Deep-Learning-and-GPT-Models") == "Deep Learning and GPT Models"


class TestCitationVerification:
    def test_requires_all_three_coordinates(self):
        article = DirectMitArticle("neco", "9", "8", "1735", "Long Short Term Memory")
        assert citation_matches({"volume": "9", "issue": "8", "page": "1735-1780"}, article)
        # Rank 2 of the live query: same volume, different issue and page.
        assert not citation_matches({"volume": "9", "issue": "6", "page": "734-742"}, article)
        assert not citation_matches({"volume": "31", "issue": "8", "page": "1735"}, article)
        assert not citation_matches({"volume": "9", "issue": "8"}, article)


class TestFetchDirectMit:
    @pytest.mark.asyncio
    async def test_accepts_the_candidate_that_round_trips_not_rank_one(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(
            {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1162/jocn.1997.9.6.734",
                            "title": ["Visual Imagery"],
                            "volume": "9",
                            "issue": "6",
                            "page": "734-742",
                        },
                        {
                            "DOI": "10.1162/neco.1997.9.8.1735",
                            "title": ["Long Short-Term Memory"],
                            "volume": "9",
                            "issue": "8",
                            "page": "1735-1780",
                            "abstract": "<jats:p>We introduce LSTM.</jats:p>",
                            "issued": {"date-parts": [[1997]]},
                        },
                    ]
                }
            }
        )

        result = await fetch_direct_mit(client, 1, _LSTM_URL)

        assert result is not None
        assert result.status == "fetched"
        assert result.doi == "10.1162/neco.1997.9.8.1735"
        assert result.title == "Long Short-Term Memory"
        assert result.card_summary == "We introduce LSTM."
        assert "direct.mit.edu" not in _requested_hosts(client)

    @pytest.mark.asyncio
    async def test_nothing_round_trips_yields_a_slug_card_not_a_wrong_match(self):
        """Why a ranked query is admissible here at all — it can be refused."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(
            {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1162/wrong.paper",
                            "title": ["A Confidently Ranked Wrong Paper"],
                            "volume": "31",
                            "issue": "11",
                            "page": "2212-2251",
                            "abstract": "<jats:p>Someone else's abstract.</jats:p>",
                        }
                    ]
                }
            }
        )

        result = await fetch_direct_mit(client, 1, _LSTM_URL)

        assert result is not None
        assert result.status == "fetched"
        assert result.doi is None  # nothing borrowed from the wrong record
        assert result.card_summary == "MIT Press article: Long Short Term Memory."
        assert result.title == "Long Short Term Memory"
        assert "unverified" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_empty_result_set_yields_the_slug_card(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response({"message": {"items": []}})

        result = await fetch_direct_mit(client, 1, _LSTM_URL)

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "Long Short Term Memory"

    @pytest.mark.asyncio
    async def test_flag_off_yields_a_slug_card_with_no_request(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "doi_metadata_lookup", False)
        client = AsyncMock(spec=httpx.AsyncClient)

        result = await fetch_direct_mit(client, 1, _LSTM_URL)

        assert result is not None
        assert result.status == "fetched"
        assert client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_pdf_filename_resolves_in_one_request(self):
        """No search: the filename spells the DOI, so it is looked up directly."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(
            {
                "message": {
                    "DOI": "10.1162/neco.1997.9.8.1735",
                    "title": ["Long Short-Term Memory"],
                    "volume": "9",
                    "issue": "8",
                    "page": "1735-1780",
                    "abstract": "<jats:p>We introduce LSTM.</jats:p>",
                    "issued": {"date-parts": [[1997]]},
                }
            }
        )

        result = await fetch_direct_mit(client, 1, _PDF_URL)

        assert client.get.call_count == 1
        assert result is not None
        assert result.doi == "10.1162/neco.1997.9.8.1735"
        assert result.title == "Long Short-Term Memory"
        assert "direct.mit.edu" not in _requested_hosts(client)

    @pytest.mark.asyncio
    async def test_derived_doi_naming_another_work_is_rejected(self):
        """The derivation is checked, not trusted — same rule as the query path."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(
            {
                "message": {
                    "DOI": "10.1162/neco.1997.9.8.1735",
                    "title": ["Some Other Paper Entirely"],
                    "volume": "31",
                    "issue": "11",
                    "page": "2212-2251",
                }
            }
        )

        assert await fetch_direct_mit(client, 1, _PDF_URL) is None

    @pytest.mark.asyncio
    async def test_derived_doi_that_does_not_resolve_falls_through(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response({}, status=404)

        assert await fetch_direct_mit(client, 1, _PDF_URL) is None

    @pytest.mark.asyncio
    async def test_pdf_shape_with_lookups_off_falls_through(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "doi_metadata_lookup", False)
        client = AsyncMock(spec=httpx.AsyncClient)

        # No title slug on this shape, so there is no honest card to build.
        assert await fetch_direct_mit(client, 1, _PDF_URL) is None
        assert client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_book_resolves_through_the_open_library_title_ladder(self, monkeypatch):
        from pka.config import settings as cfg
        from pka.ingestion.openlibrary import BookSynopsis

        monkeypatch.setattr(cfg, "external_lookup_enabled", True)
        seen: list[str] = []

        def _lookup(title, authors=None):
            seen.append(title)
            return BookSynopsis(
                title="The Alignment Problem",
                description="Machine learning and human values.",
                resolved_by="search",
            )

        monkeypatch.setattr("pka.ingestion.openlibrary.lookup_by_title_author", _lookup)

        result = await fetch_direct_mit(AsyncMock(spec=httpx.AsyncClient), 1, _BOOK_URL)

        assert seen == ["The Alignment Problem"]
        assert result is not None
        assert result.title == "The Alignment Problem"
        assert result.card_summary == "Machine learning and human values."

    @pytest.mark.asyncio
    async def test_book_with_no_verified_match_falls_back_to_the_slug(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "external_lookup_enabled", True)
        monkeypatch.setattr(
            "pka.ingestion.openlibrary.lookup_by_title_author",
            lambda title, authors=None: None,
        )

        result = await fetch_direct_mit(AsyncMock(spec=httpx.AsyncClient), 1, _BOOK_URL)

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "The Alignment Problem"
        assert "no verified open library match" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_book_with_lookups_off_makes_no_request(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "external_lookup_enabled", False)

        result = await fetch_direct_mit(AsyncMock(spec=httpx.AsyncClient), 1, _BOOK_URL)

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "The Alignment Problem"
