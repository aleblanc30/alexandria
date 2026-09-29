"""Zotero's second embedding pass: the attached PDF's full text."""

import sqlalchemy as sa

from pka.constants import FetchStatus, PdfTextLayer, Source
from pka.db.engine import get_engine
from pka.db.migrate import init_db
from pka.db.schema import chunks, documents
from pka.ingestion.book_extractor import BookExtraction
from pka.ingestion.runners.zotero import (
    ingest_zotero_embed,
    ingest_zotero_fulltext,
    ingest_zotero_metadata,
)
from pka.ingestion.text_store import document_text_meta, load_document_text
from tests.test_zotero_sync import _zotero_item

_BODY = " ".join(f"Sentence {i} of the paper's body discusses its method." for i in range(30))
_SECTIONS = [
    {"title": "", "text": _BODY, "index": 0, "page_start": 1, "page_end": 2},
    {"title": "", "text": _BODY, "index": 1, "page_start": 3, "page_end": 4},
]


def _pdf_item(tmp_path, key="PDF1", **overrides):
    pdf = tmp_path / f"{key}.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    return _zotero_item(key, pdf_path=pdf, pdf_attachment_key="ATT1", **overrides)


def _extract(monkeypatch, extraction: BookExtraction) -> None:
    monkeypatch.setattr(
        "pka.ingestion.runners.zotero.extract_book_report", lambda path, **kw: extraction
    )


def _chunk_rows(doc_source_id: str) -> list[sa.Row]:
    with get_engine().connect() as con:
        return list(
            con.execute(
                sa.select(
                    chunks.c.chunk_index,
                    chunks.c.chunk_pass,
                    chunks.c.page_start,
                    chunks.c.page_end,
                )
                .select_from(chunks.join(documents, chunks.c.document_id == documents.c.id))
                .where(documents.c.source_id == doc_source_id)
                .order_by(chunks.c.chunk_index)
            ).fetchall()
        )


def _doc(source_id: str) -> sa.Row:
    with get_engine().connect() as con:
        return con.execute(
            sa.select(documents.c.id, documents.c.fetch_status, documents.c.doc_embedding).where(
                documents.c.source_id == source_id
            )
        ).one()


def _archive(item) -> None:
    """Both earlier passes: metadata, then the title + abstract chunk."""
    ingest_zotero_metadata([item])
    ingest_zotero_embed([item])


class TestFulltextPass:
    def test_pdf_body_is_chunked_after_the_abstract(self, mock_chroma, tmp_path, monkeypatch):
        init_db()
        item = _pdf_item(tmp_path)
        _archive(item)
        _extract(monkeypatch, BookExtraction(_SECTIONS))

        stats = ingest_zotero_fulltext([item])

        assert stats["processed"] == 1
        rows = _chunk_rows(item.source_id)
        assert rows[0].chunk_pass == "metadata"
        body = [r for r in rows if r.chunk_pass == "fulltext"]
        assert len(body) == stats["chunks"] > 0
        # Offset past the abstract chunk rather than colliding with index 0.
        assert [r.chunk_index for r in rows] == list(range(len(rows)))
        assert {(r.page_start, r.page_end) for r in body} == {(1, 2), (3, 4)}
        assert _doc(item.source_id).doc_embedding is not None

    def test_the_text_is_retained_with_its_page_map(self, mock_chroma, tmp_path, monkeypatch):
        init_db()
        item = _pdf_item(tmp_path)
        _archive(item)
        _extract(monkeypatch, BookExtraction(_SECTIONS))

        ingest_zotero_fulltext([item])

        doc_id = _doc(item.source_id).id
        assert _BODY in (load_document_text(doc_id) or "")
        blocks = (document_text_meta(doc_id) or {})["blocks"]
        assert [(b["page_start"], b["page_end"]) for b in blocks] == [(1, 2), (3, 4)]

    def test_a_scan_is_marked_no_text_layer(self, mock_chroma, tmp_path, monkeypatch):
        init_db()
        item = _pdf_item(tmp_path)
        _archive(item)
        _extract(monkeypatch, BookExtraction([], status=PdfTextLayer.NONE, page_count=9))

        stats = ingest_zotero_fulltext([item])

        assert stats["no_text_layer"] == 1
        assert _doc(item.source_id).fetch_status == str(FetchStatus.NO_TEXT_LAYER)
        assert [r.chunk_pass for r in _chunk_rows(item.source_id)] == ["metadata"]

    def test_a_missing_file_is_skipped(self, mock_chroma, tmp_path, monkeypatch):
        init_db()
        item = _pdf_item(tmp_path)
        _archive(item)
        item.pdf_path.unlink()
        _extract(monkeypatch, BookExtraction(_SECTIONS))

        stats = ingest_zotero_fulltext([item])

        assert (stats["processed"], stats["skipped"]) == (0, 1)

    def test_an_item_not_yet_archived_is_skipped(self, mock_chroma, tmp_path, monkeypatch):
        init_db()
        _extract(monkeypatch, BookExtraction(_SECTIONS))
        stats = ingest_zotero_fulltext([_pdf_item(tmp_path)])
        assert (stats["processed"], stats["skipped"]) == (0, 1)

    def test_dry_run_writes_nothing(self, mock_chroma, tmp_path, monkeypatch):
        init_db()
        item = _pdf_item(tmp_path)
        _archive(item)
        _extract(monkeypatch, BookExtraction(_SECTIONS))

        stats = ingest_zotero_fulltext([item], dry_run=True)

        assert stats["chunks"] > 0
        assert [r.chunk_pass for r in _chunk_rows(item.source_id)] == ["metadata"]
        assert load_document_text(_doc(item.source_id).id) is None


