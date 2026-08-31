"""
DB schema and query helper tests.
Uses a real SQLite database created in tmp_path (via isolated_settings fixture).
No Ollama or HTTP calls are made.
"""

import pytest
import sqlalchemy as sa

from pka.constants import FetchStatus, TagOrigin
from pka.db.queries import (
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
        upsert_document("zotero", "KEY001", "Title A", None, None)
        upsert_document("zotero", "KEY001", "Title A updated", None, None)
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
        doc_id = upsert_document("zotero", "K1", "Title", None, None)
        assert isinstance(doc_id, int)

    def test_different_sources_same_source_id_are_separate(self):
        id1 = upsert_document("zotero", "K1", "T", None, None)
        id2 = upsert_document("firefox", "K1", "T", None, None)
        assert id1 != id2

    def test_upsert_updates_title(self):
        upsert_document("zotero", "K2", "Old title", None, None)
        upsert_document("zotero", "K2", "New title", None, None)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.title).where(documents.c.source_id == "K2")
            ).fetchone()
        assert row[0] == "New title"

    def test_date_added_stored(self):
        upsert_document("firefox", "F1", "T", "https://x.com", 1700000000)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.date_added).where(documents.c.source_id == "F1")
            ).fetchone()
        assert row[0] == 1700000000


class TestInsertDocumentIfNew:
    def test_inserts_new_row(self):
        doc_id = insert_document_if_new("firefox", "new1", "T", "https://x.com", None)
        assert isinstance(doc_id, int)

    def test_skips_existing_row(self):
        insert_document_if_new("firefox", "dup1", "T", "https://a.com", None)
        assert insert_document_if_new("firefox", "dup1", "Other", "https://b.com", None) is None
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
        doc_id = upsert_document("zotero", "K3", "T", None, None)
        insert_source_tags(doc_id, ["consensus", "raft"], source="zotero")
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(source_tags.c.tag_string).where(source_tags.c.document_id == doc_id)
            ).fetchall()
        assert {r[0] for r in rows} == {"consensus", "raft"}

    def test_reinsertion_replaces_not_duplicates(self):
        doc_id = upsert_document("zotero", "K4", "T", None, None)
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
        doc_id = upsert_document("zotero", "K5", "T", None, None)
        insert_source_tags(doc_id, [], source="zotero")  # should not raise

    def test_tags_from_different_sources_coexist(self):
        doc_id = upsert_document("firefox", "F2", "T", None, None)
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
        doc_id = upsert_document("zotero", "K6", "T", None, None)
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
        doc_id = upsert_document("zotero", "K7", "T", None, None)
        assert document_has_chunks(doc_id) is False

    def test_document_has_chunks_true_after_insert(self):
        doc_id = upsert_document("zotero", "K8", "T", None, None)
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
        d1 = upsert_document("zotero", "K9", "One", None, None)
        upsert_document("zotero", "K10", "Two", None, None)
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
        upsert_document("firefox", "bm1", "A", "http://a", None)
        upsert_document("firefox", "bm2", "B", "http://b", None)
        idx = document_index("firefox")
        assert idx["bm1"] != idx["bm2"]
        assert set(idx) == {"bm1", "bm2"}

    def test_insert_empty_chunks_no_error(self):
        insert_chunks([])  # should not raise


class TestFirefoxIngestQueue:
    def test_returns_pending_urls(self):
        d1 = upsert_document(
            "firefox",
            "p1",
            "T",
            "https://pending.example",
            None,
            fetch_status=FetchStatus.PENDING,
        )
        queue = firefox_ingest_queue()
        assert (d1, "https://pending.example") in queue

    def test_includes_fetched_orphans_without_chunks(self):
        orphan = upsert_document(
            "firefox",
            "o1",
            "T",
            "https://orphan.example",
            None,
            fetch_status=FetchStatus.FETCHED,
        )
        embedded = upsert_document(
            "firefox",
            "e1",
            "T",
            "https://embedded.example",
            None,
            fetch_status=FetchStatus.FETCHED,
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
            "firefox",
            "p1",
            "T",
            "https://a.example",
            None,
            fetch_status=FetchStatus.PENDING,
        )
        orphan = upsert_document(
            "firefox",
            "o1",
            "T",
            "https://b.example",
            None,
            fetch_status=FetchStatus.FETCHED,
        )
        queue = firefox_ingest_queue()
        ids = [doc_id for doc_id, _ in queue]
        assert len(ids) == len(set(ids))
        assert orphan in ids

    def test_respects_limit(self):
        for i in range(5):
            upsert_document(
                "firefox",
                f"lim{i}",
                "T",
                f"https://lim{i}.example",
                None,
                fetch_status=FetchStatus.PENDING,
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
        doc_id = upsert_document("zotero", "F1", "T", None, None)
        with get_engine().connect() as con:
            result = filter_document_ids(con, [doc_id])
        assert result == {doc_id}

    def test_source_filter(self):
        zid = upsert_document("zotero", "FZ", "Z", None, None)
        fid = upsert_document("firefox", "FF", "F", None, None)
        with get_engine().connect() as con:
            result = filter_document_ids(con, [zid, fid], source_filter=["zotero"])
        assert result == {zid}


class TestListDocuments:
    def test_filters_by_source(self):
        upsert_document("zotero", "L1", "Zotero doc", None, None)
        upsert_document("firefox", "L2", "Firefox doc", None, None)
        _total, rows = list_documents(sources=["zotero"])
        assert all(r["source"] == "zotero" for r in rows)

    def test_overlay_tag_filter(self):
        doc_id = upsert_document("zotero", "OT1", "Tagged", None, None)
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
        upsert_document("zotero", "IT1", "Paper", None, None)
        n = update_document_item_type("zotero", "IT1", "journalArticle")
        assert n == 1
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.item_type).where(documents.c.source_id == "IT1")
            ).fetchone()
        assert row[0] == "journalArticle"


class TestUpdateCardSummary:
    def test_stores_summary(self):
        doc_id = upsert_document("zotero", "CS1", "Card", None, None)
        update_card_summary(doc_id, "Short excerpt")
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.card_summary).where(documents.c.id == doc_id)
            ).fetchone()
        assert row[0] == "Short excerpt"
