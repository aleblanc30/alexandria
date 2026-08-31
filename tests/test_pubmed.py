"""Tests for PubMed URL parsing, XML parsing, and fetch handler.

See ``planning/FIREFOX_INGESTERS_PLAN.md`` §4 for the handler design.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.pubmed import (
    fetch_pubmed_article,
    is_pubmed_url,
    parse_pubmed_url,
    parse_pubmed_xml,
)

_PUBMED_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">12345678</PMID>
      <Article>
        <ArticleTitle>Dopamine signaling in reward learning</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Dopamine neurons encode reward prediction errors.</AbstractText>
          <AbstractText Label="METHODS">We recorded from ventral tegmental area neurons in mice.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Smith</LastName><Initials>A</Initials></Author>
          <Author><LastName>Jones</LastName><Initials>B</Initials></Author>
        </AuthorList>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2023</Year></PubDate>
          </JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1000/xyz123</ArticleId>
        <ArticleId IdType="pubmed">12345678</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

_PUBMED_XML_NO_DOI = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">99999999</PMID>
      <Article>
        <ArticleTitle>An older entry with no DOI on record</ArticleTitle>
        <Abstract>
          <AbstractText>A single unlabeled abstract paragraph.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Lee</LastName><Initials>C</Initials></Author>
        </AuthorList>
        <Journal>
          <JournalIssue>
            <PubDate><Year>1998</Year></PubDate>
          </JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">99999999</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class TestParsePubmedUrl:
    def test_standard_url(self):
        assert parse_pubmed_url("https://pubmed.ncbi.nlm.nih.gov/12345678/") == "12345678"

    def test_standard_url_no_trailing_slash(self):
        assert parse_pubmed_url("https://pubmed.ncbi.nlm.nih.gov/12345678") == "12345678"

    def test_legacy_ncbi_url(self):
        assert parse_pubmed_url("https://www.ncbi.nlm.nih.gov/pubmed/12345678/") == "12345678"

    def test_legacy_ncbi_url_no_www(self):
        assert parse_pubmed_url("https://ncbi.nlm.nih.gov/pubmed/12345678") == "12345678"

    def test_non_pubmed_returns_none(self):
        assert parse_pubmed_url("https://example.com/12345678/") is None

    def test_pubmed_non_article_path_returns_none(self):
        assert parse_pubmed_url("https://pubmed.ncbi.nlm.nih.gov/?term=dopamine") is None

    def test_is_pubmed_url(self):
        assert is_pubmed_url("https://pubmed.ncbi.nlm.nih.gov/12345678/")
        assert is_pubmed_url("https://www.ncbi.nlm.nih.gov/pubmed/12345678/")
        assert not is_pubmed_url("https://example.com/")


class TestParsePubmedXml:
    def test_parses_article(self):
        meta = parse_pubmed_xml(_PUBMED_XML)
        assert meta is not None
        assert meta.pmid == "12345678"
        assert meta.title == "Dopamine signaling in reward learning"
        assert meta.year == 2023
        assert meta.doi == "10.1000/xyz123"
        assert meta.authors == ["Smith A", "Jones B"]

    def test_structured_abstract_sections_joined_with_labels(self):
        meta = parse_pubmed_xml(_PUBMED_XML)
        assert meta is not None
        assert "Background: Dopamine neurons encode reward prediction errors." in meta.abstract
        assert "Methods: We recorded from ventral tegmental area neurons in mice." in meta.abstract

    def test_unlabeled_single_abstract_used_as_is(self):
        meta = parse_pubmed_xml(_PUBMED_XML_NO_DOI)
        assert meta is not None
        assert meta.abstract == "A single unlabeled abstract paragraph."

    def test_missing_doi_returns_metadata_without_doi(self):
        meta = parse_pubmed_xml(_PUBMED_XML_NO_DOI)
        assert meta is not None
        assert meta.doi is None
        assert meta.title == "An older entry with no DOI on record"

    def test_malformed_xml_returns_none(self):
        assert parse_pubmed_xml("<not><valid xml") is None

    def test_no_article_returns_none(self):
        assert parse_pubmed_xml("<PubmedArticleSet></PubmedArticleSet>") is None


class TestFetchPubmedArticle:
    @pytest.mark.asyncio
    async def test_fetches_metadata(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            200,
            text=_PUBMED_XML,
            request=httpx.Request("GET", "http://x"),
        )

        result = await fetch_pubmed_article(
            mock_client,
            doc_id=1,
            url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "Dopamine signaling in reward learning"
        assert result.doi == "10.1000/xyz123"
        assert result.year == 2023
        assert json.loads(result.authors_json) == ["Smith A", "Jones B"]
        assert "reward prediction errors" in (result.text or "")

    @pytest.mark.asyncio
    async def test_non_pubmed_returns_none(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        assert await fetch_pubmed_article(mock_client, 1, "https://example.com") is None
