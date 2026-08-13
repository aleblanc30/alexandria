"""Tests for shared document row → API model serialization."""
import time

from pka.api.document_serialize import document_detail, documents_out_batch
from pka.constants import TagOrigin
from pka.db.queries import (
    get_engine,
    init_db,
    insert_chunks,
    insert_source_tags,
    update_card_summary,
    upsert_document,
)
from pka.db.schema import cluster_assignments, cluster_runs, clusters


def _seed_doc(i: int = 0) -> int:
    return upsert_document(
        "zotero", f"DS{i:03d}", f"Serialized {i}",
        f"https://example.com/{i}", int(time.time()),
    )


def _seed_cluster(doc_id: int) -> tuple[int, int]:
    now = int(time.time())
    with get_engine().begin() as con:
        run_id = con.execute(cluster_runs.insert().values(
            timestamp=now, algorithm="test", parameters="{}",
            accepted=True, status="finished",
        )).inserted_primary_key[0]
        cluster_id = con.execute(clusters.insert().values(
            label="Test Cluster", description="", created_at=now,
            run_id=run_id, level=1,
        )).inserted_primary_key[0]
        con.execute(cluster_assignments.insert().values(
            document_id=doc_id, cluster_id=cluster_id, run_id=run_id,
            score=0.9, assigned_at=now, level=1,
        ))
    return run_id, cluster_id


class TestDocumentsOutBatch:
    def test_empty_input(self):
        init_db()
        with get_engine().connect() as con:
            assert documents_out_batch([], con, None) == []

    def test_fields_and_similarity(self):
        init_db()
        doc_id = _seed_doc()
        insert_source_tags(doc_id, ["consensus"], source="zotero")
        insert_chunks([{
            "document_id": doc_id, "chunk_index": 0,
            "text": "First chunk.", "token_count": 2, "vector_id": "v0",
        }])
        run_id, cluster_id = _seed_cluster(doc_id)
        with get_engine().connect() as con:
            out = documents_out_batch([(doc_id, 0.83)], con, run_id)
        assert len(out) == 1
        doc = out[0]
        assert doc.id == doc_id
        assert doc.similarity == 0.83
        assert doc.source_tags == ["consensus"]
        assert doc.cluster_id == cluster_id
        assert doc.cluster_label == "Test Cluster"
        assert doc.description == "First chunk."

    def test_card_summary_preferred_over_chunk(self):
        init_db()
        doc_id = _seed_doc()
        insert_chunks([{
            "document_id": doc_id, "chunk_index": 0,
            "text": "Chunk body.", "token_count": 2, "vector_id": "v0",
        }])
        update_card_summary(doc_id, "The abstract.")
        with get_engine().connect() as con:
            out = documents_out_batch([(doc_id, None)], con, None)
        assert out[0].description == "The abstract."

    def test_missing_doc_id_skipped(self):
        init_db()
        doc_id = _seed_doc()
        with get_engine().connect() as con:
            out = documents_out_batch([(doc_id, None), (99999, None)], con, None)
        assert [d.id for d in out] == [doc_id]

    def test_preserves_input_order(self):
        init_db()
        ids = [_seed_doc(i) for i in range(3)]
        ordered = [(ids[2], 0.9), (ids[0], 0.5), (ids[1], None)]
        with get_engine().connect() as con:
            out = documents_out_batch(ordered, con, None)
        assert [d.id for d in out] == [ids[2], ids[0], ids[1]]


class TestDocumentDetail:
    def test_missing_doc_returns_none(self):
        init_db()
        with get_engine().connect() as con:
            assert document_detail(con, 99999, None) is None

    def test_detail_fields(self):
        init_db()
        doc_id = _seed_doc()
        insert_source_tags(doc_id, ["raft"], source="zotero")
        insert_chunks([{
            "document_id": doc_id, "chunk_index": 0,
            "text": "Detail chunk.", "token_count": 2, "vector_id": "v0",
        }])
        from pka.clustering.cluster_tags import insert_overlay_tags
        with get_engine().begin() as con:
            insert_overlay_tags(con, [doc_id], "manual-x", TagOrigin.MANUAL)
        run_id, cluster_id = _seed_cluster(doc_id)
        with get_engine().connect() as con:
            detail = document_detail(con, doc_id, run_id)
        assert detail is not None
        assert detail.source_tags == ["raft"]
        assert [t.tag for t in detail.overlay_tags] == ["manual-x"]
        assert detail.cluster_id == cluster_id
        assert detail.chunks_count == 1
        assert detail.description == "Detail chunk."

    def test_non_image_detail_has_no_image_block(self):
        init_db()
        doc_id = _seed_doc()
        with get_engine().connect() as con:
            detail = document_detail(con, doc_id, None)
        assert detail.image is None

    def test_image_detail_carries_type_and_ocr(self):
        import sqlalchemy as sa

        init_db()
        doc_id = upsert_document(
            "image", "/tmp/pic.png", "pic.png",
            "/tmp/pic.png", int(time.time()),
        )
        update_card_summary(doc_id, "A prose description of the picture.")
        with get_engine().begin() as con:
            con.execute(sa.text("""
                INSERT INTO images
                    (document_id, path, filename, image_type, ocr_text, indexed_at)
                VALUES (:d, '/tmp/pic.png', 'pic.png', 'slide', 'Extracted text.', 1)
            """), {"d": doc_id})
        with get_engine().connect() as con:
            detail = document_detail(con, doc_id, None)
        assert detail.image is not None
        assert detail.image.image_type == "slide"
        assert detail.image.ocr_text == "Extracted text."
        assert detail.description == "A prose description of the picture."
