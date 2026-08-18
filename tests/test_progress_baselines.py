"""Tests for DB-derived ingestion progress baselines and status aggregates."""
from __future__ import annotations

import pytest

from pka.constants import FetchStatus, Source
from pka.db.queries import get_engine, init_db, insert_document_if_new
from pka.db.schema import chunks, images
from pka.ingestion.progress_baselines import (
    build_ingestion_status,
    get_phase_baselines,
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_DATA_DIR", str(tmp_path))
    init_db()


def _insert_doc(source: str, source_id: str, fetch_status: str = FetchStatus.PENDING) -> int:
    doc_id = insert_document_if_new(
        source, source_id, f"Title {source_id}", f"http://{source_id}", None,
        fetch_status=fetch_status,
    )
    assert doc_id is not None
    return doc_id


def test_get_phase_baselines_uses_archive_count_when_source_unavailable(monkeypatch):
    _insert_doc(Source.ZOTERO, "z1", FetchStatus.AVAILABLE)
    monkeypatch.setattr(
        "pka.ingestion.progress_baselines.source_corpus_size",
        lambda _src: 0,
    )
    totals, processed, fetch_outcomes = get_phase_baselines(get_engine(), Source.ZOTERO)
    assert totals == {"metadata": 1, "fetching": 1, "embedding": 1}
    assert processed == {"metadata": 1, "fetching": 1, "embedding": 0}
    assert fetch_outcomes is None


def test_get_phase_baselines_prefers_source_corpus_size(monkeypatch):
    _insert_doc(Source.ZOTERO, "z1", FetchStatus.AVAILABLE)
    monkeypatch.setattr(
        "pka.ingestion.progress_baselines.source_corpus_size",
        lambda _src: 10,
    )
    totals, processed, _ = get_phase_baselines(get_engine(), Source.ZOTERO)
    assert totals == {"metadata": 10, "fetching": 10, "embedding": 10}
    assert processed["metadata"] == 1


def test_get_phase_baselines_firefox_fetch_breakdown():
    _insert_doc(Source.FIREFOX, "f1", FetchStatus.FETCHED)
    _insert_doc(Source.FIREFOX, "f2", FetchStatus.UNFETCHABLE)
    _insert_doc(Source.FIREFOX, "f3", FetchStatus.PENDING)
    totals, processed, fetch_outcomes = get_phase_baselines(get_engine(), Source.FIREFOX)
    assert totals["metadata"] == 3
    assert processed["fetching"] == 2
    assert fetch_outcomes == {"success": 1, "failure": 1}


def test_get_phase_baselines_image_counts():
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            images.insert().values(
                path="/img/a.jpg",
                filename="a.jpg",
                clip_vector_id="clip-1",
                indexed_at=123,
            )
        )
        con.execute(
            images.insert().values(
                path="/img/b.jpg",
                filename="b.jpg",
            )
        )
    totals, processed, fetch_outcomes = get_phase_baselines(eng, Source.IMAGE)
    assert totals == {"metadata": 2, "fetching": 2, "embedding": 2}
    assert processed == {"metadata": 2, "fetching": 2, "embedding": 1}
    assert fetch_outcomes is None


def test_get_phase_baselines_image_counts_without_clip_vectors():
    """An image ingested with CLIP off still counts as embedded (indexed_at)."""
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            images.insert().values(
                path="/img/a.jpg", filename="a.jpg", indexed_at=123,
            )
        )
    _, processed, _ = get_phase_baselines(eng, Source.IMAGE)
    assert processed["embedding"] == 1


def test_build_ingestion_status_zotero_fetch_stats():
    _insert_doc(Source.ZOTERO, "z1", FetchStatus.AVAILABLE)
    _insert_doc(Source.ZOTERO, "z2", FetchStatus.PENDING)
    status = build_ingestion_status(get_engine())
    zstats = status["fetch_by_source"][Source.ZOTERO]
    assert zstats[str(FetchStatus.AVAILABLE)] == 1
    assert zstats[str(FetchStatus.PENDING)] == 1
    assert zstats["embedded"] == 0
    assert status["by_source"][Source.ZOTERO] == 2


def test_build_ingestion_status_image_uses_images_table():
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            images.insert().values(path="/img/a.jpg", filename="a.jpg")
        )
    status = build_ingestion_status(eng)
    assert status["by_source"][Source.IMAGE] == 1
    assert status["fetch_by_source"][Source.IMAGE] == {
        "registered": 1,
        "embedded": 0,
        "pending": 1,
    }
    assert status["total"] == 1


def test_build_ingestion_status_image_embedded_without_clip():
    """Registered vs embedded must not depend on the (opt-in) CLIP vector."""
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            images.insert().values(
                path="/img/a.jpg", filename="a.jpg", indexed_at=123,
            )
        )
    status = build_ingestion_status(eng)
    assert status["fetch_by_source"][Source.IMAGE] == {
        "registered": 1,
        "embedded": 1,
        "pending": 0,
    }


def test_build_ingestion_status_embedded_count():
    doc_id = _insert_doc(Source.CALIBRE, "c1", FetchStatus.AVAILABLE)
    with get_engine().begin() as con:
        con.execute(
            chunks.insert().values(
                document_id=doc_id,
                chunk_index=0,
                text="hello",
                vector_id="vec-1",
            )
        )
    status = build_ingestion_status(get_engine())
    assert status["fetch_by_source"][Source.CALIBRE]["embedded"] == 1
