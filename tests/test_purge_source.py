"""Tests for source purge (CLI helper + ingestion API endpoint)."""

import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from pka.cli.purge_source import purge_source
from pka.db.queries import get_engine, init_db, insert_chunks
from pka.db.schema import (
    chunks,
    documents,
    images,
    overlay_tags,
    source_tags,
)
from tests.conftest import make_document


def _seed_document(source: str, source_id: str, *, with_chunk: bool = True) -> int:
    doc_id = make_document(
        source,
        source_id,
        f"{source} doc",
        f"https://example.com/{source_id}",
        int(time.time()),
    )
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            source_tags.insert().values(
                document_id=doc_id,
                tag_string="tag",
                source=source,
            )
        )
        con.execute(
            overlay_tags.insert().values(
                document_id=doc_id,
                tag="inferred",
                origin="inferred",
                confidence=1.0,
                created_at=int(time.time()),
            )
        )
    if with_chunk:
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "body",
                    "token_count": 1,
                    "vector_id": f"vec-{source_id}",
                }
            ]
        )
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
        assert (
            con.execute(sa.select(sa.func.count()).select_from(source_tags)).scalar() == 1
        )  # zotero tag survives
        assert con.execute(sa.select(sa.func.count()).select_from(overlay_tags)).scalar() == 1


def test_purge_documents_batches_id_lists(empty_vector_store, monkeypatch):
    """Counts stay correct when the id list spans several batches.

    Unbatched, a source with more rows than SQLITE_MAX_VARIABLE_NUMBER (32766)
    fails with "too many SQL variables"; shrinking the batch size exercises the
    same seam without seeding 33k documents.
    """
    import pka.cli.purge_source as ps

    monkeypatch.setattr(ps, "_ID_BATCH_SIZE", 2)
    init_db()
    for i in range(5):
        _seed_document("firefox", f"F{i}")
    _seed_document("zotero", "Z1", with_chunk=False)

    counts = ps.purge_source("firefox")

    assert counts["documents"] == 5
    # Every batch's vectors and child rows are summed, not just the last one.
    assert counts["vectors"] == 5
    assert counts["vectors_purged"] == 5
    assert counts["source_tags"] == 5
    assert counts["overlay_tags"] == 5
    # chunks is 0 here, not 5: purge_vectors already deleted those rows by
    # vector_id before the child-table loop reached the table.
    assert counts["chunks"] == 0
    assert _remaining_sources() == ["zotero"]
    with get_engine().connect() as con:
        assert con.execute(sa.select(sa.func.count()).select_from(chunks)).scalar() == 0


def test_purge_documents_dry_run_batches_id_lists(empty_vector_store, monkeypatch):
    """The dry-run counts are summed across batches too, and delete nothing."""
    import pka.cli.purge_source as ps

    monkeypatch.setattr(ps, "_ID_BATCH_SIZE", 2)
    init_db()
    for i in range(5):
        _seed_document("firefox", f"F{i}")

    counts = ps.purge_source("firefox", dry_run=True)

    assert counts["documents"] == 5
    assert counts["source_tags"] == 5
    assert counts["overlay_tags"] == 5
    assert counts["chunks"] == 5
    assert len(_remaining_sources()) == 5


def test_purge_unknown_source_raises(empty_vector_store):
    init_db()
    with pytest.raises(ValueError):
        purge_source("nope")


def test_purge_images_clears_sidecar_and_documents(empty_vector_store, monkeypatch):
    init_db()
    doc_id = make_document(
        "image",
        "/tmp/a.jpg",
        "a.jpg",
        "/tmp/a.jpg",
        int(time.time()),
    )
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            images.insert().values(
                document_id=doc_id,
                path="/tmp/a.jpg",
                filename="a.jpg",
                image_type="photo",
                clip_vector_id="clip-1",
                indexed_at=int(time.time()),
            )
        )

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
        assert con.execute(sa.select(sa.func.count()).select_from(documents)).scalar() == 0


def test_purge_images_clears_rejection_cache(empty_vector_store, monkeypatch):
    """A purge must empty image_rejections; otherwise the metadata pass keeps
    skipping previously-rejected paths forever (the gate never runs there)."""
    init_db()
    from pka.db.queries import get_rejected_paths, record_image_rejection

    record_image_rejection("/tmp/bad.jpg", "low_text_coverage")
    monkeypatch.setattr("pka.ingestion.image_pipeline.delete_clip_vectors", lambda vids: len(vids))

    counts = purge_source("image")

    assert counts["image_rejections"] == 1
    assert get_rejected_paths() == set()


def test_purge_images_reports_rejections_dry_run(empty_vector_store):
    """Dry run reports the cache size without clearing it."""
    init_db()
    from pka.db.queries import get_rejected_paths, record_image_rejection

    record_image_rejection("/tmp/bad.jpg", "low_text_coverage")

    counts = purge_source("image", dry_run=True)

    assert counts["image_rejections"] == 1
    assert get_rejected_paths() == {"/tmp/bad.jpg"}  # untouched


def test_purge_then_reregisters_previously_rejected(empty_vector_store, monkeypatch, tmp_path):
    """End-to-end: after a purge, a metadata rerun re-registers a path that had
    been cached as rejected."""
    from pka.config import settings

    monkeypatch.setattr(settings, "image_gate_enabled", True)  # cache is consulted
    init_db()
    from pka.connectors.images import ImageFile
    from pka.db.queries import record_image_rejection
    from pka.ingestion.image_pipeline import register_images

    img = ImageFile(tmp_path / "reborn.jpg", "reborn.jpg", 10, 10, 1, 0, {})
    record_image_rejection(str(img.path), "low_text_coverage")

    # Before the purge the cache short-circuits registration.
    assert register_images([img])["skipped"] == 1

    purge_source("image")

    # After the purge the same path is registered fresh.
    stats = register_images([img])
    assert stats["processed"] == 1
    with get_engine().connect() as con:
        assert (
            con.execute(
                sa.select(sa.func.count()).select_from(images).where(images.c.path == str(img.path))
            ).scalar()
            == 1
        )


def test_reset_rejections_flag_clears_cache(empty_vector_store, monkeypatch):
    """`alexandria images --reset-rejections` empties the cache before scanning."""
    init_db()
    from pka.cli import images as images_cli
    from pka.db.queries import get_rejected_paths, record_image_rejection

    record_image_rejection("/tmp/bad.jpg", "low_text_coverage")

    # Stub the scan so the run does no real work beyond the reset.
    monkeypatch.setattr(images_cli, "scan_image_dirs", lambda folders: [])

    images_cli.main(["--reset-rejections"])

    assert get_rejected_paths() == set()


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
    from pka.ingestion import progress as sp

    _seed_document("firefox", "F1")
    sp.begin_job("firefox", "metadata", phase="loading")
    try:
        r = client.post("/ingestion/sources/firefox/purge")
        assert r.status_code == 409
    finally:
        sp.reset("firefox")
