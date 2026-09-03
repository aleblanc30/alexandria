"""Tests for the shared DOI metadata spine.

See ``planning/archive/PUBLISHER_FETCH_HANDLERS.md`` §2 and §4. The point of most of
these is the *request count*, not just the result: the ladder's whole cost
argument is that the Semantic Scholar rung fires only when the primary record
carried no abstract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.doi_meta import (
    doi_from_path,
    fetch_doi_metadata,
    parse_doi_record,
    strip_jats,
)

_CROSSREF_WITH_ABSTRACT = {
    "message": {
        "DOI": "10.1038/S41586-020-2649-2",
        "title": ["Array programming with NumPy"],
        "container-title": ["Nature"],
        "type": "journal-article",
        "abstract": "<jats:p>Array programming provides a syntax.</jats:p>",
        "issued": {"date-parts": [[2020, 9, 16]]},
        "author": [
            {"given": "Charles R.", "family": "Harris"},
            {"given": "K. Jarrod", "family": "Millman"},
        ],
    }
}

_CROSSREF_NO_ABSTRACT = {
    "message": {
        "DOI": "10.1007/s11263-015-0816-y",
        "title": ["ImageNet Large Scale Visual Recognition Challenge"],
        "container-title": ["International Journal of Computer Vision"],
        "type": "journal-article",
        "issued": {"date-parts": [[2015, 4, 11]]},
        "author": [{"given": "Olga", "family": "Russakovsky"}],
    }
}

_S2_ABSTRACT = {
    "title": "ImageNet Large Scale Visual Recognition Challenge",
    "abstract": "We describe the creation of this benchmark dataset.",
    "year": 2015,
    "externalIds": {"ArXiv": "1409.0575", "DOI": "10.1007/s11263-015-0816-y"},
}

_CSL_JSON = {
    "DOI": "10.5281/zenodo.1234567",
    "title": "A dataset, not a paper",
    "type": "dataset",
    "container-title": "Zenodo",
    "issued": {"date-parts": [["2021"]]},
    "author": [{"given": "Ada", "family": "Lovelace"}],
}


def _json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


class TestDoiFromPath:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/article/10.1007/s11263-015-0816-y", "10.1007/s11263-015-0816-y"),
            ("/chapter/10.1007/978-3-030-01234-5_7", "10.1007/978-3-030-01234-5_7"),
            ("/content/pdf/10.1007/s11263-015-0816-y.pdf", "10.1007/s11263-015-0816-y"),
            (
                "/prl/abstract/10.1103/PhysRevLett.116.061102",
                "10.1103/PhysRevLett.116.061102",
            ),
            ("/doi/10.1103/PhysRevLett.116.061102", "10.1103/PhysRevLett.116.061102"),
            ("/article/10.1007/s11263-015-0816-y/", "10.1007/s11263-015-0816-y"),
        ],
    )
    def test_shapes(self, path, expected):
        assert doi_from_path(path) == expected

    def test_percent_encoded_form_matches_the_plain_form(self):
        """The assertion most likely to regress if someone reorders ``unquote``."""
        assert doi_from_path("/article/10.1007%2Fs11263-015-0816-y") == doi_from_path(
            "/article/10.1007/s11263-015-0816-y"
        )

    def test_positional_scan_handles_an_uninvented_segment(self):
        """What stops a later tidy-up replacing the scan with a segment list."""
        assert (
            doi_from_path("/livingreferenceentry/10.1007/978-3-030-01234-5_7")
            == "10.1007/978-3-030-01234-5_7"
        )

    def test_prefix_constraint_rejects_a_foreign_doi(self):
        assert doi_from_path("/doi/10.1016/j.artint.2018.07.007", prefix="10.1103") is None
        assert (
            doi_from_path("/doi/10.1103/PhysRevLett.116.061102", prefix="10.1103")
            == "10.1103/PhysRevLett.116.061102"
        )

    def test_prefix_without_suffix_is_not_a_doi(self):
        assert doi_from_path("/journal/10.1007") is None

    def test_index_page_returns_none(self):
        assert doi_from_path("/journal/11263") is None
        assert doi_from_path("/") is None


class TestStripJats:
    def test_strips_the_paragraph_wrapper(self):
        assert strip_jats("<jats:p>Array programming.</jats:p>") == "Array programming."

    def test_strips_nested_markup(self):
        raw = "<jats:p>See <jats:italic>in vivo</jats:italic> results.</jats:p>"
        assert strip_jats(raw) == "See in vivo results."

    def test_drops_a_leading_abstract_heading(self):
        raw = "<jats:title>Abstract</jats:title><jats:p>The real text.</jats:p>"
        assert strip_jats(raw) == "The real text."

    def test_drops_a_bare_leading_abstract_word(self):
        assert strip_jats("<jats:p>Abstract: the real text.</jats:p>") == "the real text."

    def test_empty_returns_none(self):
        assert strip_jats("") is None
        assert strip_jats(None) is None
        assert strip_jats("<jats:p></jats:p>") is None


class TestParseDoiRecord:
    def test_title_array_takes_the_first_entry(self):
        meta = parse_doi_record(_CROSSREF_WITH_ABSTRACT["message"])
        assert meta is not None
        assert meta.title == "Array programming with NumPy"
        assert meta.container == "Nature"

    def test_doi_is_normalized_lowercase(self):
        meta = parse_doi_record(_CROSSREF_WITH_ABSTRACT["message"])
        assert meta is not None
        assert meta.doi == "10.1038/s41586-020-2649-2"

    def test_authors_assembled_given_then_family(self):
        meta = parse_doi_record(_CROSSREF_WITH_ABSTRACT["message"])
        assert meta is not None
        assert meta.authors == ["Charles R. Harris", "K. Jarrod Millman"]

    def test_year_from_date_parts(self):
        meta = parse_doi_record(_CROSSREF_WITH_ABSTRACT["message"])
        assert meta is not None
        assert meta.year == 2020

    def test_csl_json_bare_string_title(self):
        meta = parse_doi_record(_CSL_JSON)
        assert meta is not None
        assert meta.title == "A dataset, not a paper"
        assert meta.type == "dataset"
        assert meta.year == 2021

    def test_missing_title_is_a_failed_lookup(self):
        assert parse_doi_record({"DOI": "10.1/x", "title": []}) is None

    def test_missing_doi_is_a_failed_lookup(self):
        assert parse_doi_record({"title": ["Something"]}) is None

    def test_non_dict_returns_none(self):
        assert parse_doi_record(None) is None
        assert parse_doi_record(["not", "a", "record"]) is None


class TestFetchDoiMetadataLadder:
    @pytest.mark.asyncio
    async def test_abstract_present_makes_exactly_one_request(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(_CROSSREF_WITH_ABSTRACT)

        lookup = await fetch_doi_metadata(client, "10.1038/s41586-020-2649-2")

        assert client.get.call_count == 1
        assert lookup.meta is not None
        assert lookup.meta.abstract == "Array programming provides a syntax."
        assert lookup.abstract_from_s2 is False

    @pytest.mark.asyncio
    async def test_missing_abstract_climbs_to_semantic_scholar(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = [
            _json_response(_CROSSREF_NO_ABSTRACT),
            _json_response(_S2_ABSTRACT),
        ]

        lookup = await fetch_doi_metadata(client, "10.1007/s11263-015-0816-y")

        assert client.get.call_count == 2
        assert lookup.meta is not None
        assert lookup.meta.abstract == "We describe the creation of this benchmark dataset."
        assert lookup.meta.arxiv_id == "1409.0575"
        assert lookup.abstract_from_s2 is True

    @pytest.mark.asyncio
    async def test_semantic_scholar_404_leaves_a_metadata_only_record(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = [
            _json_response(_CROSSREF_NO_ABSTRACT),
            _json_response({"error": "not found"}, status=404),
        ]

        lookup = await fetch_doi_metadata(client, "10.1007/s11263-015-0816-y")

        assert lookup.meta is not None  # not a failure
        assert lookup.error is None
        assert lookup.meta.abstract is None
        assert lookup.meta.title == "ImageNet Large Scale Visual Recognition Challenge"

    @pytest.mark.asyncio
    async def test_flag_off_suppresses_the_whole_crossref_ladder(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "doi_metadata_lookup", False)
        client = AsyncMock(spec=httpx.AsyncClient)

        lookup = await fetch_doi_metadata(client, "10.1007/s11263-015-0816-y")

        assert client.get.call_count == 0
        assert lookup.meta is None
        assert "doi_metadata_lookup" in (lookup.error or "")

    @pytest.mark.asyncio
    async def test_flag_off_still_allows_negotiation_but_not_the_second_rung(self, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "doi_metadata_lookup", False)
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response(_CSL_JSON)

        lookup = await fetch_doi_metadata(client, "10.5281/zenodo.1234567", negotiated=True)

        assert client.get.call_count == 1  # doi.org only; no Semantic Scholar
        assert lookup.meta is not None
        assert lookup.meta.abstract is None
        assert lookup.primary == "doi.org"

    @pytest.mark.asyncio
    async def test_primary_404_is_a_failed_lookup(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _json_response({}, status=404)

        lookup = await fetch_doi_metadata(client, "10.1007/nope")

        assert lookup.meta is None
        assert lookup.http_status == 404
