"""Calibre two-phase ingestion integration test — patch #14.

Verifies that the metadata pass and the full-text pass produce non-colliding,
strictly increasing ``chunk_index`` values.
"""
import sqlalchemy as sa
import pytest

from pka.connectors.calibre import CalibreBook
from pka.db.queries import document_has_chunks, get_engine, init_db
from pka.db.schema import chunks, documents


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


def _make_book(**overrides) -> CalibreBook:
    defaults = dict(
        source_id="B001", title="Test Book",
        authors=["Author A"],
        description="A solid description with multiple sentences. Enough to chunk.",
        publisher="Pub", series=None, series_index=None,
        year=2023, isbn="ISBN001", tags=["tag1"],
        formats=["EPUB"], preferred_path=None,
        date_added=1700000000, rating=8,
    )
    return CalibreBook(**{**defaults, **overrides})


def test_metadata_pass_creates_chunks(mock_chroma):
    from pka.pipeline import ingest_calibre_books

    book = _make_book()
    stats = ingest_calibre_books([book])
    assert stats["processed"] == 1

    with get_engine().connect() as con:
        doc_id = con.execute(
            sa.select(documents.c.id).where(documents.c.source_id == "B001")
        ).scalar()
    assert document_has_chunks(doc_id)


def test_fulltext_pass_offsets_chunk_indices(
    mock_chroma, tmp_path, monkeypatch,
):
    from pka.pipeline import ingest_calibre_books, ingest_calibre_fulltext

    epub = tmp_path / "fake.epub"
    epub.write_bytes(b"PK")
    book = _make_book(preferred_path=epub)
    ingest_calibre_books([book])

    # Mock fulltext extractor to return two sections
    monkeypatch.setattr(
        "pka.pipeline.extract_book_text",
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
