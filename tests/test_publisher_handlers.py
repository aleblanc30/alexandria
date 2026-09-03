"""Tests for the publisher fetch handlers (``PUBLISHER_FETCH_HANDLERS.md`` §5–§10).

One class per handler. The URL tables in the plan turn directly into
parametrised cases; the tests that carry an argument beyond parsing are called
out in their own docstrings — notably the Springer no-abstract case (§8.1), the
ScienceDirect unresolved-PII card (§8.5), and the "no GET against the publisher
host" regressions that encode why these handlers exist at all.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.aps import fetch_aps_article, is_aps_url, parse_aps_url
from pka.ingestion.doi_org import fetch_doi_url, is_doi_host, parse_doi_url
from pka.ingestion.mitpress import (
    fetch_mitpress_book,
    is_mitpress_url,
    parse_mitpress_url,
    title_from_slug,
)
from pka.ingestion.nature import fetch_nature_article, is_nature_url, parse_nature_url
from pka.ingestion.researchgate import (
    is_researchgate_url,
    parse_researchgate_url,
    researchgate_result,
)
from pka.ingestion.sciencedirect import (
    fetch_sciencedirect_article,
    is_sciencedirect_url,
    parse_sciencedirect_url,
)
from pka.ingestion.springer import (
    fetch_springer_article,
    is_springer_url,
    parse_springer_url,
)


def _json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


def _work(doi: str, title: str, *, abstract: str | None = None) -> dict:
    message: dict = {
        "DOI": doi,
        "title": [title],
        "type": "journal-article",
        "issued": {"date-parts": [[2016]]},
        "author": [{"given": "Ada", "family": "Lovelace"}],
    }
    if abstract:
        message["abstract"] = f"<jats:p>{abstract}</jats:p>"
    return {"message": message}


def _requested_hosts(client: AsyncMock) -> set[str]:
    return {
        httpx.URL(call.args[0] if call.args else call.kwargs["url"]).host
        for call in client.get.call_args_list
    }


# ── §5 doi.org ───────────────────────────────────────────────────────────────


class TestDoiOrg:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://doi.org/10.1103/PhysRevLett.116.061102", "10.1103/PhysRevLett.116.061102"),
            ("https://dx.doi.org/10.1016/j.artint.2018.07.007", "10.1016/j.artint.2018.07.007"),
            ("https://www.doi.org/10.1038/s41586-020-2649-2", "10.1038/s41586-020-2649-2"),
            ("https://doi.org/10.1007/978-3-030-01234-5_7", "10.1007/978-3-030-01234-5_7"),
        ],
    )
    def test_parses_the_whole_suffix_including_slashes(self, url, expected):
        assert parse_doi_url(url) == expected

    def test_case_is_preserved_for_the_request(self):
        """``normalize_doi`` lowercases for the column; the URL keeps its case."""
        assert (
            parse_doi_url("https://doi.org/10.1103/PhysRevLett.116.061102")
            == "10.1103/PhysRevLett.116.061102"
        )

    @pytest.mark.parametrize(
        "url",
        ["https://doi.org/", "https://doi.org/about", "https://example.com/10.1/x"],
    )
    def test_non_doi_paths_fall_through(self, url):
        assert parse_doi_url(url) is None

    def test_is_doi_host(self):
        assert is_doi_host("https://doi.org/10.1/x")
        assert is_doi_host("https://dx.doi.org/")
        assert not is_doi_host("https://hdl.handle.net/2027/x")

    @pytest.mark.asyncio
    async def test_resolves_by_content_negotiation(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(
            {
                "DOI": "10.5281/zenodo.1234567",
                "title": "A dataset, not a paper",
                "type": "dataset",
                "abstract": "Measurements from the thing.",
                "issued": {"date-parts": [[2021]]},
            }
        )

        result = await fetch_doi_url(client, 1, "https://doi.org/10.5281/zenodo.1234567")

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "A dataset, not a paper"
        assert result.doi == "10.5281/zenodo.1234567"
        assert _requested_hosts(client) == {"doi.org"}
        assert "doi.org" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_non_doi_url_returns_none(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        assert await fetch_doi_url(client, 1, "https://example.com/") is None


# ── §6 nature.com ────────────────────────────────────────────────────────────


class TestNature:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.nature.com/articles/s41586-020-2649-2", "10.1038/s41586-020-2649-2"),
            ("https://www.nature.com/articles/nature12373", "10.1038/nature12373"),
            (
                "https://www.nature.com/nature/journal/v491/n7422/full/nature11421.html",
                "10.1038/nature11421",
            ),
            ("https://nature.com/nmeth/articles/s41592-019-0686-2", "10.1038/s41592-019-0686-2"),
            ("https://www.nature.com/articles/d41586-020-02462-7", "10.1038/d41586-020-02462-7"),
        ],
    )
    def test_derives_the_doi_by_concatenation(self, url, expected):
        assert parse_nature_url(url) == expected

    def test_legacy_supplement_marker_resolves_to_the_article(self):
        url = "https://www.nature.com/nature/journal/v491/n7422/full/nature11421_S1.html"
        assert parse_nature_url(url) == "10.1038/nature11421"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.nature.com/subjects/genetics",
            "https://www.nature.com/nature/volumes/491",
            "https://www.nature.com/collections/abcdef",
            "https://www.nature.com/",
            "https://www.scientificamerican.com/article/something/",
        ],
    )
    def test_index_pages_fall_through_to_the_generic_path(self, url):
        assert parse_nature_url(url) is None

    def test_is_nature_url(self):
        assert is_nature_url("https://www.nature.com/articles/x")
        assert not is_nature_url("https://www.scientificamerican.com/")

    @pytest.mark.asyncio
    async def test_builds_a_card_without_touching_nature_com(self):
        """The regression that encodes why this handler exists: no paywall scrape."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(
            _work("10.1038/s41586-020-2649-2", "Array programming with NumPy", abstract="A syntax.")
        )

        result = await fetch_nature_article(
            client, 1, "https://www.nature.com/articles/s41586-020-2649-2"
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "Array programming with NumPy"
        assert result.card_summary == "A syntax."
        assert result.year == 2016
        assert json.loads(result.authors_json) == ["Ada Lovelace"]
        assert "nature.com" not in _requested_hosts(client)

    @pytest.mark.asyncio
    async def test_metadata_miss_is_unfetchable_not_a_fallthrough(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response({}, status=404)

        result = await fetch_nature_article(
            client, 1, "https://www.nature.com/articles/s41586-020-2649-2"
        )

        assert result is not None
        assert result.status == "unfetchable"
        assert "nature.com" not in _requested_hosts(client)


# ── §7 journals.aps.org ──────────────────────────────────────────────────────


class TestAps:
    @pytest.mark.parametrize(
        "url",
        [
            "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.116.061102",
            "https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.116.061102",
            "https://journals.aps.org/prl/supplemental/10.1103/PhysRevLett.116.061102",
            "https://journals.aps.org/rmp/cited-by/10.1103/PhysRevLett.116.061102",
            "https://link.aps.org/doi/10.1103/PhysRevLett.116.061102",
            "https://link.aps.org/accepted/10.1103/PhysRevLett.116.061102",
        ],
    )
    def test_every_view_and_both_hosts_yield_the_same_doi(self, url):
        assert parse_aps_url(url) == "10.1103/PhysRevLett.116.061102"

    def test_foreign_prefix_falls_through(self):
        assert (
            parse_aps_url("https://journals.aps.org/prl/abstract/10.1016/j.x.2018.01.001") is None
        )

    def test_index_page_falls_through(self):
        assert parse_aps_url("https://journals.aps.org/prl/") is None

    def test_is_aps_url_matches_both_hosts(self):
        assert is_aps_url("https://journals.aps.org/")
        assert is_aps_url("https://link.aps.org/")
        assert not is_aps_url("https://aps.org/")

    @pytest.mark.asyncio
    async def test_builds_a_card_without_a_403(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(
            _work(
                "10.1103/PhysRevLett.116.061102",
                "Observation of Gravitational Waves",
                abstract="On September 14, 2015 the detectors observed a signal.",
            )
        )

        result = await fetch_aps_article(
            client, 1, "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.116.061102"
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.doi == "10.1103/physrevlett.116.061102"
        assert _requested_hosts(client) == {"api.crossref.org"}


# ── §8.1 link.springer.com ───────────────────────────────────────────────────


class TestSpringer:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://link.springer.com/article/10.1007/s11263-015-0816-y",
                "10.1007/s11263-015-0816-y",
            ),
            (
                "https://link.springer.com/chapter/10.1007/978-3-030-01234-5_7",
                "10.1007/978-3-030-01234-5_7",
            ),
            (
                "https://link.springer.com/content/pdf/10.1007/s11263-015-0816-y.pdf",
                "10.1007/s11263-015-0816-y",
            ),
            (
                "https://link.springer.com/article/10.1007%2Fs11263-015-0816-y",
                "10.1007/s11263-015-0816-y",
            ),
        ],
    )
    def test_reads_the_doi_out_of_the_path(self, url, expected):
        assert parse_springer_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://link.springer.com/journal/11263",
            "https://link.springer.com/search?query=vision",
            "https://link.springer.com/",
            "https://www.springer.com/gp/book/9783030012345",
            "https://www.springeropen.com/articles/10.1186/s12345-020-1",
        ],
    )
    def test_index_pages_and_excluded_hosts_fall_through(self, url):
        assert parse_springer_url(url) is None

    def test_is_springer_url(self):
        assert is_springer_url("https://link.springer.com/")
        assert not is_springer_url("https://springer.com/")

    @pytest.mark.asyncio
    async def test_crossref_without_an_abstract_climbs_to_semantic_scholar(self):
        """The most important test in the set — see ``PUBLISHER_FETCH_HANDLERS.md`` §8.1.

        Crossref has no abstract for ``10.1007`` deposits. Built "Crossref only",
        a large slice of SpringerLink bookmarks would silently land as
        abstract-less cards.
        """
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = [
            _json_response(_work("10.1007/s11263-015-0816-y", "ImageNet Large Scale Challenge")),
            _json_response(
                {
                    "abstract": "We describe the creation of this benchmark dataset.",
                    "externalIds": {"ArXiv": "1409.0575"},
                }
            ),
        ]

        result = await fetch_springer_article(
            client, 1, "https://link.springer.com/article/10.1007/s11263-015-0816-y"
        )

        assert result is not None
        assert result.card_summary == "We describe the creation of this benchmark dataset."
        assert result.arxiv_id == "1409.0575"
        assert "semantic scholar" in (result.error_msg or "")
        assert "link.springer.com" not in _requested_hosts(client)