class TestSyncRunsTheFulltextPassOnce:
    """The loader's filter is the only thing stopping a second sync from
    appending a second copy of every PDF's chunks."""

    def _wire(self, monkeypatch, zotero_db, items):
        monkeypatch.setattr("pka.ingestion.zotero_sync.ensure_zotero_copy", lambda: zotero_db)
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.load_item_keys", lambda **kw: [i.source_id for i in items]
        )
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.load_items",
            lambda **kw: [i for i in items if i.source_id in kw.get("keys", {i.source_id})],
        )

    def test_second_sync_adds_no_chunks(self, mock_chroma, tmp_path, monkeypatch, zotero_db):
        from pka.ingestion.zotero_sync import sync_zotero_ingest

        init_db()
        item = _pdf_item(tmp_path)
        ingest_zotero_metadata([item])
        self._wire(monkeypatch, zotero_db, [item])
        _extract(monkeypatch, BookExtraction(_SECTIONS))

        first = sync_zotero_ingest(progress_key="zotero")
        after_first = len(_chunk_rows(item.source_id))
        second = sync_zotero_ingest(progress_key="zotero")

        assert first["fulltext"]["processed"] == 1
        assert "fulltext" not in second
        assert len(_chunk_rows(item.source_id)) == after_first

    def test_a_scan_is_not_re_read(self, mock_chroma, tmp_path, monkeypatch, zotero_db):
        from pka.ingestion.zotero_sync import _load_zotero_items_for_fulltext

        init_db()
        item = _pdf_item(tmp_path)
        _archive(item)
        self._wire(monkeypatch, zotero_db, [item])
        _extract(monkeypatch, BookExtraction([], status=PdfTextLayer.NONE, page_count=3))
        ingest_zotero_fulltext([item])

        assert _load_zotero_items_for_fulltext() == []

    def test_items_without_a_readable_pdf_are_not_loaded(
        self, mock_chroma, tmp_path, monkeypatch, zotero_db
    ):
        from pka.ingestion.zotero_sync import _load_zotero_items_for_fulltext

        init_db()
        with_pdf = _pdf_item(tmp_path, "HASPDF")
        gone = _pdf_item(tmp_path, "GONE")
        gone.pdf_path.unlink()
        no_pdf = _zotero_item("NOPDF")
        self._wire(monkeypatch, zotero_db, [with_pdf, gone, no_pdf])

        assert [i.source_id for i in _load_zotero_items_for_fulltext()] == ["HASPDF"]


class TestAbstractChunkSurvivesARechunk:
    def test_rechunk_replaces_only_the_pdf_body(self, mock_chroma, tmp_path, monkeypatch):
        from pka.ingestion.rechunk import rechunk_documents

        init_db()
        item = _pdf_item(tmp_path)
        _archive(item)
        _extract(monkeypatch, BookExtraction(_SECTIONS))
        ingest_zotero_fulltext([item])

        stats = rechunk_documents(source=Source.ZOTERO)

        assert stats["rechunked"] == 1
        passes = [r.chunk_pass for r in _chunk_rows(item.source_id)]
        assert passes.count("metadata") == 1
        assert passes.count("fulltext") > 0


class TestMigrationTagsOldAbstractChunks:
    def test_only_untagged_zotero_chunks_are_tagged(self, mock_chroma):
        from pka.db.migrate import _zotero_metadata_pass
        from tests.conftest import make_document

        init_db()
        zot = make_document("zotero", "OLD1", "Old paper", None, None)
        ff = make_document("firefox", "FF1", "Bookmark", "https://example.org", None)
        with get_engine().begin() as con:
            for doc_id, idx, chunk_pass in ((zot, 0, None), (zot, 1, "fulltext"), (ff, 0, None)):
                con.execute(
                    chunks.insert().values(
                        document_id=doc_id,
                        chunk_index=idx,
                        text="t",
                        chunk_pass=chunk_pass,
                    )
                )
            _zotero_metadata_pass(con)

        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(chunks.c.document_id, chunks.c.chunk_index, chunks.c.chunk_pass)
            ).fetchall()
        got = {(r.document_id, r.chunk_index): r.chunk_pass for r in rows}
        assert got == {(zot, 0): "metadata", (zot, 1): "fulltext", (ff, 0): None}
