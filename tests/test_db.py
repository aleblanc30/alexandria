"""
DB schema and query helper tests.
Uses a real SQLite database created in tmp_path (via isolated_settings fixture).
No Ollama or HTTP calls are made.
"""

import pytest
import sqlalchemy as sa

from pka.constants import FetchStatus, Source, TagOrigin
from pka.db.queries import (
    DocumentWrite,
    document_has_chunks,
    document_index,
    filter_document_ids,
    firefox_ingest_queue,
    get_engine,
    init_db,
    insert_chunks,
    insert_document_if_new,
    insert_source_tags,
    list_documents,
    resolve_description,
    source_ids_with_chunks,
    update_card_summary,
    update_document_item_type,
    upsert_document,
)
from pka.db.schema import chunks, documents, overlay_tags, source_tags


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


# ── Schema ────────────────────────────────────────────────────────────────────


class TestSchema:
    def test_all_core_tables_exist(self):
        eng = get_engine()
        inspector = sa.inspect(eng)
        existing = set(inspector.get_table_names())
        expected = {
            "documents",
            "source_tags",
            "source_collections",
            "chunks",
            "fetch_log",
            "overlay_tags",
            "clusters",
            "cluster_runs",
            "cluster_assignments",
            "reading_lists",
            "reading_list_items",
            "tag_training_sessions",
            "tag_training_labels",
        }
        assert expected <= existing

    def test_documents_has_unique_constraint(self):
        """Inserting the same (source, source_id) twice should not duplicate."""
        upsert_document(DocumentWrite("zotero", "KEY001", "Title A", None, None))
        upsert_document(DocumentWrite("zotero", "KEY001", "Title A updated", None, None))
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count())
                .select_from(documents)
                .where(documents.c.source_id == "KEY001")
            ).scalar()
        assert count == 1


# ── upsert_document ───────────────────────────────────────────────────────────


class TestUpsertDocument:
    def test_returns_integer_id(self):
        doc_id = upsert_document(DocumentWrite("zotero", "K1", "Title", None, None))
        assert isinstance(doc_id, int)

    def test_different_sources_same_source_id_are_separate(self):
        id1 = upsert_document(DocumentWrite("zotero", "K1", "T", None, None))
        id2 = upsert_document(DocumentWrite("firefox", "K1", "T", None, None))
        assert id1 != id2

    def test_upsert_updates_title(self):
        upsert_document(DocumentWrite("zotero", "K2", "Old title", None, None))
        upsert_document(DocumentWrite("zotero", "K2", "New title", None, None))
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.title).where(documents.c.source_id == "K2")
            ).fetchone()
        assert row[0] == "New title"

    def test_date_added_stored(self):
        upsert_document(DocumentWrite("firefox", "F1", "T", "https://x.com", 1700000000))
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.date_added).where(documents.c.source_id == "F1")
            ).fetchone()
        assert row[0] == 1700000000


class TestInsertDocumentIfNew:
    def test_inserts_new_row(self):
        doc_id = insert_document_if_new(
            DocumentWrite("firefox", "new1", "T", "https://x.com", None)
        )
        assert isinstance(doc_id, int)

    def test_skips_existing_row(self):
        insert_document_if_new(DocumentWrite("firefox", "dup1", "T", "https://a.com", None))
        assert (
            insert_document_if_new(DocumentWrite("firefox", "dup1", "Other", "https://b.com", None))
            is None
        )
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.title, documents.c.url_or_path).where(
                    documents.c.source_id == "dup1"
                )
            ).fetchone()
        assert row[0] == "T"
        assert row[1] == "https://a.com"


# ── insert_source_tags ────────────────────────────────────────────────────────


class TestInsertSourceTags:
    def test_tags_stored(self):
        doc_id = upsert_document(DocumentWrite("zotero", "K3", "T", None, None))
        insert_source_tags(doc_id, ["consensus", "raft"], source="zotero")
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(source_tags.c.tag_string).where(source_tags.c.document_id == doc_id)
            ).fetchall()
        assert {r[0] for r in rows} == {"consensus", "raft"}

    def test_reinsertion_replaces_not_duplicates(self):
        doc_id = upsert_document(DocumentWrite("zotero", "K4", "T", None, None))
        insert_source_tags(doc_id, ["a", "b"], source="zotero")
        insert_source_tags(doc_id, ["a", "b"], source="zotero")
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count())
                .select_from(source_tags)
                .where(source_tags.c.document_id == doc_id)
            ).scalar()
        assert count == 2

    def test_empty_tags_no_error(self):
        doc_id = upsert_document(DocumentWrite("zotero", "K5", "T", None, None))
        insert_source_tags(doc_id, [], source="zotero")  # should not raise

    def test_tags_from_different_sources_coexist(self):
        doc_id = upsert_document(DocumentWrite("firefox", "F2", "T", None, None))
        insert_source_tags(doc_id, ["tag-a"], source="firefox")
        insert_source_tags(doc_id, ["tag-b"], source="zotero")
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count())
                .select_from(source_tags)
                .where(source_tags.c.document_id == doc_id)
            ).scalar()
        assert count == 2