# ── §8.2/§8.5 sciencedirect.com ──────────────────────────────────────────────


class TestScienceDirect:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://www.sciencedirect.com/science/article/pii/S0004370218305988",
                "S0004370218305988",
            ),
            (
                "https://www.sciencedirect.com/science/article/abs/pii/S0004370218305988",
                "S0004370218305988",
            ),
            (
                "https://www.sciencedirect.com/science/article/pii/S0004370218305988/pdfft",
                "S0004370218305988",
            ),
            (
                "https://linkinghub.elsevier.com/retrieve/pii/S0004370218305988",
                "S0004370218305988",
            ),
        ],
    )
    def test_reads_the_pii(self, url, expected):
        assert parse_sciencedirect_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.sciencedirect.com/journal/artificial-intelligence",
            "https://www.sciencedirect.com/search?qs=vision",
            "https://www.sciencedirect.com/",
        ],
    )
    def test_index_pages_fall_through(self, url):
        assert parse_sciencedirect_url(url) is None

    def test_is_sciencedirect_url_covers_linkinghub(self):
        assert is_sciencedirect_url("https://www.sciencedirect.com/")
        assert is_sciencedirect_url("https://linkinghub.elsevier.com/")

    @pytest.mark.asyncio
    async def test_pii_resolves_through_the_alternative_id_filter(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = [
            _json_response(
                {
                    "message": {
                        "items": [
                            {
                                "DOI": "10.1016/j.artint.2018.07.007",
                                "title": ["Bridging machine learning and logic"],
                                "issued": {"date-parts": [[2018]]},
                                "author": [{"given": "Ada", "family": "Lovelace"}],
                            }
                        ]
                    }
                }
            ),
            _json_response({"abstract": "A survey of the boundary."}),
        ]

        result = await fetch_sciencedirect_article(
            client, 1, "https://www.sciencedirect.com/science/article/pii/S0004370218305988"
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.doi == "10.1016/j.artint.2018.07.007"
        assert result.card_summary == "A survey of the boundary."
        assert "sciencedirect.com" not in _requested_hosts(client)

    @pytest.mark.asyncio
    async def test_unresolvable_old_pii_yields_a_url_derived_card(self):
        """§8.5: Elsevier's ``alternative-id`` deposits are not retroactive."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response({"message": {"items": []}})

        result = await fetch_sciencedirect_article(
            client, 1, "https://www.sciencedirect.com/science/article/pii/S0004370200000521"
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.http_status is None
        assert "S0004370200000521" in (result.card_summary or "")
        assert "unresolved" in (result.error_msg or "")
        assert "sciencedirect.com" not in _requested_hosts(client)

    @pytest.mark.asyncio
    async def test_flag_off_yields_the_card_with_no_request(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "doi_metadata_lookup", False)
        client = AsyncMock(spec=httpx.AsyncClient)

        result = await fetch_sciencedirect_article(
            client, 1, "https://www.sciencedirect.com/science/article/pii/S0004370218305988"
        )

        assert result is not None
        assert result.status == "fetched"
        assert client.get.call_count == 0


# ── §9 mitpress.mit.edu ──────────────────────────────────────────────────────


class TestMitPress:
    @pytest.mark.parametrize(
        ("url", "isbn", "slug"),
        [
            (
                "https://mitpress.mit.edu/9780262533256/raised-to-rage/",
                "9780262533256",
                "raised-to-rage",
            ),
            ("https://mitpress.mit.edu/9780262536332/spaceflight/", "9780262536332", "spaceflight"),
            ("https://mitpress.mit.edu/9780262192026/", "9780262192026", None),
        ],
    )
    def test_reads_isbn_and_slug(self, url, isbn, slug):
        assert parse_mitpress_url(url) == (isbn, slug)

    def test_non_book_paths_fall_through(self):
        assert parse_mitpress_url("https://mitpress.mit.edu/books/subject/economics") is None
        assert parse_mitpress_url("https://mitpress.mit.edu/") is None

    def test_is_mitpress_url_excludes_the_journals_gateway(self):
        assert is_mitpress_url("https://mitpress.mit.edu/")
        assert not is_mitpress_url("https://direct.mit.edu/neco/article/9/8/1735")

    def test_title_from_slug(self):
        assert title_from_slug("raised-to-rage") == "Raised To Rage"
        assert title_from_slug(None) is None

    @pytest.mark.asyncio
    async def test_flag_off_builds_a_slug_card_with_zero_requests(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "external_lookup_enabled", False)
        client = AsyncMock(spec=httpx.AsyncClient)

        result = await fetch_mitpress_book(
            client, 1, "https://mitpress.mit.edu/9780262533256/raised-to-rage/"
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "Raised To Rage"
        assert result.isbn == "9780262533256"
        assert result.http_status is None
        assert client.get.call_count == 0

    @pytest.mark.asyncio
    async def test_flag_on_layers_the_open_library_synopsis_on_top(self, monkeypatch):
        from pka.config import settings as cfg
        from pka.ingestion import mitpress
        from pka.ingestion.openlibrary import BookSynopsis

        monkeypatch.setattr(cfg, "external_lookup_enabled", True)
        calls: list[str] = []

        def _lookup(isbn):
            calls.append(isbn)
            return BookSynopsis(
                title="Raised to Rage",
                description="A study of anger and inheritance.",
                isbn="9780262533256",
            )

        monkeypatch.setattr(mitpress, "lookup_by_isbn", _lookup, raising=False)
        monkeypatch.setattr("pka.ingestion.openlibrary.lookup_by_isbn", _lookup)

        result = await fetch_mitpress_book(
            AsyncMock(spec=httpx.AsyncClient),
            1,
            "https://mitpress.mit.edu/9780262533256/raised-to-rage/",
        )

        assert calls == ["9780262533256"]
        assert result is not None
        assert result.title == "Raised to Rage"
        assert result.card_summary == "A study of anger and inheritance."
        assert result.isbn == "9780262533256"

    @pytest.mark.asyncio
    async def test_bad_checksum_isbn_is_not_written(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "external_lookup_enabled", False)

        result = await fetch_mitpress_book(
            AsyncMock(spec=httpx.AsyncClient),
            1,
            "https://mitpress.mit.edu/9780262533250/raised-to-rage/",
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.isbn is None


# ── §10 researchgate.net ─────────────────────────────────────────────────────


class TestResearchGate:
    def test_decodes_the_publication_slug(self):
        parsed = parse_researchgate_url(
            "https://www.researchgate.net/publication/"
            "334080242_403_Forbidden_A_Global_View_of_CDN_Geoblocking"
        )
        assert parsed == ("334080242", "403 Forbidden A Global View of CDN Geoblocking")

    def test_casing_is_left_alone_so_acronyms_survive(self):
        _id, title = parse_researchgate_url(
            "https://researchgate.net/publication/1_A_CDN_and_TLS_Study"
        )
        assert title == "A CDN and TLS Study"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.researchgate.net/profile/Ada-Lovelace",
            "https://www.researchgate.net/figure/some-figure_fig1_334080242",
            "https://www.researchgate.net/post/How-do-I",
            "https://www.researchgate.net/",
        ],
    )
    def test_non_publication_paths_fall_through(self, url):
        assert parse_researchgate_url(url) is None
        assert researchgate_result(1, url) is None

    def test_is_researchgate_url(self):
        assert is_researchgate_url("https://www.researchgate.net/")
        assert not is_researchgate_url("https://example.com/")

    def test_builds_a_card_with_no_client_at_all(self):
        result = researchgate_result(
            1,
            "https://www.researchgate.net/publication/"
            "334080242_403_Forbidden_A_Global_View_of_CDN_Geoblocking",
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.http_status is None
        assert result.title == "403 Forbidden A Global View of CDN Geoblocking"
        assert result.card_summary.startswith("ResearchGate publication:")
        assert "no fetch" in (result.error_msg or "")
