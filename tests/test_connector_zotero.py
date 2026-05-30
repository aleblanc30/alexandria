import pytest
from pathlib import Path
from pka.connectors.zotero import load_items, ZoteroItem, zotero_embed_text


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

    def test_zotero_embed_text_includes_authors(self):
        item = ZoteroItem(
            source_id="X", title="Short title", authors=["Ada Lovelace"],
            abstract=None, year=None, doi=None, item_type="journalArticle",
            collections=[], tags=[], pdf_path=None, date_added=None,
        )
        text = zotero_embed_text(item)
        assert "Short title" in text
        assert "Ada Lovelace" in text

    def test_zotero_embed_text_uses_highlight_for_annotations(self):
        item = ZoteroItem(
            source_id="A", title="", authors=[], abstract=None, year=None,
            doi=None, item_type="annotation", collections=[], tags=[],
            pdf_path=None, date_added=None,
            highlight_text="Important PDF highlight text here.",
        )
        assert zotero_embed_text(item) == "Important PDF highlight text here."


