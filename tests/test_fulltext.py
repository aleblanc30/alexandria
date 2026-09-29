"""Keyword search: the FTS5 indexes, the triggers that keep them current, and the query."""

import pytest
import sqlalchemy as sa

from pka.db.chunks import insert_chunks
from pka.db.documents import update_card_summary
from pka.db.engine import get_engine
from pka.db.fulltext import keyword_document_ids
from pka.db.migrate import init_db
from pka.db.schema import chunks, documents
from tests.conftest import make_document


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


def _chunk(doc_id: int, text: str, index: int = 0) -> None:
    insert_chunks(
        [
            {
                "document_id": doc_id,
                "chunk_index": index,
                "text": text,
                "token_count": len(text.split()),
                "vector_id": f"v-{doc_id}-{index}",
            }
        ]
    )


def _find(query: str, sources=None) -> list[int]:
    with get_engine().connect() as con:
        return keyword_document_ids(con, query, sources)


class TestWhatIsSearched:
    def test_title(self):
        doc = make_document("zotero", "T1", "Protein folding at scale")
        assert _find("folding") == [doc]

    def test_card_summary(self):
        doc = make_document("firefox", "T1", "A bookmark")
        update_card_summary(doc, "Notes on lattice cryptography.")
        assert _find("lattice") == [doc]

    def test_chunk_body(self):
        doc = make_document("calibre", "T1", "A book")
        _chunk(doc, "Deep in chapter nine the heron finally speaks.")
        assert _find("heron finally") == [doc]

    def test_one_row_per_document_however_many_chunks_match(self):
        doc = make_document("calibre", "T1", "A book")
        _chunk(doc, "The heron waits.", 0)
        _chunk(doc, "The heron flies.", 1)
        assert _find("heron") == [doc]


class TestMatching:
    def test_substring_inside_a_word(self):
        doc = make_document("zotero", "T1", "Neural networks")
        assert _find("eural") == [doc]

    def test_case_insensitive(self):
        doc = make_document("zotero", "T1", "Neural networks")
        assert _find("NEURAL") == [doc]

    def test_the_query_is_one_phrase_not_separate_words(self):
        """Contiguous, like the ILIKE it replaces: words out of order do not match."""
        make_document("zotero", "T1", "networks that are neural")
        assert _find("neural networks") == []

    def test_fts_syntax_in_the_query_is_searched_for_literally(self):
        doc = make_document("zotero", "T1", 'The "AND" operator: a-b*c')
        assert _find('"AND" operator: a-b*') == [doc]
        assert _find("NOT OR") == []

    def test_text_without_word_boundaries(self):
        """Trigram needs no spaces, so CJK text is searchable by substring."""
        doc = make_document("zotero", "T1", "深層学習による画像認識")
        assert _find("画像認識") == [doc]

    def test_source_filter(self):
        keep = make_document("zotero", "T1", "Heron studies")
        make_document("firefox", "T2", "Heron sightings")
        assert _find("heron", ["zotero"]) == [keep]


class TestTriggersKeepTheIndexCurrent:
    def test_an_updated_title_is_found_by_its_new_text_only(self):
        doc = make_document("zotero", "T1", "Old heading")
        with get_engine().begin() as con:
            con.execute(documents.update().where(documents.c.id == doc).values(title="New heading"))
        assert _find("New heading") == [doc]
        assert _find("Old heading") == []

    def test_a_deleted_document_is_gone(self):
        doc = make_document("zotero", "T1", "Ephemeral paper")
        with get_engine().begin() as con:
            con.execute(documents.delete().where(documents.c.id == doc))
        assert _find("Ephemeral") == []

    def test_deleted_chunks_are_gone(self):
        doc = make_document("calibre", "T1", "A book")
        _chunk(doc, "The heron waits.")
        with get_engine().begin() as con:
            con.execute(chunks.delete().where(chunks.c.document_id == doc))
        assert _find("heron") == []

    def test_the_index_agrees_with_its_tables(self):
        """FTS5's own consistency check, after a mix of writes."""
        doc = make_document("zotero", "T1", "Heron studies")
        _chunk(doc, "Body text.")
        update_card_summary(doc, "A summary.")
        with get_engine().begin() as con:
            for fts in ("documents_fts", "chunks_fts"):
                con.execute(
                    sa.text(f"INSERT INTO {fts}({fts}, rank) VALUES ('integrity-check', 1)")
                )
