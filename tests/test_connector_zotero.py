
import pytest

from pka.connectors.zotero import (
    ZoteroItem,
    load_items,
    zotero_card_summary,
    zotero_document_url_or_path,
    zotero_embed_text,
)


class TestLoadItems:
    def test_returns_list_of_zotero_items(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        assert all(isinstance(i, ZoteroItem) for i in items)

    def test_excludes_attachments_and_notes(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        # journalArticle (2) + annotation (1); attachment and note excluded
        assert len(items) == 3
        assert all(i.item_type not in ("attachment", "note") for i in items)

    def test_title_extracted(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        titles = {i.title for i in items}
        assert "Raft Consensus" in titles

    def test_abstract_extracted(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert raft.abstract == "A paper about Raft."

    def test_doi_extracted(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert raft.doi == "10.1/raft"

    def test_year_extracted(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert raft.year == 2023

    def test_authors_extracted(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert "Diego Ongaro" in raft.authors

    def test_collections_extracted(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert "Distributed Systems" in raft.collections

    def test_tags_extracted_verbatim(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert "consensus" in raft.tags

    def test_pdf_path_resolved(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert raft.pdf_path is not None
        assert str(raft.pdf_path).endswith("raft.pdf")

    def test_item_without_abstract_has_none(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        bare = next(i for i in items if i.title == "Bare Article")
        assert bare.abstract is None

    def test_item_without_authors_has_empty_list(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        bare = next(i for i in items if i.title == "Bare Article")
        assert bare.authors == []

    def test_raises_if_db_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_items(
                zotero_db=tmp_path / "nonexistent.sqlite",
                copy_path=tmp_path / "copy.sqlite",
            )

    def test_date_added_is_unix_timestamp(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        raft = next(i for i in items if i.title == "Raft Consensus")
        assert isinstance(raft.date_added, int)
        assert raft.date_added > 0

    def test_annotation_highlight_text_extracted(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        ann = next(i for i in items if i.source_id == "ANN00001")
        assert ann.item_type == "annotation"
        assert "highlighted passage" in (ann.highlight_text or "")

    def test_pdf_attachment_key_loaded(self, zotero_db, tmp_path):
        items = load_items(zotero_db=zotero_db, copy_path=tmp_path / "copy.sqlite")
        with_pdf = [i for i in items if i.pdf_path]
        assert with_pdf, "fixture should include at least one PDF attachment"
        assert all(i.pdf_attachment_key for i in with_pdf)
        assert len(with_pdf[0].pdf_attachment_key or "") == 8

    def test_zotero_document_url_or_path_prefers_http_url(self, tmp_path):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF")
        item = ZoteroItem(
            source_id="X", title="T", authors=[], abstract=None, year=None,
            doi="10.1/xyz", url="https://journal.example/article",
            item_type="journalArticle", collections=[], tags=[],
            pdf_path=pdf, date_added=None,
        )
        assert zotero_document_url_or_path(item) == "https://journal.example/article"

    def test_zotero_document_url_or_path_pdf_without_url(self, tmp_path):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF")
        item = ZoteroItem(
            source_id="X", title="T", authors=[], abstract=None, year=None,
            doi=None, url=None, item_type="journalArticle", collections=[], tags=[],
            pdf_path=pdf, date_added=None,
        )
        assert zotero_document_url_or_path(item) == str(pdf)

    def test_zotero_embed_text_includes_authors(self):
        item = ZoteroItem(
            source_id="X", title="Short title", authors=["Ada Lovelace"],
            abstract=None, year=None, doi=None, url=None, item_type="journalArticle",
            collections=[], tags=[], pdf_path=None, date_added=None,
        )
        text = zotero_embed_text(item)
        assert "Short title" in text
        assert "Ada Lovelace" in text

    def test_zotero_embed_text_uses_highlight_for_annotations(self):
        item = ZoteroItem(
            source_id="A", title="", authors=[], abstract=None, year=None,
            doi=None, url=None, item_type="annotation", collections=[], tags=[],
            pdf_path=None, date_added=None,
            highlight_text="Important PDF highlight text here.",
        )
        assert zotero_embed_text(item) == "Important PDF highlight text here."


class TestDevZoteroCopy:
    def test_dev_mode_reuses_one_time_copy(self, zotero_db, monkeypatch):
        from pka import config

        monkeypatch.setattr(config.settings, "dev", True)
        copy_path = config.settings.zotero_db_copy
        assert not copy_path.exists()

        first = load_items(zotero_db=zotero_db)
        assert copy_path.exists()
        assert len(first) == 3
        mtime = copy_path.stat().st_mtime

        second = load_items(zotero_db=zotero_db)
        assert copy_path.stat().st_mtime == mtime
        assert len(second) == 3

        load_items(zotero_db=zotero_db, refresh=True)
        assert copy_path.stat().st_mtime >= mtime


class TestZoteroCardSummary:
    def test_abstract(self):
        item = ZoteroItem(
            source_id="1", title="T", authors=[], abstract="Paper abstract.",
            year=None, doi=None, url=None, item_type="journalArticle",
            collections=[], tags=[], pdf_path=None, date_added=None,
        )
        assert zotero_card_summary(item) == "Paper abstract."

    def test_annotation_uses_highlight(self):
        item = ZoteroItem(
            source_id="1", title="", authors=[], abstract=None,
            year=None, doi=None, url=None, item_type="annotation",
            collections=[], tags=[], pdf_path=None, date_added=None,
            highlight_text="Highlighted passage.",
        )
        assert zotero_card_summary(item) == "Highlighted passage."

    def test_missing_text_returns_none(self):
        item = ZoteroItem(
            source_id="1", title="T", authors=[], abstract=None,
            year=None, doi=None, url=None, item_type="journalArticle",
            collections=[], tags=[], pdf_path=None, date_added=None,
        )
        assert zotero_card_summary(item) is None
