"""Tests for source purge (CLI helper + ingestion API endpoint)."""
import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from pka.cli.purge_source import purge_source
from pka.db.queries import get_engine, init_db, insert_chunks, upsert_document
from pka.db.schema import (
    chunks,
    documents,
    images,
    overlay_tags,
    source_tags,
)


def _seed_document(source: str, source_id: str, *, with_chunk: bool = True) -> int:
    doc_id = upsert_document(
        source, source_id, f"{source} doc", f"https://example.com/{source_id}",
        int(time.time()),
    )
    eng = get_engine()
    with eng.begin() as con:
        con.execute(source_tags.insert().values(
            document_id=doc_id, tag_string="tag", source=source,
        ))
        con.execute(overlay_tags.insert().values(
            document_id=doc_id, tag="inferred", origin="inferred",
            confidence=1.0, created_at=int(time.time()),
        ))
    if with_chunk:
        insert_chunks([{
            "document_id": doc_id, "chunk_index": 0, "text": "body",
            "token_count": 1, "vector_id": f"vec-{source_id}",
        }])
    return doc_id


def _remaining_sources() -> list[str]:
    with get_engine().connect() as con:
        return [r[0] for r in con.execute(sa.select(documents.c.source)).fetchall()]


@pytest.fixture()
def client(empty_vector_store):
    init_db()
    from pka.api.main import app
    return TestClient(app, raise_server_exceptions=True)


def test_purge_source_removes_only_that_source(empty_vector_store):
    init_db()
    _seed_document("firefox", "F1")
    _seed_document("zotero", "Z1", with_chunk=False)

    counts = purge_source("firefox")

    assert counts["documents"] == 1
    # Chunk carried a vector_id, so it is removed via the Chroma vector purge.
    assert counts["vectors_purged"] == 1
    assert _remaining_sources() == ["zotero"]

    eng = get_engine()
    with eng.connect() as con:
        assert con.execute(sa.select(sa.func.count()).select_from(chunks)).scalar() == 0
        assert con.execute(
            sa.select(sa.func.count()).select_from(source_tags)
        ).scalar() == 1  # zotero tag survives
        assert con.execute(
            sa.select(sa.func.count()).select_from(overlay_tags)
        ).scalar() == 1


def test_purge_unknown_source_raises(empty_vector_store):
    init_db()
    with pytest.raises(ValueError):
        purge_source("nope")


def test_purge_images_clears_sidecar_and_documents(empty_vector_store, monkeypatch):
    init_db()
    doc_id = upsert_document(
        "image", "/tmp/a.jpg", "a.jpg", "/tmp/a.jpg", int(time.time()),
    )
    eng = get_engine()
    with eng.begin() as con:
        con.execute(images.insert().values(
            document_id=doc_id, path="/tmp/a.jpg", filename="a.jpg",
            image_type="photo", clip_vector_id="clip-1", indexed_at=int(time.time()),
        ))

    called: list[list[str]] = []
    monkeypatch.setattr(
        "pka.ingestion.image_pipeline.delete_clip_vectors",
        lambda vids: called.append(vids) or len(vids),
    )

    counts = purge_source("image")

    assert counts["images"] == 1
    assert counts["clip_vectors"] == 1
    assert called == [["clip-1"]]
    with eng.connect() as con:
        assert con.execute(sa.select(sa.func.count()).select_from(images)).scalar() == 0
        assert con.execute(
            sa.select(sa.func.count()).select_from(documents)
        ).scalar() == 0


def test_purge_endpoint(client):
    _seed_document("firefox", "F1")
    _seed_document("zotero", "Z1")

    r = client.post("/ingestion/sources/firefox/purge")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "purged"
    assert body["counts"]["documents"] == 1
    assert _remaining_sources() == ["zotero"]


def test_purge_endpoint_unknown_source(client):
    r = client.post("/ingestion/sources/bogus/purge")
    assert r.status_code == 400


def test_purge_endpoint_blocked_while_running(client):
    from pka.ingestion import sync_progress as sp

    _seed_document("firefox", "F1")
    sp.begin_job("firefox", "metadata", phase="loading")
    try:
        r = client.post("/ingestion/sources/firefox/purge")
        assert r.status_code == 409
    finally:
        sp.reset("firefox")
