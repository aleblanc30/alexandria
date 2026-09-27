"""Retention of the extracted body text (``document_texts``).

Slices 1-2 of ``planning/FULL_TEXT_RETENTION.md``: the table is written and
read, nothing consumes it yet. The load-bearing assertions are about *what* is
stored — the body rather than the embedding composite, and a section map whose
offsets still slice that body after it has been stripped — and about retention
never costing a document its ordinary ingestion.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import pka.ingestion.reddit_sync as rs
from pka.config import settings as cfg
from pka.constants import Source
from pka.db.queries import document_index, get_engine, init_db
from pka.db.schema import chunks, document_texts
from pka.ingestion.text_store import (
    content_hash,
    document_text_meta,
    load_document_text,
    store_document_text,
)
from tests.conftest import make_document


@pytest.fixture()
def db(empty_vector_store):
    init_db()


def _new_doc(source: str = "firefox", source_id: str = "F1", title: str = "A page") -> int:
    return make_document(source, source_id, title, f"https://example.com/{source_id}", None)


def _row_count() -> int:
    with get_engine().connect() as con:
        return con.execute(sa.select(sa.func.count()).select_from(document_texts)).scalar()


# ── The store itself ─────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_text_survives_compression_unchanged(self, db):
        doc_id = _new_doc()
        text = "Prosaic paragraph. Àccénts, emoji 🐝, and a tab\tinside.\n\n" * 500

        assert store_document_text(doc_id, text)

        assert load_document_text(doc_id) == text.strip()

    def test_meta_describes_the_plain_text_not_the_blob(self, db):
        doc_id = _new_doc()
        text = "A body long enough that zlib actually saves something. " * 100

        store_document_text(doc_id, text)

        meta = document_text_meta(doc_id)
        assert meta["char_count"] == len(text.strip())
        assert meta["content_hash"] == content_hash(text.strip())
        assert meta["encoding"] == "zlib"
        assert meta["blocks"] is None
        assert meta["extracted_at"] > 0

    def test_stored_blob_is_smaller_than_the_text(self, db):
        doc_id = _new_doc()
        text = "Repetitive prose compresses well and that is the whole argument. " * 200

        store_document_text(doc_id, text)

        with get_engine().connect() as con:
            blob = con.execute(
                sa.select(document_texts.c.text).where(document_texts.c.document_id == doc_id)
            ).scalar()
        assert len(blob) < len(text.encode()) / 2

    def test_blocks_round_trip(self, db):
        """The section map paginated sources will need in slice 2."""
        doc_id = _new_doc("calibre", "C1")
        blocks = [{"index": 0, "title": "Ch. 1", "page_start": 1, "page_end": 9, "offset": 0}]

        store_document_text(doc_id, "Chapter one text.", blocks=blocks)

        assert document_text_meta(doc_id)["blocks"] == blocks

    def test_missing_document_reads_as_none(self, db):
        """The ordinary case for anything ingested before retention shipped."""
        assert load_document_text(_new_doc()) is None
        assert document_text_meta(_new_doc("firefox", "F2")) is None


class TestCodec:
    """``encoding`` is the hedge that makes a codec change a migration."""

    def test_raw_encoding_is_readable(self):
        from pka.ingestion.text_store import decode_text

        assert decode_text(b"plain bytes", "raw") == "plain bytes"

    def test_unknown_encoding_refuses_to_guess(self):
        from pka.ingestion.text_store import decode_text

        with pytest.raises(ValueError, match="Unknown document_texts encoding"):
            decode_text(b"\x00\x01", "brotli")

    def test_an_undecodable_row_reads_as_none(self, db):
        """A corrupt blob must not take down whatever is reading the corpus."""
        doc_id = _new_doc()
        store_document_text(doc_id, "Readable body text.")
        with get_engine().begin() as con:
            con.execute(
                document_texts.update()
                .where(document_texts.c.document_id == doc_id)
                .values(text=b"not zlib at all")
            )

        assert load_document_text(doc_id) is None


class TestWriteRules:
    def test_second_store_replaces_rather_than_duplicates(self, db):
        doc_id = _new_doc()
        store_document_text(doc_id, "The first fetch of this page.")

        store_document_text(doc_id, "The page after it was rewritten.")

        assert _row_count() == 1
        assert load_document_text(doc_id) == "The page after it was rewritten."
        assert document_text_meta(doc_id)["content_hash"] == content_hash(
            "The page after it was rewritten."
        )

    @pytest.mark.parametrize("text", ["", "   \n\t "])
    def test_empty_text_stores_nothing(self, db, text):
        doc_id = _new_doc()

        assert store_document_text(doc_id, text) is False
        assert _row_count() == 0

    def test_dry_run_stores_nothing(self, db):
        assert store_document_text(_new_doc(), "A body.", dry_run=True) is False
        assert _row_count() == 0

    def test_flag_off_stores_nothing(self, db, monkeypatch):
        monkeypatch.setattr(cfg, "retain_document_text", False)

        assert store_document_text(_new_doc(), "A body.") is False
        assert _row_count() == 0


# ── Retention must not cost a document its ingestion ─────────────────────────


def test_a_failing_store_does_not_fail_the_document(db, mock_chroma, monkeypatch):
    from pka.ingestion import text_store
    from pka.ingestion.runners.firefox import embed_fetched_text

    def boom(_text: str) -> bytes:
        raise RuntimeError("disk full")

    monkeypatch.setattr(text_store, "encode_text", boom)
    doc_id = _new_doc("firefox", "F9", "Paxos Made Simple")

    outcome = embed_fetched_text(
        doc_id,
        "The protocol proceeds in two phases and the body is long enough to chunk.",
        skip_existing=False,
    )

    assert outcome["processed"] and outcome["chunks"] > 0
    assert _row_count() == 0


# ── What the runners store ───────────────────────────────────────────────────


class TestFirefox:
    def test_stores_the_body_not_the_embedding_composite(self, db, mock_chroma):
        """Title and card summary already live on ``documents`` — storing the
        composite would double them on every re-chunk."""
        from pka.ingestion.runners.firefox import embed_fetched_text

        doc_id = _new_doc("firefox", "F100", "Paxos Made Simple")
        body = (
            "The protocol proceeds in two phases. A proposer picks a number and "
            "asks the acceptors to promise. Nothing here names the subject."
        )
        abstract = "We revisit consensus for replicated logs."

        embed_fetched_text(doc_id, body, card_summary=abstract, skip_existing=False)

        stored = load_document_text(doc_id)
        assert stored == body
        assert "Paxos Made Simple" not in stored
        assert abstract not in stored

    def test_refetch_refreshes_the_row(self, db, mock_chroma):
        from pka.ingestion.runners.firefox import embed_fetched_text

        doc_id = _new_doc("firefox", "F101", "A page")
        embed_fetched_text(doc_id, "The page as it was first fetched, at length.", None)

        embed_fetched_text(
            doc_id, "The page after the site rewrote it, at length.", skip_existing=False
        )

        assert _row_count() == 1
        assert load_document_text(doc_id) == "The page after the site rewrote it, at length."

    def test_dry_run_ingestion_stores_nothing(self, db, mock_chroma):
        from pka.ingestion.runners.firefox import embed_fetched_text

        doc_id = _new_doc("firefox", "F102", "A page")

        embed_fetched_text(doc_id, "A body long enough to chunk normally.", dry_run=True)

        assert _row_count() == 0


class TestSectionBlocks:
    """The map that lets a re-chunk reproduce section and page metadata."""

    def test_blocks_slice_the_joined_text_back_into_sections(self):
        from pka.ingestion.text_store import section_blocks

        sections = [
            {
                "title": "Pages 1–5",
                "text": "First span. ",
                "index": 0,
                "page_start": 1,
                "page_end": 5,
            },
            {
                "title": "Pages 6–10",
                "text": "Second span.",
                "index": 1,
                "page_start": 6,
                "page_end": 10,
            },
        ]

        text, blocks = section_blocks(sections)

        assert text == "First span.\n\nSecond span."
        assert [text[b["offset"] : b["offset"] + b["length"]] for b in blocks] == [
            "First span.",
            "Second span.",
        ]
        assert [(b["page_start"], b["page_end"]) for b in blocks] == [(1, 5), (6, 10)]
        assert [b["index"] for b in blocks] == [0, 1]

    def test_empty_sections_are_dropped_so_offsets_survive_the_strip(self):
        """``store_document_text`` strips; an offset past leading whitespace would lie."""
        from pka.ingestion.text_store import section_blocks

        sections = [
            {"title": "Front matter", "text": "   ", "index": 0},
            {"title": "Ch1", "text": "Real text.", "index": 1},
        ]

        text, blocks = section_blocks(sections)

        assert text == "Real text." == text.strip()
        assert len(blocks) == 1
        assert text[blocks[0]["offset"] : blocks[0]["offset"] + blocks[0]["length"]] == "Real text."

    def test_epub_chapters_carry_no_pages(self):
        from pka.ingestion.text_store import section_blocks

        _, blocks = section_blocks([{"title": "Ch1", "text": "Chapter text.", "index": 0}])

        assert blocks[0]["page_start"] is None and blocks[0]["page_end"] is None

    def test_no_usable_sections_is_an_empty_body(self):
        from pka.ingestion.text_store import section_blocks

        assert section_blocks([]) == ("", [])
        assert section_blocks([{"text": "  ", "index": 0}]) == ("", [])


class TestCalibre:
    """Slice 2: the extraction is retained, not just the chunks cut from it."""

    def _book(self, tmp_path, source_id="PDF1"):
        from pka.connectors.calibre import CalibreBook

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF")
        return CalibreBook(
            source_id=source_id,
            title="A Book",
            authors=["Author A"],
            description="A solid description with multiple sentences. Enough to chunk.",
            publisher="Pub",
            series=None,
            series_index=None,
            year=2023,
            isbn=None,
            tags=[],
            formats=["PDF"],
            preferred_path=pdf,
            date_added=1700000000,
            rating=None,
        )

    def _sections(self):
        return [
            {
                "title": "Pages 1–10",
                "text": "The opening pages. They say a number of things worth chunking.",
                "index": 0,
                "page_start": 1,
                "page_end": 10,
            },
            {
                "title": "Pages 11–20",
                "text": "The next pages. They continue at similar length and detail.",
                "index": 1,
                "page_start": 11,
                "page_end": 20,
            },
        ]

    def _run(self, tmp_path, monkeypatch, *, dry_run=False):
        from pka.ingestion.book_extractor import BookExtraction
        from pka.ingestion.runners.calibre import ingest_calibre_books, ingest_calibre_fulltext

        book = self._book(tmp_path)
        ingest_calibre_books([book])
        monkeypatch.setattr(
            "pka.ingestion.runners.calibre.extract_book_report",
            lambda p, **kw: BookExtraction(self._sections()),
        )
        ingest_calibre_fulltext([book], dry_run=dry_run)
        return document_index(Source.CALIBRE)[book.source_id]

    def test_full_text_is_retained_with_its_section_map(
        self, db, mock_chroma, tmp_path, monkeypatch
    ):
        doc_id = self._run(tmp_path, monkeypatch)

        stored = load_document_text(doc_id)
        blocks = document_text_meta(doc_id)["blocks"]
        assert stored == "\n\n".join(s["text"] for s in self._sections())
        assert [stored[b["offset"] : b["offset"] + b["length"]] for b in blocks] == [
            s["text"] for s in self._sections()
        ]
        assert [(b["title"], b["page_start"], b["page_end"]) for b in blocks] == [
            ("Pages 1–10", 1, 10),
            ("Pages 11–20", 11, 20),
        ]

    def test_dry_run_stores_nothing(self, db, mock_chroma, tmp_path, monkeypatch):
        doc_id = self._run(tmp_path, monkeypatch, dry_run=True)

        assert load_document_text(doc_id) is None


class TestReddit:
    def _sync(self, monkeypatch, reddit_saved_items):
        monkeypatch.setattr(rs, "load_saved", lambda *a, **k: list(reddit_saved_items))
        rs.sync_reddit_metadata()
        return document_index(Source.REDDIT)

    def test_fetched_link_post_body_is_retained(
        self, db, monkeypatch, reddit_saved_items, mock_chroma
    ):
        from pka.ingestion.runners.reddit import embed_fetched_text

        doc_id = self._sync(monkeypatch, reddit_saved_items)["t3_linkpost"]
        body = "The article behind the link, fetched over HTTP and long enough to chunk."

        embed_fetched_text(doc_id, body, skip_existing=False)

        assert load_document_text(doc_id) == body

    def test_inline_post_and_comment_are_not_duplicated(
        self, db, monkeypatch, reddit_saved_items, mock_chroma
    ):
        """``reddit_items.body`` already holds these verbatim (plan §3)."""
        from pka.ingestion.runners.reddit import ingest_reddit_embed

        index = self._sync(monkeypatch, reddit_saved_items)
        inline = [s for s in reddit_saved_items if s.external_url is None]

        ingest_reddit_embed(inline, skip_existing=False)

        assert load_document_text(index["t3_selfpost"]) is None
        assert load_document_text(index["t1_comment1"]) is None
        with get_engine().connect() as con:
            embedded = con.execute(
                sa.select(sa.func.count())
                .select_from(chunks)
                .where(chunks.c.document_id == index["t3_selfpost"])
            ).scalar()
        assert embedded > 0
