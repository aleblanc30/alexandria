"""Zotero attachment key persistence and refresh."""
import sqlalchemy as sa

from pka.connectors.zotero import ZoteroItem, zotero_document_url_or_path
from pka.constants import FetchStatus, Source
from pka.db.queries import get_engine, init_db, refresh_zotero_attachment_keys, upsert_document
from pka.db.schema import documents
from pka.ingestion.runners.zotero import ingest_zotero_items


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
    upsert_document(
        Source.ZOTERO,
        "RAFT0001",
        "Raft",
        zotero_document_url_or_path(_item()),
        1700000000,
        FetchStatus.AVAILABLE,
        zotero_attachment_key="RAFT0002",
    )
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(documents.c.zotero_attachment_key).where(
                documents.c.source_id == "RAFT0001"
            )
        ).fetchone()
    assert row[0] == "RAFT0002"


def test_refresh_zotero_attachment_keys_updates_existing_row():
    init_db()
    upsert_document(Source.ZOTERO, "RAFT0001", "Raft", None, 1700000000)
    n = refresh_zotero_attachment_keys({"RAFT0001": "RAFT0002"})
    assert n == 1
    with get_engine().connect() as con:
        key = con.execute(
            sa.select(documents.c.zotero_attachment_key).where(
                documents.c.source_id == "RAFT0001"
            )
        ).scalar()
    assert key == "RAFT0002"


def test_ingest_zotero_items_persists_attachment_key(mock_chroma):
    init_db()
    ingest_zotero_items([_item()])
    with get_engine().connect() as con:
        key = con.execute(
            sa.select(documents.c.zotero_attachment_key).where(
                documents.c.source == "zotero"
            )
        ).scalar()
    assert key == "RAFT0002"
