"""Zotero attachment key persistence and refresh."""

import sqlalchemy as sa

from pka.connectors.zotero import (
    ZoteroItem,
    zotero_document_url_or_path,
    zotero_path,
    zotero_url,
)
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    DocumentWrite,
    get_engine,
    init_db,
    refresh_zotero_metadata,
    upsert_document,
)
from pka.db.schema import documents
from pka.ingestion.arxiv import parse_arxiv_url
from pka.ingestion.identifiers import derive_arxiv_doi, normalize_doi, resolve_doi
from pka.ingestion.runners.zotero import ingest_zotero_items
from tests.conftest import make_document


def _item(**overrides) -> ZoteroItem:
    defaults = dict(
        source_id="RAFT0001",
        title="Raft",
        authors=[],
        abstract="Abstract",
        year=2023,
        doi="10.1/raft",
        url=None,
        item_type="journalArticle",
        collections=[],
        tags=[],
        pdf_path=None,
        pdf_attachment_key="RAFT0002",
        date_added=1700000000,
    )
    return ZoteroItem(**{**defaults, **overrides})


def test_upsert_stores_zotero_attachment_key():
    init_db()
    item = _item()
    upsert_document(
        DocumentWrite(
            Source.ZOTERO,
            "RAFT0001",
            "Raft",
            zotero_document_url_or_path(zotero_url(item), zotero_path(item)),
            1700000000,
            FetchStatus.AVAILABLE,
            zotero_attachment_key="RAFT0002",
        )
    )
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(documents.c.zotero_attachment_key).where(documents.c.source_id == "RAFT0001")
        ).fetchone()
    assert row[0] == "RAFT0002"


def test_refresh_zotero_metadata_updates_existing_row():
    init_db()
    make_document(Source.ZOTERO, "RAFT0001", "Raft", None, 1700000000)
    n = refresh_zotero_metadata(
        {
            "RAFT0001": {
                "zotero_attachment_key": "RAFT0002",
                "doi": "10.1/raft",
                "arxiv_id": None,
                "year": 2023,
                "authors_json": None,
                "zotero_url": None,
                "zotero_path": None,
                "url_or_path": None,
            }
        }
    )
    assert n == 1
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(documents.c.zotero_attachment_key, documents.c.doi, documents.c.year).where(
                documents.c.source_id == "RAFT0001"
            )
        ).fetchone()
    assert row == ("RAFT0002", "10.1/raft", 2023)


def test_refresh_zotero_metadata_does_not_null_out_stored_doi():
    """A missing value in the incoming dict must not blank an already-stored one."""
    init_db()
    upsert_document(
        DocumentWrite(Source.ZOTERO, "RAFT0001", "Raft", None, 1700000000, doi="10.1/original")
    )
    refresh_zotero_metadata(
        {
            "RAFT0001": {
                "zotero_attachment_key": None,
                "doi": None,
                "arxiv_id": None,
                "year": None,
                "authors_json": None,
                "zotero_url": None,
                "zotero_path": None,
                "url_or_path": None,
            }
        }
    )
    with get_engine().connect() as con:
        doi = con.execute(
            sa.select(documents.c.doi).where(documents.c.source_id == "RAFT0001")
        ).scalar()
    assert doi == "10.1/original"


def test_refresh_zotero_metadata_migrates_bare_doi_url_or_path():
    """Regression: an archive written by the old DOI-in-url_or_path ladder must
    converge with a freshly inserted DOI-only item after the backfill runs."""
    init_db()
    make_document(
        Source.ZOTERO,
        "OLD0001",
        "Old item",
        "10.1145/xyz",
        1700000000,
    )
    n = refresh_zotero_metadata(
        {
            "OLD0001": {
                "zotero_attachment_key": None,
                "doi": "10.1145/xyz",
                "arxiv_id": None,
                "year": None,
                "authors_json": None,
                "zotero_url": None,
                "zotero_path": None,
                "url_or_path": None,
            }
        }
    )
    assert n == 1
    upsert_document(
        DocumentWrite(Source.ZOTERO, "NEW0001", "New item", None, 1700000000, doi="10.1145/xyz")
    )
    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.source_id, documents.c.url_or_path, documents.c.doi).where(
                documents.c.source_id.in_(["OLD0001", "NEW0001"])
            )
        ).fetchall()
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    assert by_id["OLD0001"] == (None, "10.1145/xyz")
    assert by_id["NEW0001"] == (None, "10.1145/xyz")


def test_ingest_zotero_items_persists_attachment_key(mock_chroma):
    init_db()
    ingest_zotero_items([_item()])
    with get_engine().connect() as con:
        key = con.execute(
            sa.select(documents.c.zotero_attachment_key).where(documents.c.source == "zotero")
        ).scalar()
    assert key == "RAFT0002"


class TestDoiDerivation:
    def test_derive_arxiv_doi(self):
        assert derive_arxiv_doi("2301.12345") == "10.48550/arxiv.2301.12345"

    def test_normalize_doi_strips_prefix_and_lowercases(self):
        assert normalize_doi("https://doi.org/10.1145/XYZ") == "10.1145/xyz"
        assert normalize_doi("doi:10.1145/XYZ") == "10.1145/xyz"
        assert normalize_doi(None) is None

    def test_resolve_doi_prefers_source_doi(self):
        assert resolve_doi("10.1/journal", "2301.12345") == "10.1/journal"

    def test_resolve_doi_derives_when_no_source_doi(self):
        assert resolve_doi(None, "2301.12345") == "10.48550/arxiv.2301.12345"

    def test_resolve_doi_none_when_neither_present(self):
        assert resolve_doi(None, None) is None


def test_zotero_item_arxiv_id_and_doi_join_with_fetched_arxiv_document(mock_chroma):
    """A Zotero item whose ``url`` is an arXiv abs page and a Firefox document
    fetched from that same URL must agree on both arxiv_id and the derived DOI —
    the join the identifier columns exist for."""
    init_db()
    arxiv_url = "https://arxiv.org/abs/2301.12345"
    item = _item(source_id="ZOT0001", doi=None, url=arxiv_url)
    ingest_zotero_items([item])

    arxiv_id = parse_arxiv_url(arxiv_url)
    upsert_document(
        DocumentWrite(
            Source.FIREFOX,
            "FF0001",
            "Some Paper",
            arxiv_url,
            1700000000,
            doi=resolve_doi(None, arxiv_id),
            arxiv_id=arxiv_id,
        )
    )

    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(documents.c.source, documents.c.arxiv_id, documents.c.doi).where(
                documents.c.source_id.in_(["ZOT0001", "FF0001"])
            )
        ).fetchall()
    by_source = {r[0]: (r[1], r[2]) for r in rows}
    assert by_source["zotero"] == by_source["firefox"]
    assert by_source["zotero"] == ("2301.12345", "10.48550/arxiv.2301.12345")


def test_zotero_item_with_non_arxiv_url_leaves_arxiv_id_none(mock_chroma):
    init_db()
    item = _item(source_id="ZOT0002", url="https://example.com/paper")
    ingest_zotero_items([item])
    with get_engine().connect() as con:
        arxiv_id = con.execute(
            sa.select(documents.c.arxiv_id).where(documents.c.source_id == "ZOT0002")
        ).scalar()
    assert arxiv_id is None
