"""``/search`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

import pytest
import sqlalchemy as sa

from pka.db.queries import (
    update_card_summary,
)
from pka.db.schema import cluster_runs, clusters
from tests.api_seed import image_document_id, seed_docs, seed_image, seed_run

# ── Search ────────────────────────────────────────────────────────────────────


class TestSearch:
    def test_returns_200(self, client):
        seed_docs()
        r = client.post("/search", json={"query": "document"})
        assert r.status_code == 200

    def test_response_has_documents_key(self, client):
        seed_docs()
        r = client.post("/search", json={"query": "document"})
        assert "documents" in r.json()

    def test_fulltext_mode_finds_matching_title(self, client):
        seed_docs()
        r = client.post("/search", json={"query": "Document 0", "mode": "fulltext"})
        titles = [d["title"] for d in r.json()["documents"]]
        assert any("Document 0" in t for t in titles)

    def test_search_includes_description(self, client):
        ids = seed_docs(1)
        update_card_summary(ids[0], "Searchable card summary.")
        r = client.post("/search", json={"query": "Document 0", "mode": "fulltext"})
        docs = r.json()["documents"]
        assert len(docs) >= 1
        match = next(d for d in docs if d["id"] == ids[0])
        assert match["description"] == "Searchable card summary."

    def test_source_filter_applied(self, client):
        seed_docs()
        r = client.post(
            "/search", json={"query": "document", "mode": "fulltext", "sources": ["zotero"]}
        )
        for doc in r.json()["documents"]:
            assert doc["source"] == "zotero"

    def test_fulltext_pagination_past_first_page(self, client):
        """total counts all matches; page 2 is full and disjoint from page 1."""
        n, limit = 13, 5
        seed_docs(n)
        pages = []
        for offset in (0, 5, 10):
            r = client.post(
                "/search",
                json={
                    "query": "Document",
                    "mode": "fulltext",
                    "limit": limit,
                    "offset": offset,
                },
            )
            body = r.json()
            assert body["total"] == n
            pages.append([d["id"] for d in body["documents"]])
        assert len(pages[0]) == limit
        assert len(pages[1]) == limit
        assert len(pages[2]) == n - 2 * limit
        all_ids = [i for page in pages for i in page]
        assert len(all_ids) == len(set(all_ids)) == n

    def test_empty_query_returns_200(self, client):
        r = client.post("/search", json={"query": ""})
        assert r.status_code == 200

    def test_semantic_mode_returns_similarity(self, client, monkeypatch):
        ids = seed_docs(1)
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda emb, n_results=10, where=None: [
                {
                    "vector_id": "v1",
                    "text": "matching chunk",
                    "distance": 0.25,
                    "metadata": {"document_id": ids[0], "source": "zotero"},
                }
            ],
        )
        r = client.post("/search", json={"query": "raft", "mode": "semantic"})
        docs = r.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["similarity"] == pytest.approx(0.75)

    def test_hybrid_mode_merges_semantic_and_fulltext(self, client, monkeypatch):
        ids = seed_docs(2)
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda emb, n_results=10, where=None: [
                {
                    "vector_id": "v1",
                    "text": "chunk",
                    "distance": 0.1,
                    "metadata": {"document_id": ids[0], "source": "zotero"},
                }
            ],
        )
        r = client.post("/search", json={"query": "Document 1", "mode": "hybrid"})
        returned_ids = {d["id"] for d in r.json()["documents"]}
        assert ids[0] in returned_ids
        assert ids[1] in returned_ids

    def test_fetch_status_filter(self, client, monkeypatch):
        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_tbl

        ids = seed_docs(2)
        with get_engine().begin() as con:
            con.execute(
                docs_tbl.update().where(docs_tbl.c.id == ids[0]).values(fetch_status="pending")
            )
            con.execute(
                docs_tbl.update().where(docs_tbl.c.id == ids[1]).values(fetch_status="fetched")
            )
        monkeypatch.setattr("pka.storage.vector_store.query", lambda *a, **kw: [])
        r = client.post(
            "/search",
            json={
                "query": "Document",
                "mode": "fulltext",
                "fetch_status": "pending",
            },
        )
        assert all(d["fetch_status"] == "pending" for d in r.json()["documents"])

    def test_clip_matches_merged_into_documents(self, client, monkeypatch):
        """CLIP visual hits surface image documents in the unified result list."""
        image_id = seed_image(client)
        doc_id = image_document_id(image_id)
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_text",
            lambda q, n=10: [
                {
                    "vector_id": "clip-1",
                    "document_id": doc_id,
                    "filename": "slide.png",
                    "path": "/tmp/slide.png",
                    "image_type": "slide",
                    "distance": 0.2,
                }
            ],
        )
        r = client.post("/search", json={"query": "unrelated visual query"})
        body = r.json()
        assert "images" not in body
        match = next(d for d in body["documents"] if d["id"] == doc_id)
        assert match["source"] == "image"
        assert match["similarity"] == pytest.approx(0.8)

    def test_clip_matches_excluded_by_source_filter(self, client, monkeypatch):
        """Filtering to non-image sources must not pull in CLIP image hits."""
        image_id = seed_image(client)
        doc_id = image_document_id(image_id)
        called = {"n": 0}

        def _fake(q, n=10):
            called["n"] += 1
            return [{"vector_id": "clip-1", "document_id": doc_id, "distance": 0.1}]

        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_text",
            _fake,
        )
        r = client.post("/search", json={"query": "neural", "sources": ["zotero"]})
        ids = [d["id"] for d in r.json()["documents"]]
        assert doc_id not in ids
        assert called["n"] == 0  # image search skipped entirely when out of scope

    def test_cluster_id_filter(self, client, monkeypatch):
        ids = seed_docs(4)
        run_id = seed_run(ids, n_clusters=2)
        from pka.db.queries import get_engine

        with get_engine().connect() as con:
            cid = con.execute(
                sa.select(clusters.c.cluster_id).where(clusters.c.run_id == run_id)
            ).fetchone()[0]
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update().where(cluster_runs.c.run_id == run_id).values(accepted=True)
            )
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda *a, **kw: [
                {
                    "vector_id": "v1",
                    "text": "c",
                    "distance": 0.1,
                    "metadata": {"document_id": ids[0], "source": "zotero"},
                },
            ],
        )
        r = client.post(
            "/search",
            json={
                "query": "Document",
                "mode": "semantic",
                "cluster_ids": [cid],
            },
        )
        assert all(d["cluster_id"] == cid for d in r.json()["documents"])

    def test_semantic_query_failure_falls_back_to_fulltext(self, client, monkeypatch):
        seed_docs(2)
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("chroma down")),
        )
        r = client.post("/search", json={"query": "Document 0", "mode": "hybrid"})
        assert r.status_code == 200
        assert len(r.json()["documents"]) >= 1

    def test_search_source_tags_filter(self, client):
        from pka.db.queries import insert_source_tags

        ids = seed_docs(3)
        insert_source_tags(ids[0], ["ml", "python"], source="zotero")
        insert_source_tags(ids[1], ["ml"], source="firefox")
        insert_source_tags(ids[2], ["python"], source="calibre")
        r = client.post(
            "/search",
            json={
                "query": "Document",
                "mode": "fulltext",
                "source_tags": ["ml", "python"],
            },
        )
        assert r.status_code == 200
        returned_ids = {d["id"] for d in r.json()["documents"]}
        assert returned_ids == {ids[0]}

    def test_search_general_tags_filter(self, client):
        from pka.classification import sync_classification_tags

        ids = seed_docs(3)
        sync_classification_tags(ids[0], ["academic", "paper"])
        sync_classification_tags(ids[1], ["academic", "preprint"])
        r = client.post(
            "/search",
            json={
                "query": "Document",
                "mode": "fulltext",
                "general_tags": ["preprint"],
            },
        )
        assert r.status_code == 200
        returned_ids = {d["id"] for d in r.json()["documents"]}
        assert returned_ids == {ids[1]}

    def test_search_wayback_only_filter(self, client):

        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table

        ids = seed_docs(3)
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/1"
        with get_engine().begin() as con:
            con.execute(
                docs_table.update().where(docs_table.c.id == ids[1]).values(archive_url=snapshot)
            )
        r = client.post(
            "/search",
            json={
                "query": "Document",
                "mode": "fulltext",
                "wayback_only": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["documents"][0]["id"] == ids[1]
        assert data["documents"][0]["archive_url"] == snapshot
