"""Tests for ingest-time academic classification."""

import sqlalchemy as sa

from pka.classification import (
    classify_document,
    resolve_general_tag_filter,
    sync_classification_tags,
)
from pka.constants import Source, TagOrigin
from pka.db.queries import DocumentWrite, get_engine, init_db, upsert_document
from pka.db.schema import overlay_tags


class TestClassifyDocument:
    def test_zotero_journal_article(self):
        assert classify_document(
            Source.ZOTERO,
            item_type="journalArticle",
        ) == ["academic", "paper"]

    def test_zotero_preprint(self):
        assert classify_document(
            Source.ZOTERO,
            item_type="preprint",
        ) == ["academic", "preprint"]

    def test_zotero_book_not_academic(self):
        assert classify_document(Source.ZOTERO, item_type="book") == []

    def test_firefox_arxiv_preprint(self):
        assert classify_document(
            Source.FIREFOX,
            url_or_path="https://arxiv.org/abs/2301.00001",
        ) == ["academic", "preprint"]

    def test_firefox_doi_paper(self):
        assert classify_document(
            Source.FIREFOX,
            url_or_path="https://doi.org/10.1234/example",
        ) == ["academic", "paper"]

    def test_firefox_pmc_paper(self):
        assert classify_document(
            Source.FIREFOX,
            url_or_path="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/",
        ) == ["academic", "paper"]

    def test_firefox_generic_url(self):
        assert (
            classify_document(
                Source.FIREFOX,
                url_or_path="https://example.com/blog",
            )
            == []
        )

    def test_calibre_not_classified(self):
        assert classify_document(Source.CALIBRE, url_or_path="/books/foo.epub") == []


class TestResolveGeneralTagFilter:
    def test_off(self):
        assert resolve_general_tag_filter(False, []) is None

    def test_academic_all(self):
        assert resolve_general_tag_filter(True, []) == ["academic"]

    def test_paper_only(self):
        assert resolve_general_tag_filter(True, ["paper"]) == ["paper"]

    def test_both_subs_means_all(self):
        assert resolve_general_tag_filter(True, ["paper", "preprint"]) == ["academic"]


class TestSyncClassificationTags:
    def test_writes_inferred_tags(self):
        init_db()
        doc_id = upsert_document(
            DocumentWrite(
                Source.ZOTERO,
                "Z001",
                "Paper",
                "https://example.com",
                1700000000,
                item_type="journalArticle",
            )
        )
        sync_classification_tags(doc_id, ["academic", "paper"])
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(overlay_tags.c.tag, overlay_tags.c.origin).where(
                    overlay_tags.c.document_id == doc_id
                )
            ).fetchall()
        tags = {r[0]: r[1] for r in rows}
        assert tags["academic"] == str(TagOrigin.INFERRED)
        assert tags["paper"] == str(TagOrigin.INFERRED)

    def test_removes_stale_tags(self):
        init_db()
        doc_id = upsert_document(
            DocumentWrite(
                Source.ZOTERO,
                "Z002",
                "Preprint",
                "https://example.com",
                1700000000,
                item_type="preprint",
            )
        )
        sync_classification_tags(doc_id, ["academic", "preprint"])
        sync_classification_tags(doc_id, ["academic", "paper"])
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(overlay_tags.c.tag).where(overlay_tags.c.document_id == doc_id)
            ).fetchall()
        assert {r[0] for r in rows} == {"academic", "paper"}

    def test_clears_tags_when_unclassified(self):
        init_db()
        doc_id = upsert_document(
            DocumentWrite(
                Source.ZOTERO,
                "Z003",
                "Book",
                "https://example.com",
                1700000000,
                item_type="book",
            )
        )
        sync_classification_tags(doc_id, ["academic", "paper"])
        sync_classification_tags(doc_id, [])
        with get_engine().connect() as con:
            n = con.execute(
                sa.select(sa.func.count())
                .select_from(overlay_tags)
                .where(overlay_tags.c.document_id == doc_id)
            ).scalar()
        assert n == 0