# ── insert_chunks / document_has_chunks ──────────────────────────────────────


class TestChunks:
    def test_chunks_stored(self):
        doc_id = upsert_document(DocumentWrite("zotero", "K6", "T", None, None))
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "chunk zero",
                    "token_count": 2,
                    "vector_id": "v0",
                },
                {
                    "document_id": doc_id,
                    "chunk_index": 1,
                    "text": "chunk one",
                    "token_count": 2,
                    "vector_id": "v1",
                },
            ]
        )
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(chunks).where(chunks.c.document_id == doc_id)
            ).scalar()
        assert count == 2

    def test_document_has_chunks_false_when_none(self):
        doc_id = upsert_document(DocumentWrite("zotero", "K7", "T", None, None))
        assert document_has_chunks(doc_id) is False

    def test_document_has_chunks_true_after_insert(self):
        doc_id = upsert_document(DocumentWrite("zotero", "K8", "T", None, None))
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "some text",
                    "token_count": 2,
                    "vector_id": "vx",
                },
            ]
        )
        assert document_has_chunks(doc_id) is True

    def test_source_ids_with_chunks(self):
        d1 = upsert_document(DocumentWrite("zotero", "K9", "One", None, None))
        upsert_document(DocumentWrite("zotero", "K10", "Two", None, None))
        insert_chunks(
            [
                {
                    "document_id": d1,
                    "chunk_index": 0,
                    "text": "chunk",
                    "token_count": 1,
                    "vector_id": "v9",
                },
            ]
        )
        assert source_ids_with_chunks("zotero") == {"K9"}

    def test_document_index(self):
        upsert_document(DocumentWrite("firefox", "bm1", "A", "http://a", None))
        upsert_document(DocumentWrite("firefox", "bm2", "B", "http://b", None))
        idx = document_index("firefox")
        assert idx["bm1"] != idx["bm2"]
        assert set(idx) == {"bm1", "bm2"}

    def test_insert_empty_chunks_no_error(self):
        insert_chunks([])  # should not raise


class TestFirefoxIngestQueue:
    def test_returns_pending_urls(self):
        d1 = upsert_document(
            DocumentWrite(
                "firefox",
                "p1",
                "T",
                "https://pending.example",
                None,
                fetch_status=FetchStatus.PENDING,
            )
        )
        queue = firefox_ingest_queue()
        assert (d1, "https://pending.example") in queue

    def test_includes_fetched_orphans_without_chunks(self):
        orphan = upsert_document(
            DocumentWrite(
                "firefox",
                "o1",
                "T",
                "https://orphan.example",
                None,
                fetch_status=FetchStatus.FETCHED,
            )
        )
        embedded = upsert_document(
            DocumentWrite(
                "firefox",
                "e1",
                "T",
                "https://embedded.example",
                None,
                fetch_status=FetchStatus.FETCHED,
            )
        )
        insert_chunks(
            [
                {
                    "document_id": embedded,
                    "chunk_index": 0,
                    "text": "already embedded text chunk content here",
                    "token_count": 5,
                    "vector_id": "v-orphan-test",
                },
            ]
        )
        queue = firefox_ingest_queue()
        urls = dict(queue)
        assert urls[orphan] == "https://orphan.example"
        assert embedded not in urls

    def test_no_duplicate_document_ids(self):
        upsert_document(
            DocumentWrite(
                "firefox",
                "p1",
                "T",
                "https://a.example",
                None,
                fetch_status=FetchStatus.PENDING,
            )
        )
        orphan = upsert_document(
            DocumentWrite(
                "firefox",
                "o1",
                "T",
                "https://b.example",
                None,
                fetch_status=FetchStatus.FETCHED,
            )
        )
        queue = firefox_ingest_queue()
        ids = [doc_id for doc_id, _ in queue]
        assert len(ids) == len(set(ids))
        assert orphan in ids

    def test_respects_limit(self):
        for i in range(5):
            upsert_document(
                DocumentWrite(
                    "firefox",
                    f"lim{i}",
                    "T",
                    f"https://lim{i}.example",
                    None,
                    fetch_status=FetchStatus.PENDING,
                )
            )
        assert len(firefox_ingest_queue(limit=2)) == 2


