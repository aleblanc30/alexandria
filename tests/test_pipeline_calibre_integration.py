"""Calibre two-phase ingestion integration test — patch #14.

Verifies that the metadata pass and the full-text pass produce non-colliding,
strictly increasing ``chunk_index`` values.
"""
import pytest
import sqlalchemy as sa

from pka.connectors.calibre import CalibreBook
from pka.db.queries import document_has_chunks, get_engine, init_db
from pka.db.schema import chunks, documents, source_tags


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


def _make_book(**overrides) -> CalibreBook:
    sid = overrides.pop("source_id", "B001")
    defaults = dict(
        source_id=sid, title="Test Book",
        authors=["Author A"],
        description="A solid description with multiple sentences. Enough to chunk.",
        publisher="Pub", series=None, series_index=None,
        year=2023, isbn="ISBN001", tags=["tag1"],
        formats=["EPUB"], preferred_path=None,
        date_added=1700000000, rating=8,
    )
    return CalibreBook(**{**defaults, **overrides})


def test_metadata_pass_creates_chunks(mock_chroma):
    from pka.ingestion.runners.calibre import ingest_calibre_books

    book = _make_book()
    stats = ingest_calibre_books([book])
    assert stats["processed"] == 1

    with get_engine().connect() as con:
        doc_id = con.execute(
            sa.select(documents.c.id).where(documents.c.source_id == "B001")
        ).scalar()
    assert document_has_chunks(doc_id)


def test_metadata_pass_embeds_title_when_body_too_short(mock_chroma):
    """A book with no description and a short title still gets one chunk so it
    stays findable by semantic search on the title."""
    from pka.ingestion.runners.calibre import ingest_calibre_books

    book = _make_book(source_id="SHORT", title="Dune", description=None)
    stats = ingest_calibre_books([book])
    assert stats["processed"] == 1

    with get_engine().connect() as con:
        doc_id = con.execute(
            sa.select(documents.c.id).where(documents.c.source_id == "SHORT")
        ).scalar()
        chunk_texts = [
            r[0] for r in con.execute(
                sa.select(chunks.c.text).where(chunks.c.document_id == doc_id)
            ).fetchall()
        ]

    assert document_has_chunks(doc_id)
    assert chunk_texts == ["Dune"]


def test_fulltext_pass_offsets_chunk_indices(
    mock_chroma, tmp_path, monkeypatch,
):
    from pka.ingestion.runners.calibre import ingest_calibre_books, ingest_calibre_fulltext

    epub = tmp_path / "fake.epub"
    epub.write_bytes(b"PK")
    book = _make_book(preferred_path=epub)
    ingest_calibre_books([book])

    # Mock fulltext extractor to return two sections
    monkeypatch.setattr(
        "pka.ingestion.runners.calibre.extract_book_text",
        lambda p, **kw: [
            {"title": "Ch1",
             "text":  "Sentence one. Sentence two. Sentence three.",
             "index": 0},
            {"title": "Ch2",
             "text":  "Another section. With more text. And more.",
             "index": 1},
        ],
    )
    ingest_calibre_fulltext([book])

    with get_engine().connect() as con:
        idx_rows = con.execute(
            sa.select(chunks.c.chunk_index).order_by(chunks.c.chunk_index)
        ).fetchall()
    indices = [r[0] for r in idx_rows]

    # Strictly increasing, no duplicates
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)


def test_metadata_only_registers_without_chunks(mock_chroma):
    from pka.ingestion.runners.calibre import ingest_calibre_metadata

    book = _make_book()
    stats = ingest_calibre_metadata([book])
    assert stats["processed"] == 1
    with get_engine().connect() as con:
        doc_id = con.execute(
            sa.select(documents.c.id).where(documents.c.source_id == "B001")
        ).scalar()
    assert not document_has_chunks(doc_id)


def test_long_tags_bundled_into_note_and_dropped_from_tags(mock_chroma):
    from pka.ingestion.runners.calibre import ingest_calibre_metadata

    book = _make_book(
        source_id="NOTE1",
        tags=["psychology", "imported from zotero on friday"],
    )
    ingest_calibre_metadata([book])

    with get_engine().connect() as con:
        doc_id, note = con.execute(
            sa.select(documents.c.id, documents.c.note)
            .where(documents.c.source_id == "NOTE1")
        ).one()
        stored_tags = [
            r[0] for r in con.execute(
                sa.select(source_tags.c.tag_string)
                .where(source_tags.c.document_id == doc_id)
            ).fetchall()
        ]

    # Long tag routed to note; short tag stays a source tag.
    assert stored_tags == ["psychology"]
    assert note == "imported from zotero on friday"


def test_metadata_skips_known_books(mock_chroma):
    from pka.ingestion.runners.calibre import ingest_calibre_metadata

    book = _make_book()
    ingest_calibre_metadata([book])
    stats = ingest_calibre_metadata([book])
    assert stats["skipped"] == 1


def test_embed_stops_on_cancel(mock_chroma):
    from pka.ingestion import sync_progress as sp
    from pka.ingestion.runners.calibre import ingest_calibre_books

    sp.begin("calibre")
    sp.set_phase("calibre", "embedding", 2)
    sp.request_cancel("calibre")
    stats = ingest_calibre_books(
        [_make_book(source_id="C1"), _make_book(source_id="C2")],
        progress_key="calibre",
    )
    assert stats.get("stopped") == "cancel"
