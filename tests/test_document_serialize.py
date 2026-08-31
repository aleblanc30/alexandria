"""Tests for shared document row → API model serialization."""

import time
from contextlib import contextmanager

import pytest

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


@contextmanager
def file_engine_detail(doc_id: int):
    """Open a connection and yield the serialized detail for one document."""
    with get_engine().connect() as con:
        yield document_detail(con, doc_id, None)


def _seed_doc(i: int = 0) -> int:
    return upsert_document(
        "zotero",
        f"DS{i:03d}",
        f"Serialized {i}",
        f"https://example.com/{i}",
        int(time.time()),
    )


def _seed_cluster(doc_id: int) -> tuple[int, int]:
    now = int(time.time())
    with get_engine().begin() as con:
        run_id = con.execute(
            cluster_runs.insert().values(
                timestamp=now,
                algorithm="test",
                parameters="{}",
                accepted=True,
                status="finished",
            )
        ).inserted_primary_key[0]
        cluster_id = con.execute(
            clusters.insert().values(
                label="Test Cluster",
                description="",
                created_at=now,
                run_id=run_id,
                level=1,
            )
        ).inserted_primary_key[0]
        con.execute(
            cluster_assignments.insert().values(
                document_id=doc_id,
                cluster_id=cluster_id,
                run_id=run_id,
                score=0.9,
                assigned_at=now,
                level=1,
            )
        )
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
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "First chunk.",
                    "token_count": 2,
                    "vector_id": "v0",
                }
            ]
        )
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
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "Chunk body.",
                    "token_count": 2,
                    "vector_id": "v0",
                }
            ]
        )
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

    def test_structured_metadata_fields_surfaced(self):
        import json

        init_db()
        doc_id = upsert_document(
            "zotero",
            "META1",
            "A Paper",
            "https://example.com/paper",
            int(time.time()),
            doi="10.1/raft",
            arxiv_id="2301.12345",
            isbn="9780132350884",
            year=2023,
            authors_json=json.dumps(["Ada Lovelace", "Alan Turing"]),
        )
        with get_engine().connect() as con:
            out = documents_out_batch([(doc_id, None)], con, None)
        doc = out[0]
        assert doc.doi == "10.1/raft"
        assert doc.doi_url == "https://doi.org/10.1/raft"
        assert doc.arxiv_id == "2301.12345"
        assert doc.isbn == "9780132350884"
        assert doc.year == 2023
        assert doc.authors == ["Ada Lovelace", "Alan Turing"]

    def test_missing_structured_metadata_defaults(self):
        init_db()
        doc_id = _seed_doc()
        with get_engine().connect() as con:
            out = documents_out_batch([(doc_id, None)], con, None)
        doc = out[0]
        assert doc.doi is None
        assert doc.doi_url is None
        assert doc.authors == []


class TestDocumentDetail:
    def test_missing_doc_returns_none(self):
        init_db()
        with get_engine().connect() as con:
            assert document_detail(con, 99999, None) is None

    def test_detail_fields(self):
        init_db()
        doc_id = _seed_doc()
        insert_source_tags(doc_id, ["raft"], source="zotero")
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "Detail chunk.",
                    "token_count": 2,
                    "vector_id": "v0",
                }
            ]
        )
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
            "image",
            "/tmp/pic.png",
            "pic.png",
            "/tmp/pic.png",
            int(time.time()),
        )
        update_card_summary(doc_id, "A prose description of the picture.")
        with get_engine().begin() as con:
            con.execute(
                sa.text("""
                INSERT INTO images
                    (document_id, path, filename, image_type, ocr_text, indexed_at)
                VALUES (:d, '/tmp/pic.png', 'pic.png', 'slide', 'Extracted text.', 1)
            """),
                {"d": doc_id},
            )
        with get_engine().connect() as con:
            detail = document_detail(con, doc_id, None)
        assert detail.image is not None
        assert detail.image.image_type == "slide"
        assert detail.image.ocr_text == "Extracted text."
        assert detail.description == "A prose description of the picture."

    def test_detail_surfaces_structured_metadata(self):
        import json

        init_db()
        doc_id = upsert_document(
            "calibre",
            "BOOK1",
            "A Book",
            None,
            int(time.time()),
            isbn="9780132350884",
            year=2008,
            authors_json=json.dumps(["Robert C. Martin"]),
        )
        with get_engine().connect() as con:
            detail = document_detail(con, doc_id, None)
        assert detail.isbn == "9780132350884"
        assert detail.year == 2008
        assert detail.authors == ["Robert C. Martin"]
        assert detail.doi is None
        assert detail.doi_url is None