class TestResolveDescription:
    def test_prefers_card_summary(self):
        assert resolve_description("Card text here", "chunk fallback") == "Card text here"

    def test_falls_back_to_chunk_snippet(self):
        long_chunk = "word " * 200
        desc = resolve_description(None, long_chunk)
        assert len(desc) <= 300
        assert "word" in desc


class TestFilterDocumentIds:
    def test_no_filters_returns_all(self):
        doc_id = upsert_document(DocumentWrite("zotero", "F1", "T", None, None))
        with get_engine().connect() as con:
            result = filter_document_ids(con, [doc_id])
        assert result == {doc_id}

    def test_source_filter(self):
        zid = upsert_document(DocumentWrite("zotero", "FZ", "Z", None, None))
        fid = upsert_document(DocumentWrite("firefox", "FF", "F", None, None))
        with get_engine().connect() as con:
            result = filter_document_ids(con, [zid, fid], source_filter=["zotero"])
        assert result == {zid}


class TestListDocuments:
    def test_filters_by_source(self):
        upsert_document(DocumentWrite("zotero", "L1", "Zotero doc", None, None))
        upsert_document(DocumentWrite("firefox", "L2", "Firefox doc", None, None))
        _total, rows = list_documents(sources=["zotero"])
        assert all(r["source"] == "zotero" for r in rows)

    def test_overlay_tag_filter(self):
        doc_id = upsert_document(DocumentWrite("zotero", "OT1", "Tagged", None, None))
        with get_engine().begin() as con:
            con.execute(
                overlay_tags.insert(),
                [
                    {
                        "document_id": doc_id,
                        "tag": "ml-topic",
                        "origin": TagOrigin.INFERRED,
                        "confidence": 0.9,
                    }
                ],
            )
        total, rows = list_documents(overlay_tags=["ml-topic"])
        assert total == 1
        assert rows[0]["id"] == doc_id


class TestUpdateDocumentItemType:
    def test_updates_existing_row(self):
        upsert_document(DocumentWrite("zotero", "IT1", "Paper", None, None))
        n = update_document_item_type("zotero", "IT1", "journalArticle")
        assert n == 1
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.item_type).where(documents.c.source_id == "IT1")
            ).fetchone()
        assert row[0] == "journalArticle"


class TestUpdateCardSummary:
    def test_stores_summary(self):
        doc_id = upsert_document(DocumentWrite("zotero", "CS1", "Card", None, None))
        update_card_summary(doc_id, "Short excerpt")
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.card_summary).where(documents.c.id == doc_id)
            ).fetchone()
        assert row[0] == "Short excerpt"


# ── DocumentWrite / _write_document ─────────────────────────────────────────
#
# These pin the write-path collapse (planning/DOCUMENT_WRITE_PATH_PLAN.md):
# every writable column round-trips, the overwrite/COALESCE split matches the
# pre-refactor ON CONFLICT clause exactly, and DocumentWrite's field set stays
# in sync with the documents table.

# Columns `documents` carries that ingestion never writes through
# DocumentWrite — each has its own dedicated writer (update_card_summary,
# set_generated_summary, the Wayback/doc-embedding paths) — plus id/ingested_at,
# which the writer itself controls.
_NON_WRITE_COLUMNS = {
    "id",
    "archive_url",
    "card_summary",
    "doc_embedding",
    "generated_summary",
    # Provenance for generated_summary; set_generated_summary writes both
    # together so the stamp can never outlive the summary it describes.
    "summary_run_id",
    "ingested_at",
}

_FULL_ROW = dict(
    source=Source.ZOTERO,
    source_id="RT1",
    title="Round Trip Title",
    url_or_path="https://example.com/rt",
    date_added=1_700_000_001,
    fetch_status=FetchStatus.AVAILABLE,
    zotero_attachment_key="ATTACH1",
    item_type="journalArticle",
    note="a note",
    doi="10.1/rt",
    arxiv_id="2101.00001",
    isbn="9780130224187",
    year=2021,
    authors_json='["A. Author"]',
    zotero_url="https://zotero.org/rt",
    zotero_path="/tmp/rt.pdf",
)


def _document_row(doc_id: int) -> dict:
    with get_engine().connect() as con:
        row = con.execute(sa.select(documents).where(documents.c.id == doc_id)).mappings().one()
    return dict(row)


