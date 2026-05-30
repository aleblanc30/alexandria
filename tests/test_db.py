"""
DB schema and query helper tests.
Uses a real SQLite database created in tmp_path (via isolated_settings fixture).
No Ollama or HTTP calls are made.
"""
import pytest
import sqlalchemy as sa

from pka.db.queries import (
    init_db, get_engine,
    upsert_document, insert_source_tags, insert_source_collections,
    insert_chunks, document_has_chunks, document_index, source_ids_with_chunks,
)
from pka.db.schema import documents, source_tags, source_collections, chunks


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
            "documents", "source_tags", "source_collections",
            "chunks", "fetch_log",
            "overlay_tags", "clusters", "cluster_runs",
            "cluster_assignments", "reading_lists", "reading_list_items",
        }
        assert expected <= existing

    def test_documents_has_unique_constraint(self):
        """Inserting the same (source, source_id) twice should not duplicate."""
        upsert_document("zotero", "KEY001", "Title A", None, None)
        upsert_document("zotero", "KEY001", "Title A updated", None, None)
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(documents).where(
                    documents.c.source_id == "KEY001"
                )
            ).scalar()
        assert count == 1


# ── upsert_document ───────────────────────────────────────────────────────────

class TestUpsertDocument:
    def test_returns_integer_id(self):
        doc_id = upsert_document("zotero", "K1", "Title", None, None)
        assert isinstance(doc_id, int)

    def test_different_sources_same_source_id_are_separate(self):
        id1 = upsert_document("zotero",   "K1", "T", None, None)
        id2 = upsert_document("firefox",  "K1", "T", None, None)
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


# ── insert_source_tags ────────────────────────────────────────────────────────

class TestInsertSourceTags:
    def test_tags_stored(self):
        doc_id = upsert_document("zotero", "K3", "T", None, None)
        insert_source_tags(doc_id, ["consensus", "raft"], source="zotero")
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(source_tags.c.tag_string).where(
                    source_tags.c.document_id == doc_id
                )
            ).fetchall()
        assert {r[0] for r in rows} == {"consensus", "raft"}

    def test_reinsertion_replaces_not_duplicates(self):
        doc_id = upsert_document("zotero", "K4", "T", None, None)
        insert_source_tags(doc_id, ["a", "b"], source="zotero")
        insert_source_tags(doc_id, ["a", "b"], source="zotero")
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(source_tags).where(
                    source_tags.c.document_id == doc_id
                )
            ).scalar()
        assert count == 2

    def test_empty_tags_no_error(self):
        doc_id = upsert_document("zotero", "K5", "T", None, None)
        insert_source_tags(doc_id, [], source="zotero")   # should not raise

    def test_tags_from_different_sources_coexist(self):
        doc_id = upsert_document("firefox", "F2", "T", None, None)
        insert_source_tags(doc_id, ["tag-a"], source="firefox")
        insert_source_tags(doc_id, ["tag-b"], source="zotero")
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(source_tags).where(
                    source_tags.c.document_id == doc_id
                )
            ).scalar()
        assert count == 2


# ── insert_chunks / document_has_chunks ──────────────────────────────────────

class TestChunks:
    def test_chunks_stored(self):
        doc_id = upsert_document("zotero", "K6", "T", None, None)
        insert_chunks([
            {"document_id": doc_id, "chunk_index": 0,
             "text": "chunk zero", "token_count": 2, "vector_id": "v0"},
            {"document_id": doc_id, "chunk_index": 1,
             "text": "chunk one",  "token_count": 2, "vector_id": "v1"},
        ])
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(chunks).where(
                    chunks.c.document_id == doc_id
                )
            ).scalar()
        assert count == 2

    def test_document_has_chunks_false_when_none(self):
        doc_id = upsert_document("zotero", "K7", "T", None, None)
        assert document_has_chunks(doc_id) is False

    def test_document_has_chunks_true_after_insert(self):
        doc_id = upsert_document("zotero", "K8", "T", None, None)
        insert_chunks([
            {"document_id": doc_id, "chunk_index": 0,
             "text": "some text", "token_count": 2, "vector_id": "vx"},
        ])
        assert document_has_chunks(doc_id) is True

    def test_source_ids_with_chunks(self):
        d1 = upsert_document("zotero", "K9", "One", None, None)
        upsert_document("zotero", "K10", "Two", None, None)
        insert_chunks([
            {"document_id": d1, "chunk_index": 0,
             "text": "chunk", "token_count": 1, "vector_id": "v9"},
        ])
        assert source_ids_with_chunks("zotero") == {"K9"}

    def test_document_index(self):
        upsert_document("firefox", "bm1", "A", "http://a", None)
        d2 = upsert_document("firefox", "bm2", "B", "http://b", None)
        idx = document_index("firefox")
        assert idx["bm1"] != idx["bm2"]
        assert set(idx) == {"bm1", "bm2"}

    def test_insert_empty_chunks_no_error(self):
        insert_chunks([])   # should not raise
