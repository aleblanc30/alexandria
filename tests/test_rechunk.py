"""Re-chunking from retained text (``planning/FULL_TEXT_RETENTION.md`` §6.2).

This is the pass retention exists for, so the assertions that matter are the
ones about *not losing anything*: the summary chunk that costs inference stays,
the section and page metadata a book's chunks carried is reproduced rather than
downgraded, and a document whose re-chunk yields nothing keeps the chunks it
already had instead of being silently emptied.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import Source
from pka.db.queries import document_index, get_engine, init_db, insert_chunks
from pka.db.schema import chunks
from pka.ingestion.rechunk import rechunk_documents
from pka.ingestion.text_store import store_document_text
from tests.conftest import make_document

# Twelve sentences, each comfortably over half of ``min_chunk_chars``, so a
# two-sentence window survives the minimum-length filter and a five-sentence one
# produces visibly fewer chunks. Anything shorter and every window is discarded,
# which exercises the fallback path rather than the chunker.
BODY = " ".join(
    f"The protocol proceeds through phase number {i} of the run."
    f" A quorum of acceptors must answer before step {i} commits."
    for i in range(6)
)


@pytest.fixture()
def db(empty_vector_store):
    init_db()


def _chunk_rows(doc_id: int) -> list[sa.Row]:
    with get_engine().connect() as con:
        return list(
            con.execute(
                sa.select(
                    chunks.c.id,
                    chunks.c.chunk_index,
                    chunks.c.text,
                    chunks.c.chunk_pass,
                    chunks.c.vector_id,
                    chunks.c.page_start,
                    chunks.c.page_end,
                )
                .where(chunks.c.document_id == doc_id)
                .order_by(chunks.c.chunk_index)
            ).fetchall()
        )


def _fetched_doc(source_id: str = "F1", title: str = "Paxos Made Simple") -> int:
    """A Firefox document ingested the way the fetch worker ingests one."""
    from pka.ingestion.runners.firefox import embed_fetched_text

    doc_id = make_document("firefox", source_id, title, f"https://example.com/{source_id}", None)
    embed_fetched_text(doc_id, BODY, skip_existing=False)
    return doc_id


class TestFetchedBody:
    def test_new_chunker_settings_replace_the_old_chunks(self, db, mock_chroma, monkeypatch):
        store, col = mock_chroma
        doc_id = _fetched_doc()
        before = _chunk_rows(doc_id)
        assert len(before) >= 1

        # A two-sentence window cuts the same body far more finely.
        monkeypatch.setattr(cfg, "chunk_sentences", 2)
        monkeypatch.setattr(cfg, "chunk_overlap", 0)
        stats = rechunk_documents()

        after = _chunk_rows(doc_id)
        assert stats["rechunked"] == 1
        assert stats["chunks_removed"] == len(before)
        assert stats["chunks_added"] == len(after)
        assert len(after) > len(before)
        # Chroma is told to drop the superseded vectors, not just SQLite — the
        # in-memory double records upserts and not deletes, so the delete call is
        # what there is to check.
        old_vectors = {r.vector_id for r in before}
        deleted = {vid for call in col.delete.call_args_list for vid in call.kwargs["ids"]}
        assert deleted == old_vectors
        assert not (old_vectors & {r.vector_id for r in after})
        assert {r.vector_id for r in after} <= set(store)

    def test_title_and_card_summary_are_folded_back_in(self, db, mock_chroma):
        """`document_texts` holds the body alone, so the composite is rebuilt."""
        doc_id = _fetched_doc(title="Paxos Made Simple")

        rechunk_documents()

        texts = " ".join(r.text for r in _chunk_rows(doc_id))
        assert "Paxos Made Simple" in texts
        assert "The protocol proceeds through phase number 0 of the run." in texts

    def test_the_summary_chunk_survives_and_indices_do_not_collide(self, db, mock_chroma):
        """A summary costs inference; the chunker changing is no reason to lose it."""
        doc_id = _fetched_doc()
        body_count = len(_chunk_rows(doc_id))
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": body_count,
                    "text": "A summary of the protocol.",
                    "token_count": 5,
                    "vector_id": "vec-summary",
                    "chunk_pass": "summary",
                }
            ]
        )

        rechunk_documents()

        rows = _chunk_rows(doc_id)
        summaries = [r for r in rows if r.chunk_pass == "summary"]
        assert len(summaries) == 1
        assert summaries[0].vector_id == "vec-summary"
        assert len({r.chunk_index for r in rows}) == len(rows), "chunk indices collided"

    def test_source_filter_scopes_the_pass(self, db, mock_chroma):
        firefox = _fetched_doc("F1")
        other = make_document("zotero", "Z1", "A paper", None, None)
        store_document_text(other, BODY)

        stats = rechunk_documents(source="firefox")

        assert stats["candidates"] == 1
        assert stats["rechunked"] == 1
        assert _chunk_rows(firefox)
        assert not _chunk_rows(other), "a scoped pass must not touch another source"


class TestCalibreBlocks:
    SECTIONS = [
        {
            "title": "Pages 1–10",
            "text": (
                "The opening pages introduce the subject at some length."
                " They say a number of things that are worth chunking here."
                " A third sentence keeps the section above the minimum."
                " And a fourth gives the window something to slide over."
            ),
            "index": 0,
            "page_start": 1,
            "page_end": 10,
        },
        {
            "title": "Pages 11–20",
            "text": (
                "The next pages continue at a similar length and detail."
                " They develop the argument the opening pages introduced."
                " A third sentence keeps this section above the minimum too."
                " And a fourth rounds the page group out at four."
            ),
            "index": 1,
            "page_start": 11,
            "page_end": 20,
        },
    ]

    def _book_doc(self, tmp_path, monkeypatch) -> int:
        from pka.connectors.calibre import CalibreBook
        from pka.ingestion.book_extractor import BookExtraction
        from pka.ingestion.runners.calibre import ingest_calibre_books, ingest_calibre_fulltext

        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF")
        book = CalibreBook(
            source_id="PDF1",
            title="A Book",
            authors=["Author A"],
            description="A solid description with several sentences. Enough to chunk.",
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
        ingest_calibre_books([book])
        monkeypatch.setattr(
            "pka.ingestion.runners.calibre.extract_book_report",
            lambda p, **kw: BookExtraction(list(self.SECTIONS)),
        )
        ingest_calibre_fulltext([book])
        return document_index(Source.CALIBRE)[book.source_id]

    def test_section_and_page_metadata_are_reproduced(self, db, mock_chroma, tmp_path, monkeypatch):
        """Without the block map this would be a downgrade, not a re-chunk."""
        doc_id = self._book_doc(tmp_path, monkeypatch)
        before = [(r.chunk_pass, r.page_start, r.page_end) for r in _chunk_rows(doc_id)]
        assert ("fulltext", 1, 10) in before and ("fulltext", 11, 20) in before

        monkeypatch.setattr(cfg, "chunk_sentences", 2)
        monkeypatch.setattr(cfg, "chunk_overlap", 0)
        stats = rechunk_documents()

        after = _chunk_rows(doc_id)
        assert stats["rechunked"] == 1
        pages = {(r.page_start, r.page_end) for r in after if r.chunk_pass == "fulltext"}
        assert pages == {(1, 10), (11, 20)}
        # Pass 1's metadata chunk is not body text and must be left alone.
        assert [r for r in after if r.chunk_pass == "metadata"]


class TestGuards:
    def test_documents_without_retained_text_are_not_candidates(self, db, mock_chroma):
        """There is no backfill, so a pre-retention document is simply untouched."""
        doc_id = make_document("firefox", "OLD", "An old bookmark", "https://e.com/o", None)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "Body text from before retention shipped.",
                    "token_count": 6,
                    "vector_id": "vec-old",
                }
            ]
        )

        stats = rechunk_documents()

        assert stats["candidates"] == 0
        assert [r.vector_id for r in _chunk_rows(doc_id)] == ["vec-old"]

    def test_dry_run_counts_without_touching_anything(self, db, mock_chroma):
        doc_id = _fetched_doc()
        before = _chunk_rows(doc_id)

        stats = rechunk_documents(dry_run=True)

        assert stats["candidates"] == 1
        assert stats["rechunked"] == 0
        assert _chunk_rows(doc_id) == before

    def test_a_document_that_yields_no_chunks_keeps_the_ones_it_has(
        self, db, mock_chroma, monkeypatch
    ):
        """Better to leave a document as it was than to empty it silently."""
        doc_id = _fetched_doc()
        before = _chunk_rows(doc_id)

        import pka.ingestion.rechunk as rc

        monkeypatch.setattr(
            rc, "ingest_text_block", lambda *a, **k: {"chunks_added": 0, "skipped": True}
        )
        stats = rechunk_documents()

        assert stats == {
            "candidates": 1,
            "rechunked": 0,
            "skipped": 1,
            "chunks_added": 0,
            "chunks_removed": 0,
            "vectors_purged": 0,
        }
        assert _chunk_rows(doc_id) == before

    def test_retention_flag_off_leaves_the_pass_with_nothing_to_do(self, db, mock_chroma):
        """Off is the pre-retention world, where a re-chunk is not possible."""
        doc_id = make_document("firefox", "OFF", "A bookmark", "https://e.com/off", None)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "Body text ingested with retention switched off.",
                    "token_count": 7,
                    "vector_id": "vec-off",
                }
            ]
        )

        assert rechunk_documents()["candidates"] == 0

    def test_a_failing_document_does_not_stop_the_pass(self, db, mock_chroma, monkeypatch):
        first = _fetched_doc("F1")
        second = _fetched_doc("F2")

        import pka.ingestion.rechunk as rc

        real = rc.load_document_text

        def _boom(doc_id):
            if doc_id == first:
                raise RuntimeError("unreadable row")
            return real(doc_id)

        monkeypatch.setattr(rc, "load_document_text", _boom)
        stats = rechunk_documents()

        assert stats["candidates"] == 2
        assert stats["rechunked"] == 1
        assert stats["skipped"] == 1
        assert _chunk_rows(first), "the failing document keeps its chunks"
        assert _chunk_rows(second)


# ── API surface (§6.2, §6.3) ────────────────────────────────────────────────


@pytest.fixture()
def client(empty_vector_store):
    init_db()
    from fastapi.testclient import TestClient

    from pka.api.main import app

    return TestClient(app, raise_server_exceptions=True)


def test_rechunk_endpoint_dry_run_counts_without_changing_anything(client, mock_chroma):
    doc_id = _fetched_doc()
    before = _chunk_rows(doc_id)

    r = client.post("/ingestion/rechunk?dry_run=true")

    assert r.status_code == 202
    assert r.json()["stats"]["candidates"] == 1
    assert _chunk_rows(doc_id) == before


def test_rechunk_endpoint_is_blocked_while_a_sync_runs(client, mock_chroma):
    """It rewrites the rows a sync writes, so it waits like a purge does."""
    from pka.ingestion import progress as sp

    _fetched_doc()
    sp.begin_job("firefox", "metadata", phase="loading")
    try:
        assert client.post("/ingestion/rechunk").status_code == 409
        # A dry run only reads, so it stays available.
        assert client.post("/ingestion/rechunk?dry_run=true").status_code == 202
    finally:
        sp.reset("firefox")


def test_rechunk_endpoint_rejects_an_unknown_source(client):
    assert client.post("/ingestion/rechunk?source=bogus").status_code == 400


def test_document_text_endpoint_serves_the_retained_body(client, mock_chroma):
    doc_id = _fetched_doc()

    r = client.get(f"/documents/{doc_id}/text")

    assert r.status_code == 200
    body = r.json()
    assert body["text"] == BODY
    assert body["char_count"] == len(BODY)
    assert body["blocks"] is None


def test_document_text_endpoint_404s_without_retained_text(client):
    doc_id = make_document("firefox", "OLD", "An old bookmark", "https://e.com/o", None)

    assert client.get(f"/documents/{doc_id}/text").status_code == 404