class TestDocumentWriteFieldSet:
    def test_covers_exactly_the_writable_columns(self):
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(DocumentWrite)}
        table_names = {c.name for c in documents.c}
        assert field_names == table_names - _NON_WRITE_COLUMNS

    def test_bad_keyword_raises(self):
        with pytest.raises(TypeError):
            DocumentWrite("zotero", "BAD1", arxvi_id="typo")


class TestDocumentWriteRoundTrip:
    def test_upsert_document_round_trips_every_column(self):
        doc_id = upsert_document(DocumentWrite(**_FULL_ROW))
        row = _document_row(doc_id)
        for field, value in _FULL_ROW.items():
            expected = str(value) if field in ("source", "fetch_status") else value
            assert row[field] == expected

    def test_insert_document_if_new_round_trips_every_column(self):
        doc_id = insert_document_if_new(DocumentWrite(**{**_FULL_ROW, "source_id": "RT2"}))
        row = _document_row(doc_id)
        for field, value in _FULL_ROW.items():
            if field == "source_id":
                continue
            expected = str(value) if field in ("source", "fetch_status") else value
            assert row[field] == expected


class TestUpsertOverwriteVsCoalesce:
    _OVERWRITE_FIELDS = ("title", "url_or_path", "fetch_status")

    def test_none_in_optional_columns_does_not_erase_stored_values(self):
        doc_id = upsert_document(DocumentWrite(**{**_FULL_ROW, "source_id": "OC1"}))
        upsert_document(
            DocumentWrite(
                source=Source.ZOTERO,
                source_id="OC1",
                title="New Title",
                url_or_path="https://example.com/new",
                date_added=None,
                fetch_status=FetchStatus.FETCHED,
            )
        )
        row = _document_row(doc_id)
        assert row["title"] == "New Title"
        assert row["url_or_path"] == "https://example.com/new"
        assert row["fetch_status"] == str(FetchStatus.FETCHED)
        # date_added COALESCEs like the metadata columns, not overwrites like
        # title/url_or_path/fetch_status — the incoming None must not erase it.
        for field in _FULL_ROW:
            if field in ("source", "source_id", *self._OVERWRITE_FIELDS):
                continue
            assert row[field] == _FULL_ROW[field]

    def test_new_values_land_on_a_second_write(self):
        doc_id = upsert_document(DocumentWrite(**{**_FULL_ROW, "source_id": "OC2"}))
        upsert_document(
            DocumentWrite(
                source=Source.ZOTERO,
                source_id="OC2",
                doi="10.1/updated",
                isbn="9780262033848",
                year=2099,
            )
        )
        row = _document_row(doc_id)
        assert row["doi"] == "10.1/updated"
        assert row["isbn"] == "9780262033848"
        assert row["year"] == 2099


class TestIngestedAt:
    def test_survives_across_upserts(self):
        doc_id = upsert_document(DocumentWrite("zotero", "IA1", "T", None, None))
        first = _document_row(doc_id)["ingested_at"]
        upsert_document(DocumentWrite("zotero", "IA1", "T2", None, None))
        second = _document_row(doc_id)["ingested_at"]
        assert first == second


class TestInsertDocumentIfNewLeavesRowUntouched:
    def test_duplicate_call_does_not_modify_any_column(self):
        doc_id = insert_document_if_new(DocumentWrite(**{**_FULL_ROW, "source_id": "DUP1"}))
        before = _document_row(doc_id)
        result = insert_document_if_new(
            DocumentWrite(
                source=Source.ZOTERO,
                source_id="DUP1",
                title="Different Title",
                url_or_path="https://example.com/different",
                date_added=1,
                fetch_status=FetchStatus.MISSING,
                doi="10.1/different",
            )
        )
        assert result is None
        assert _document_row(doc_id) == before


class TestEnumAndStringFormsAgree:
    def test_source_enum_and_string_write_the_same_value(self):
        enum_id = upsert_document(DocumentWrite(Source.ZOTERO, "ES1", "T", None, None))
        str_id = upsert_document(DocumentWrite("zotero", "ES2", "T", None, None))
        assert _document_row(enum_id)["source"] == _document_row(str_id)["source"] == "zotero"

    def test_fetch_status_enum_and_string_write_the_same_value(self):
        enum_id = upsert_document(
            DocumentWrite("zotero", "ES3", "T", None, None, fetch_status=FetchStatus.PENDING)
        )
        str_id = upsert_document(
            DocumentWrite("zotero", "ES4", "T", None, None, fetch_status="pending")
        )
        assert (
            _document_row(enum_id)["fetch_status"]
            == _document_row(str_id)["fetch_status"]
            == "pending"
        )