# ── Enrichment provenance (DESIGN.md §3.2) ────────────────────────────────────


class TestEnrichmentProvenance:
    """Which rung of the ladder produced a chunk, from ingest through to the API."""

    @pytest.fixture(autouse=True)
    def _db(self):
        init_db()

    def _chunk(self, doc_id: int, idx: int, **prov):
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": idx,
                    "text": f"text {idx}",
                    "token_count": 2,
                    "vector_id": f"v{doc_id}-{idx}",
                    **prov,
                }
            ]
        )

    def test_migration_is_idempotent(self):
        """init_db must stay safe to re-run — it is the documented setup step."""
        init_db()
        init_db()
        import sqlalchemy as sa

        with get_engine().connect() as con:
            cols = [r[1] for r in con.execute(sa.text("PRAGMA table_info(chunks)")).fetchall()]
        for col in ("chunk_pass", "resolved_by", "source_ref", "ref_title"):
            assert cols.count(col) == 1

    def test_ingest_text_block_persists_provenance(self, mock_chroma):
        """The provenance Chroma gets must also land in SQLite, or the API is blind."""
        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.schema import chunks as chunks_t
        from pka.ingestion.core import ingest_text_block

        doc_id = _seed_doc(90)
        ingest_text_block(
            doc_id,
            "A resolved synopsis for the book.",
            Source.IMAGE,
            extra_metadata={
                "title": "shelf.jpg",
                "pass": "external_synopsis",
                "resolved_by": "isbn",
                "isbn": "9780306406157",
                "book_title": "Dune",
            },
            min_chars=1,
        )
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(
                    chunks_t.c.chunk_pass,
                    chunks_t.c.resolved_by,
                    chunks_t.c.source_ref,
                    chunks_t.c.ref_title,
                ).where(chunks_t.c.document_id == doc_id)
            ).fetchone()
        assert row == ("external_synopsis", "isbn", "9780306406157", "Dune")

    def test_ordinary_chunks_carry_no_provenance(self, mock_chroma):
        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.schema import chunks as chunks_t
        from pka.ingestion.core import ingest_text_block

        doc_id = _seed_doc(91)
        ingest_text_block(
            doc_id, "Just an ordinary body chunk of text.", Source.FIREFOX, min_chars=1
        )
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(chunks_t.c.chunk_pass, chunks_t.c.resolved_by).where(
                    chunks_t.c.document_id == doc_id
                )
            ).fetchone()
        assert row == (None, None)

    def test_work_key_used_when_no_isbn(self, mock_chroma):
        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.schema import chunks as chunks_t
        from pka.ingestion.core import ingest_text_block

        doc_id = _seed_doc(92)
        ingest_text_block(
            doc_id,
            "Resolved by title match.",
            Source.IMAGE,
            extra_metadata={
                "pass": "external_synopsis",
                "resolved_by": "search",
                "work_key": "/works/OL1W",
                "book_title": "Dune",
            },
            min_chars=1,
        )
        with get_engine().connect() as con:
            ref = con.execute(
                sa.select(chunks_t.c.source_ref).where(chunks_t.c.document_id == doc_id)
            ).scalar()
        assert ref == "/works/OL1W"

    def test_mixed_batch_inserts(self):
        """executemany binds one statement for the batch, so keys must be uniform.

        A caller writing an enriched row and a plain one together must not have to
        know that — the helper documents the provenance fields as optional.
        """
        doc_id = _seed_doc(89)
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "synopsis",
                    "token_count": 1,
                    "vector_id": "m0",
                    "chunk_pass": "external_synopsis",
                    "resolved_by": "isbn",
                },
                {
                    "document_id": doc_id,
                    "chunk_index": 1,
                    "text": "plain body",
                    "token_count": 2,
                    "vector_id": "m1",
                },
            ]
        )
        from pka.db.queries import document_enrichment

        rows = document_enrichment([doc_id])[doc_id]
        assert [r["resolved_by"] for r in rows] == ["isbn"]

    def test_body_passes_are_not_enrichment(self):
        """Calibre's metadata/fulltext passes are ordinary body text, not provenance."""
        from pka.db.queries import document_enrichment

        doc_id = _seed_doc(93)
        self._chunk(doc_id, 0, chunk_pass="metadata")
        self._chunk(doc_id, 1, chunk_pass="fulltext")
        self._chunk(doc_id, 2, chunk_pass="summary")
        rows = document_enrichment([doc_id])[doc_id]
        assert [r["chunk_pass"] for r in rows] == ["summary"]

    def test_batched_helper_issues_one_query(self):
        """A list view must not degrade into an N+1 across documents."""
        import sqlalchemy as sa

        from pka.db.queries import document_enrichment

        ids = []
        for n in range(5):
            doc_id = _seed_doc(100 + n)
            self._chunk(doc_id, 0, chunk_pass="summary")
            ids.append(doc_id)

        seen: list[str] = []
        eng = get_engine()

        def _record(conn, cursor, statement, *args):
            if "FROM chunks" in statement:
                seen.append(statement)

        sa.event.listen(eng, "before_cursor_execute", _record)
        try:
            out = document_enrichment(ids)
        finally:
            sa.event.remove(eng, "before_cursor_execute", _record)
        assert set(out) == set(ids)
        assert len(seen) == 1, f"expected one query, got {len(seen)}"

    def test_empty_input_and_unenriched_docs(self):
        from pka.db.queries import document_enrichment

        assert document_enrichment([]) == {}
        doc_id = _seed_doc(94)
        self._chunk(doc_id, 0)
        assert document_enrichment([doc_id]) == {}

    def test_detail_reports_no_enrichment_as_empty_list(self):
        doc_id = _seed_doc(95)
        with get_engine().connect() as con:
            detail = document_detail(con, doc_id, None)
        assert detail is not None
        assert detail.enrichment == []

    def test_detail_labels_each_rung(self):
        doc_id = _seed_doc(96)
        self._chunk(
            doc_id,
            0,
            chunk_pass="external_synopsis",
            resolved_by="isbn",
            source_ref="9780306406157",
            ref_title="Dune",
        )
        self._chunk(
            doc_id, 1, chunk_pass="external_synopsis", resolved_by="brave", ref_title="Neuromancer"
        )
        with get_engine().connect() as con:
            detail = document_detail(con, doc_id, None)
        labels = [e.label for e in detail.enrichment]
        assert labels == ["Open Library · ISBN", "Brave search"]
        assert [e.ref_title for e in detail.enrichment] == ["Dune", "Neuromancer"]

    def test_summary_normalises_to_local_model(self):
        """A summary chunk stores no resolver; the API must still name the rung."""
        doc_id = _seed_doc(97)
        self._chunk(doc_id, 0, chunk_pass="summary")
        with file_engine_detail(doc_id) as detail:
            assert detail.enrichment[0].resolved_by == "local_model"
            assert detail.enrichment[0].label == "Local model"

    def test_unknown_resolver_falls_back_rather_than_crashing(self):
        doc_id = _seed_doc(98)
        self._chunk(doc_id, 0, chunk_pass="external_synopsis", resolved_by="wat")
        with file_engine_detail(doc_id) as detail:
            assert detail.enrichment[0].label == "External source"

    def test_multi_book_image_keeps_one_entry_per_book(self):
        """A shelf photo carries several synopses; each must stay identifiable."""
        doc_id = _seed_doc(99)
        for i, title in enumerate(["Dune", "Neuromancer", "Solaris"]):
            self._chunk(
                doc_id, i, chunk_pass="external_synopsis", resolved_by="search", ref_title=title
            )
        with file_engine_detail(doc_id) as detail:
            assert [e.ref_title for e in detail.enrichment] == ["Dune", "Neuromancer", "Solaris"]
