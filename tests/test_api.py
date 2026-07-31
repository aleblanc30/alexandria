"""
FastAPI endpoint tests.
Uses TestClient (synchronous httpx wrapper) — no running server needed.
All storage and embedding calls are mocked; DB is real SQLite in tmp_path.
"""
import time
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from pka.db.queries import init_db, insert_chunks, update_card_summary, upsert_document
from pka.db.schema import cluster_assignments, cluster_runs, clusters

# ── App fixture ───────────────────────────────────────────────────────────────

@pytest.fixture()
def client(empty_vector_store):
    """Return a TestClient with a fresh DB and mocked (empty) vector store."""
    init_db()
    from pka.api.main import app
    return TestClient(app, raise_server_exceptions=True)


def _seed_docs(n: int = 3) -> list[int]:
    ids = []
    for i in range(n):
        src = ["zotero", "firefox", "calibre"][i % 3]
        ids.append(upsert_document(
            src, f"K{i:03d}", f"Document {i}",
            f"https://example.com/{i}", int(time.time()) - i * 86400
        ))
    return ids


def _seed_run(doc_ids: list[int], n_clusters: int = 2, *, with_l2: bool = False) -> int:
    from pka.db.queries import get_engine
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        run_res = con.execute(
            cluster_runs.insert().values(
                timestamp=now, algorithm="HDBSCAN-hierarchical",
                parameters="{}", accepted=True, status="finished",
            )
        )
        run_id = run_res.inserted_primary_key[0]
        l1_ids: list[int] = []
        for i in range(n_clusters):
            cl_res = con.execute(
                clusters.insert().values(
                    label=f"Cluster {i}", description="",
                    created_at=now, run_id=run_id,
                    level=1, parent_cluster_id=None,
                )
            )
            cid = cl_res.inserted_primary_key[0]
            l1_ids.append(cid)
            for did in doc_ids[i::n_clusters]:
                con.execute(cluster_assignments.insert().values(
                    document_id=did, cluster_id=cid,
                    run_id=run_id, score=0.9, assigned_at=now,
                    level=1,
                ))
        if with_l2 and l1_ids:
            parent = l1_ids[0]
            parent_docs = doc_ids[0::n_clusters]
            for sub_idx in range(2):
                l2_res = con.execute(
                    clusters.insert().values(
                        label=f"Subcluster {sub_idx}", description="",
                        created_at=now, run_id=run_id,
                        level=2, parent_cluster_id=parent,
                    )
                )
                l2_id = l2_res.inserted_primary_key[0]
                for did in parent_docs[sub_idx::2]:
                    con.execute(cluster_assignments.insert().values(
                        document_id=did, cluster_id=l2_id,
                        run_id=run_id, score=0.8, assigned_at=now,
                        level=2,
                    ))
    return run_id


def _seed_image(client=None) -> int:
    """Insert a test image row and return its id."""
    from pka.db.queries import get_engine
    from pka.db.schema import image_tags
    from pka.db.schema import images as images_tbl

    now = int(time.time())
    with get_engine().begin() as con:
        res = con.execute(images_tbl.insert().values(
            path="/tmp/slide.png",
            filename="slide.png",
            image_type="slide",
            width=800,
            height=600,
            file_size=12345,
            date_taken=now,
            ocr_text="Neural networks overview",
            description="A slide about neural networks",
            clip_vector_id="clip-1",
            text_vector_id="text-1",
            indexed_at=now,
        ))
        image_id = res.inserted_primary_key[0]
        con.execute(image_tags.insert().values(
            image_id=image_id, tag="ml", origin="auto",
        ))
    return image_id


# ── Search ────────────────────────────────────────────────────────────────────

class TestSearch:
    def test_returns_200(self, client):
        _seed_docs()
        r = client.post("/search", json={"query": "document"})
        assert r.status_code == 200

    def test_response_has_documents_key(self, client):
        _seed_docs()
        r = client.post("/search", json={"query": "document"})
        assert "documents" in r.json()

    def test_fulltext_mode_finds_matching_title(self, client):
        _seed_docs()
        r = client.post("/search", json={"query": "Document 0", "mode": "fulltext"})
        titles = [d["title"] for d in r.json()["documents"]]
        assert any("Document 0" in t for t in titles)

    def test_search_includes_description(self, client):
        ids = _seed_docs(1)
        update_card_summary(ids[0], "Searchable card summary.")
        r = client.post("/search", json={"query": "Document 0", "mode": "fulltext"})
        docs = r.json()["documents"]
        assert len(docs) >= 1
        match = next(d for d in docs if d["id"] == ids[0])
        assert match["description"] == "Searchable card summary."

    def test_source_filter_applied(self, client):
        _seed_docs()
        r = client.post("/search", json={"query": "document", "mode": "fulltext",
                                          "sources": ["zotero"]})
        for doc in r.json()["documents"]:
            assert doc["source"] == "zotero"

    def test_fulltext_pagination_past_first_page(self, client):
        """total counts all matches; page 2 is full and disjoint from page 1."""
        n, limit = 13, 5
        _seed_docs(n)
        pages = []
        for offset in (0, 5, 10):
            r = client.post("/search", json={
                "query": "Document", "mode": "fulltext",
                "limit": limit, "offset": offset,
            })
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
        ids = _seed_docs(1)
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda emb, n_results=10, where=None: [{
                "vector_id": "v1",
                "text": "matching chunk",
                "distance": 0.25,
                "metadata": {"document_id": ids[0], "source": "zotero"},
            }],
        )
        r = client.post("/search", json={"query": "raft", "mode": "semantic"})
        docs = r.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["similarity"] == pytest.approx(0.75)

    def test_hybrid_mode_merges_semantic_and_fulltext(self, client, monkeypatch):
        ids = _seed_docs(2)
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda emb, n_results=10, where=None: [{
                "vector_id": "v1",
                "text": "chunk",
                "distance": 0.1,
                "metadata": {"document_id": ids[0], "source": "zotero"},
            }],
        )
        r = client.post("/search", json={"query": "Document 1", "mode": "hybrid"})
        returned_ids = {d["id"] for d in r.json()["documents"]}
        assert ids[0] in returned_ids
        assert ids[1] in returned_ids

    def test_fetch_status_filter(self, client, monkeypatch):
        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_tbl

        ids = _seed_docs(2)
        with get_engine().begin() as con:
            con.execute(
                docs_tbl.update().where(docs_tbl.c.id == ids[0]).values(fetch_status="pending")
            )
            con.execute(
                docs_tbl.update().where(docs_tbl.c.id == ids[1]).values(fetch_status="fetched")
            )
        monkeypatch.setattr("pka.storage.vector_store.query", lambda *a, **kw: [])
        r = client.post("/search", json={
            "query": "Document",
            "mode": "fulltext",
            "fetch_status": "pending",
        })
        assert all(d["fetch_status"] == "pending" for d in r.json()["documents"])

    def test_include_images(self, client, monkeypatch):
        _seed_image(client)
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_text",
            lambda q, n=10: [{
                "vector_id": "clip-1",
                "filename": "slide.png",
                "path": "/tmp/slide.png",
                "image_type": "slide",
                "distance": 0.2,
            }],
        )
        r = client.post("/search", json={"query": "neural", "include_images": True})
        images = r.json()["images"]
        assert len(images) == 1
        assert images[0]["filename"] == "slide.png"

    def test_cluster_id_filter(self, client, monkeypatch):
        ids = _seed_docs(4)
        run_id = _seed_run(ids, n_clusters=2)
        from pka.db.queries import get_engine
        from pka.db.schema import cluster_runs, clusters

        with get_engine().connect() as con:
            cid = con.execute(
                sa.select(clusters.c.cluster_id).where(clusters.c.run_id == run_id)
            ).fetchone()[0]
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update()
                .where(cluster_runs.c.run_id == run_id)
                .values(accepted=True)
            )
        monkeypatch.setattr("pka.storage.vector_store.query", lambda *a, **kw: [
            {"vector_id": "v1", "text": "c", "distance": 0.1,
             "metadata": {"document_id": ids[0], "source": "zotero"}},
        ])
        r = client.post("/search", json={
            "query": "Document",
            "mode": "semantic",
            "cluster_ids": [cid],
        })
        assert all(d["cluster_id"] == cid for d in r.json()["documents"])

    def test_semantic_query_failure_falls_back_to_fulltext(self, client, monkeypatch):
        _seed_docs(2)
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("chroma down")),
        )
        r = client.post("/search", json={"query": "Document 0", "mode": "hybrid"})
        assert r.status_code == 200
        assert len(r.json()["documents"]) >= 1

    def test_search_source_tags_filter(self, client):
        from pka.db.queries import insert_source_tags

        ids = _seed_docs(3)
        insert_source_tags(ids[0], ["ml", "python"], source="zotero")
        insert_source_tags(ids[1], ["ml"], source="firefox")
        insert_source_tags(ids[2], ["python"], source="calibre")
        r = client.post("/search", json={
            "query": "Document",
            "mode": "fulltext",
            "source_tags": ["ml", "python"],
        })
        assert r.status_code == 200
        returned_ids = {d["id"] for d in r.json()["documents"]}
        assert returned_ids == {ids[0]}

    def test_search_general_tags_filter(self, client):
        from pka.classification import sync_classification_tags

        ids = _seed_docs(3)
        sync_classification_tags(ids[0], ["academic", "paper"])
        sync_classification_tags(ids[1], ["academic", "preprint"])
        r = client.post("/search", json={
            "query": "Document",
            "mode": "fulltext",
            "general_tags": ["preprint"],
        })
        assert r.status_code == 200
        returned_ids = {d["id"] for d in r.json()["documents"]}
        assert returned_ids == {ids[1]}

    def test_search_wayback_only_filter(self, client):

        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table

        ids = _seed_docs(3)
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/1"
        with get_engine().begin() as con:
            con.execute(
                docs_table.update()
                .where(docs_table.c.id == ids[1])
                .values(archive_url=snapshot)
            )
        r = client.post("/search", json={
            "query": "Document",
            "mode": "fulltext",
            "wayback_only": True,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["documents"][0]["id"] == ids[1]
        assert data["documents"][0]["archive_url"] == snapshot


# ── Documents ─────────────────────────────────────────────────────────────────

class TestDocuments:
    def test_list_documents_200(self, client):
        _seed_docs(3)
        r = client.get("/documents")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["documents"]) == 3

    def test_list_documents_fields(self, client):
        ids = _seed_docs(1)
        insert_chunks([{
            "document_id": ids[0], "chunk_index": 0,
            "text": "First chunk body text.", "token_count": 4, "vector_id": "v0",
        }])
        doc = client.get("/documents").json()["documents"][0]
        for key in (
            "id", "source", "source_id", "title", "description", "url_or_path",
            "archive_url", "zotero_attachment_key",
            "source_tags", "cluster_l1_tags", "cluster_l2_tags",
        ):
            assert key in doc
        assert doc["description"] == "First chunk body text."
        assert doc["source_tags"] == []
        assert doc["cluster_l1_tags"] == []
        assert doc["cluster_l2_tags"] == []

    def test_list_documents_snippet_truncation(self, client):
        from pka.card_summary import SUMMARY_MAX_LEN

        ids = _seed_docs(1)
        long_text = "word " * 70
        insert_chunks([{
            "document_id": ids[0], "chunk_index": 0,
            "text": long_text, "token_count": 70, "vector_id": "v0",
        }])
        doc = client.get("/documents").json()["documents"][0]
        assert len(doc["description"]) <= SUMMARY_MAX_LEN + 1  # +1 for the ellipsis
        assert doc["description"].endswith("…")

    def test_list_documents_prefers_card_summary(self, client):
        ids = _seed_docs(1)
        insert_chunks([{
            "document_id": ids[0], "chunk_index": 0,
            "text": "Test Paper by Alice", "token_count": 4, "vector_id": "v0",
        }])
        update_card_summary(ids[0], "This is the abstract for the paper.")
        doc = client.get("/documents").json()["documents"][0]
        assert doc["description"] == "This is the abstract for the paper."

    def test_list_documents_uses_first_chunk(self, client):
        ids = _seed_docs(1)
        insert_chunks([
            {"document_id": ids[0], "chunk_index": 1, "text": "Second", "token_count": 1, "vector_id": "v1"},
            {"document_id": ids[0], "chunk_index": 0, "text": "First", "token_count": 1, "vector_id": "v0"},
        ])
        doc = client.get("/documents").json()["documents"][0]
        assert doc["description"] == "First"

    def test_list_documents_source_filter(self, client):
        _seed_docs(3)
        r = client.get("/documents?sources=zotero")
        docs = r.json()["documents"]
        assert all(d["source"] == "zotero" for d in docs)

    def test_list_documents_wayback_only_filter(self, client):

        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table

        ids = _seed_docs(3)
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/1"
        with get_engine().begin() as con:
            con.execute(
                docs_table.update()
                .where(docs_table.c.id == ids[1])
                .values(archive_url=snapshot)
            )

        r = client.get("/documents?wayback_only=true")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert len(data["documents"]) == 1
        assert data["documents"][0]["source"] == "firefox"
        assert data["documents"][0]["archive_url"] == snapshot

    def test_list_documents_pagination(self, client):
        _seed_docs(5)
        page1 = client.get("/documents?limit=2&offset=0").json()
        page2 = client.get("/documents?limit=2&offset=2").json()
        assert page1["total"] == 5
        assert len(page1["documents"]) == 2
        assert len(page2["documents"]) == 2
        ids1 = {d["id"] for d in page1["documents"]}
        ids2 = {d["id"] for d in page2["documents"]}
        assert ids1.isdisjoint(ids2)

    def test_list_documents_source_tags_and_filter(self, client):
        from pka.db.queries import insert_source_tags

        ids = _seed_docs(3)
        insert_source_tags(ids[0], ["ml", "python"], source="zotero")
        insert_source_tags(ids[1], ["ml"], source="firefox")
        insert_source_tags(ids[2], ["python"], source="calibre")

        r = client.get("/documents", params=[("source_tags", "ml"), ("source_tags", "python")])
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["documents"][0]["id"] == ids[0]

    def test_list_documents_overlay_tags_and_source_filter(self, client):
        ids = _seed_docs(3)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["review"], "remove": []})
        client.patch(f"/documents/{ids[1]}/tags", json={"add": ["review"], "remove": []})

        r = client.get(
            "/documents",
            params=[("sources", "zotero"), ("overlay_tags", "review")],
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["documents"][0]["id"] == ids[0]

    def test_list_documents_general_tags_filter(self, client):
        from pka.classification import sync_classification_tags

        ids = _seed_docs(3)
        sync_classification_tags(ids[0], ["academic", "paper"])
        sync_classification_tags(ids[1], ["academic", "preprint"])
        sync_classification_tags(ids[2], [])

        r_all = client.get("/documents", params=[("general_tags", "academic")])
        assert r_all.status_code == 200
        assert r_all.json()["total"] == 2
        assert {d["id"] for d in r_all.json()["documents"]} == {ids[0], ids[1]}

        r_paper = client.get("/documents", params=[("general_tags", "paper")])
        assert r_paper.json()["total"] == 1
        assert r_paper.json()["documents"][0]["id"] == ids[0]

        r_preprint = client.get("/documents", params=[("general_tags", "preprint")])
        assert r_preprint.json()["total"] == 1
        assert r_preprint.json()["documents"][0]["id"] == ids[1]

    def test_list_documents_cluster_l1_l2_tag_filters(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2, with_l2=True)
        l1 = next(c for c in client.get("/clusters").json() if c["level"] == 1)
        l2 = next(c for c in client.get("/clusters").json() if c["level"] == 2)
        l1_tag = client.post(
            f"/clusters/{l1['cluster_id']}/apply-tag", json={"tag": "topic-l1"},
        ).json()["tag"]
        l2_tag = client.post(
            f"/clusters/{l2['cluster_id']}/apply-tag", json={"tag": "topic-l2"},
        ).json()["tag"]

        r1 = client.get("/documents", params=[("cluster_l1_tags", l1_tag)])
        assert r1.status_code == 200
        assert r1.json()["total"] >= 1

        r2 = client.get(
            "/documents",
            params=[("cluster_l1_tags", l1_tag), ("cluster_l2_tags", l2_tag)],
        )
        assert r2.status_code == 200
        assert r2.json()["total"] >= 1

    def test_get_document_200(self, client):
        ids = _seed_docs(1)
        r = client.get(f"/documents/{ids[0]}")
        assert r.status_code == 200

    def test_get_document_fields(self, client):
        ids = _seed_docs(1)
        data = client.get(f"/documents/{ids[0]}").json()
        for key in ("id", "title", "source", "source_tags", "overlay_tags", "chunks_count"):
            assert key in data

    def test_get_document_description(self, client):
        ids = _seed_docs(1)
        insert_chunks([{
            "document_id": ids[0], "chunk_index": 0,
            "text": "First chunk body text.", "token_count": 4, "vector_id": "v0",
        }])
        data = client.get(f"/documents/{ids[0]}").json()
        assert data["description"] == "First chunk body text."

    def test_get_document_prefers_card_summary(self, client):
        ids = _seed_docs(1)
        insert_chunks([{
            "document_id": ids[0], "chunk_index": 0,
            "text": "Title chunk only", "token_count": 3, "vector_id": "v0",
        }])
        update_card_summary(ids[0], "Stored card summary.")
        data = client.get(f"/documents/{ids[0]}").json()
        assert data["description"] == "Stored card summary."

    def test_get_document_with_cluster(self, client):
        ids = _seed_docs(4)
        run_id = _seed_run(ids, n_clusters=2)
        from pka.db.queries import get_engine
        from pka.db.schema import cluster_runs
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update()
                .where(cluster_runs.c.run_id == run_id)
                .values(accepted=True)
            )
        data = client.get(f"/documents/{ids[0]}").json()
        assert data["cluster_id"] is not None
        assert data["cluster_label"] is not None

    def test_get_document_404(self, client):
        assert client.get("/documents/99999").status_code == 404

    def test_patch_tags_add(self, client):
        ids = _seed_docs(1)
        r = client.patch(f"/documents/{ids[0]}/tags",
                         json={"add": ["my-tag"], "remove": []})
        assert r.status_code == 200
        data = client.get(f"/documents/{ids[0]}").json()
        overlay = [t["tag"] for t in data["overlay_tags"]]
        assert "my-tag" in overlay

    def test_patch_tags_remove(self, client):
        ids = _seed_docs(1)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["bye"], "remove": []})
        client.patch(f"/documents/{ids[0]}/tags", json={"add": [], "remove": ["bye"]})
        data = client.get(f"/documents/{ids[0]}").json()
        overlay = [t["tag"] for t in data["overlay_tags"]]
        assert "bye" not in overlay

    def test_patch_tags_add_is_idempotent(self, client):
        """Adding the same manual tag twice must not create duplicate rows."""
        ids = _seed_docs(1)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["twice"], "remove": []})
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["twice"], "remove": []})
        data = client.get(f"/documents/{ids[0]}").json()
        overlay = [t["tag"] for t in data["overlay_tags"]]
        assert overlay.count("twice") == 1


class TestDocumentCover:
    def test_cover_served_when_file_exists(self, client, tmp_path):
        book_dir = tmp_path / "Author" / "Title (1)"
        book_dir.mkdir(parents=True)
        (book_dir / "book.epub").write_bytes(b"epub")
        (book_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

        doc_id = upsert_document(
            "calibre", "1", "Title", str(book_dir / "book.epub"), int(time.time()),
        )
        r = client.get(f"/documents/{doc_id}/cover")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == b"\xff\xd8\xff\xe0fakejpeg"

    def test_404_when_no_cover_file(self, client, tmp_path):
        book_dir = tmp_path / "Author" / "Title (1)"
        book_dir.mkdir(parents=True)
        (book_dir / "book.epub").write_bytes(b"epub")

        doc_id = upsert_document(
            "calibre", "1", "Title", str(book_dir / "book.epub"), int(time.time()),
        )
        r = client.get(f"/documents/{doc_id}/cover")
        assert r.status_code == 404

    def test_404_for_non_calibre_source(self, client):
        ids = _seed_docs(3)
        firefox_id = next(
            i for i in ids
            if client.get(f"/documents/{i}").json()["source"] == "firefox"
        )
        r = client.get(f"/documents/{firefox_id}/cover")
        assert r.status_code == 404

    def test_404_for_unknown_document(self, client):
        r = client.get("/documents/999999/cover")
        assert r.status_code == 404


# ── Tags ──────────────────────────────────────────────────────────────────────

class TestTags:
    def test_returns_list(self, client):
        r = client.get("/tags")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_filter_by_origin(self, client):
        ids = _seed_docs(1)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["manual-tag"], "remove": []})
        r = client.get("/tags?origin=manual")
        tags = [t["tag"] for t in r.json()]
        assert "manual-tag" in tags

    def test_query_filter(self, client):
        ids = _seed_docs(1)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["unique-xyz"], "remove": []})
        r = client.get("/tags?q=unique-xyz")
        assert any("unique-xyz" in t["tag"] for t in r.json())

    def test_filter_by_document_source(self, client):
        from pka.db.queries import insert_source_tags

        ids = _seed_docs(3)
        insert_source_tags(ids[0], ["zotero-only"], source="zotero")
        insert_source_tags(ids[1], ["firefox-only"], source="firefox")

        r = client.get("/tags", params=[("sources", "firefox"), ("origin", "source")])
        tags = [t["tag"] for t in r.json()]
        assert "firefox-only" in tags
        assert "zotero-only" not in tags

    def test_filter_by_selected_tags_shows_cooccurring_only(self, client):
        from pka.db.queries import insert_source_tags

        ids = _seed_docs(3)
        insert_source_tags(ids[0], ["ml", "python"], source="zotero")
        insert_source_tags(ids[1], ["ml"], source="firefox")
        insert_source_tags(ids[2], ["python"], source="calibre")

        all_source = [t["tag"] for t in client.get("/tags?origin=source").json()]
        assert set(all_source) >= {"ml", "python"}

        scoped = client.get("/tags", params=[("origin", "source"), ("source_tags", "python")])
        scoped_tags = [t["tag"] for t in scoped.json()]
        assert "python" in scoped_tags
        assert "ml" in scoped_tags
        assert len(scoped_tags) == 2

        ml_only = client.get("/tags", params=[("origin", "source"), ("source_tags", "ml")])
        ml_tags = [t["tag"] for t in ml_only.json()]
        assert set(ml_tags) == {"ml", "python"}

        restored = [t["tag"] for t in client.get("/tags?origin=source").json()]
        assert set(restored) >= {"ml", "python"}

    def test_filter_by_cluster_l1_l2_origin(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2, with_l2=True)
        l1 = next(c for c in client.get("/clusters").json() if c["level"] == 1)
        l2 = next(c for c in client.get("/clusters").json() if c["level"] == 2)
        l1_tag = client.post(
            f"/clusters/{l1['cluster_id']}/apply-tag", json={"tag": "topic-l1"},
        ).json()["tag"]
        l2_tag = client.post(
            f"/clusters/{l2['cluster_id']}/apply-tag", json={"tag": "topic-l2"},
        ).json()["tag"]

        l1_tags = [t["tag"] for t in client.get("/tags?origin=cluster_l1").json()]
        l2_tags = [t["tag"] for t in client.get("/tags?origin=cluster_l2").json()]
        assert l1_tag in l1_tags
        assert l2_tag in l2_tags
        assert l2_tag not in l1_tags


# ── Clusters ──────────────────────────────────────────────────────────────────

class TestClusters:
    def test_returns_empty_without_active_run(self, client):
        r = client.get("/clusters")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_clusters_with_active_run(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2)
        r = client.get("/clusters")
        data = r.json()
        assert len(data) == 2
        for key in ("cluster_id", "label", "level", "doc_count"):
            assert key in data[0]

    def test_cluster_detail_200(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2)
        clusters_list = client.get("/clusters").json()
        cid = clusters_list[0]["cluster_id"]
        r = client.get(f"/clusters/{cid}")
        assert r.status_code == 200
        data = r.json()
        assert "top_tags" in data
        assert "label" in data

    def test_patch_cluster_label(self, client):
        ids = _seed_docs(2)
        _seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.patch(f"/clusters/{cid}", json={"label": "Custom Topic Name"})
        assert r.status_code == 200
        assert r.json()["label"] == "Custom Topic Name"
        listed = client.get("/clusters").json()
        assert next(c for c in listed if c["cluster_id"] == cid)["label"] == "Custom Topic Name"

    def test_regenerate_label(self, client, monkeypatch):
        ids = _seed_docs(2)
        _seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        monkeypatch.setattr(
            "pka.clustering.engine._label_cluster_with_llm",
            lambda samples, model=None, **kw: ("Regenerated Topic", "A description."),
        )
        r = client.post(f"/clusters/{cid}/regenerate-label")
        assert r.status_code == 200
        assert r.json()["label"] == "Regenerated Topic"
        assert r.json()["description"] == "A description."

    def test_regenerate_label_passes_temperature(self, client, monkeypatch):
        captured: dict = {}

        def fake_chat_json(prompt, model=None, timeout=90, *, temperature=None):
            captured["temperature"] = temperature
            return {"label": "New Label", "description": "New desc"}, None

        monkeypatch.setattr("pka.ollama_chat.chat_json", fake_chat_json)
        ids = _seed_docs(2)
        _seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.post(f"/clusters/{cid}/regenerate-label")
        assert r.status_code == 200
        assert captured.get("temperature") == 0.85

    def test_apply_tag_uses_cluster_label(self, client):
        ids = _seed_docs(3)
        _seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        from pka.db.queries import get_engine
        from pka.db.schema import clusters
        with get_engine().begin() as con:
            con.execute(
                clusters.update()
                .where(clusters.c.cluster_id == cid)
                .values(label="Distributed Systems")
            )
        r = client.post(f"/clusters/{cid}/apply-tag", json={})
        assert r.status_code == 200
        assert r.json()["tag"] == "distributed-systems"

    def test_apply_tag_to_cluster(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.post(f"/clusters/{cid}/apply-tag", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["cluster_id"] == cid
        assert data["applied"] >= 1
        assert data["tag"]

        from pka.db.queries import get_engine
        from pka.db.schema import overlay_tags
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(overlay_tags.c.tag, overlay_tags.c.origin)
                .where(overlay_tags.c.tag == data["tag"])
            ).fetchall()
        assert len(rows) >= 1
        assert all(r[1] == "cluster_l1" for r in rows)

    def test_apply_l2_tag_uses_cluster_l2_origin(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2, with_l2=True)
        l2 = next(c for c in client.get("/clusters").json() if c["level"] == 2)
        r = client.post(
            f"/clusters/{l2['cluster_id']}/apply-tag", json={"tag": "subtopic-a"},
        )
        assert r.status_code == 200
        from pka.db.queries import get_engine
        from pka.db.schema import overlay_tags
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(overlay_tags.c.origin)
                .where(overlay_tags.c.tag == r.json()["tag"])
            ).fetchall()
        assert rows
        assert all(row[0] == "cluster_l2" for row in rows)

    def test_apply_tag_idempotent(self, client):
        ids = _seed_docs(3)
        _seed_run(ids, n_clusters=1)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        first = client.post(f"/clusters/{cid}/apply-tag", json={}).json()
        second = client.post(f"/clusters/{cid}/apply-tag", json={}).json()
        assert second["applied"] == 0
        assert second["skipped"] == first["applied"]

    def test_apply_all_tags(self, client):
        ids = _seed_docs(6)
        _seed_run(ids, n_clusters=3)
        r = client.post("/clusters/apply-all-tags")
        assert r.status_code == 200
        data = r.json()
        assert len(data["clusters"]) == 3
        assert data["total_applied"] >= 3

    def test_apply_all_tags_no_active_run(self, client):
        assert client.post("/clusters/apply-all-tags").status_code == 404

    def test_cluster_404(self, client):
        assert client.get("/clusters/99999").status_code == 404

    def test_cluster_documents(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2)
        cid = client.get("/clusters").json()[0]["cluster_id"]
        r = client.get(f"/clusters/{cid}/documents")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_scatter_points_with_umap(self, client):
        import json

        from pka.db.queries import get_engine
        from pka.db.schema import cluster_runs

        ids = _seed_docs(3)
        run_id = _seed_run(ids, n_clusters=2)
        points = [{"doc_id": ids[0], "x": 1.5, "y": -0.5, "cluster_id": 0}]
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update()
                .where(cluster_runs.c.run_id == run_id)
                .values(accepted=True, umap_points=json.dumps(points))
            )
        r = client.get("/clusters/scatter/points")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["doc_id"] == ids[0]

    def test_scatter_empty_without_umap(self, client):
        ids = _seed_docs(2)
        run_id = _seed_run(ids)
        from pka.db.queries import get_engine
        from pka.db.schema import cluster_runs
        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update()
                .where(cluster_runs.c.run_id == run_id)
                .values(accepted=True)
            )
        assert client.get("/clusters/scatter/points").json() == []


# ── Runs ──────────────────────────────────────────────────────────────────────

class TestRuns:
    def test_list_runs_empty(self, client):
        r = client.get("/runs")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_runs_after_seed(self, client):
        ids = _seed_docs(2)
        _seed_run(ids)
        r = client.get("/runs")
        assert len(r.json()) >= 1

    def test_accept_run(self, client):
        ids = _seed_docs(2)
        run_id = _seed_run(ids)
        r = client.post(f"/runs/{run_id}/reject", params={"notes": "too fragmented"})
        assert r.status_code == 204
        r2 = client.post(f"/runs/{run_id}/accept")
        assert r2.status_code == 204

    def test_accept_older_run_switches_active(self, client):
        """Design §4.2.2: rollback = changing which run_id is marked active."""
        ids = _seed_docs(2)
        old_run = _seed_run(ids)
        new_run = _seed_run(ids)
        assert client.post(f"/runs/{new_run}/accept").status_code == 204
        assert client.post(f"/runs/{old_run}/accept").status_code == 204
        runs = {r["run_id"]: r for r in client.get("/runs").json()}
        assert runs[old_run]["accepted"] is True
        assert runs[new_run]["accepted"] is False

    def test_accept_failed_run_409(self, client):
        from pka.db.queries import get_engine
        with get_engine().begin() as con:
            res = con.execute(cluster_runs.insert().values(
                timestamp=int(time.time()), algorithm="HDBSCAN",
                parameters="{}", accepted=False, status="failed",
            ))
            run_id = res.inserted_primary_key[0]
        assert client.post(f"/runs/{run_id}/accept").status_code == 409
        assert client.post(f"/runs/{run_id}/reject").status_code == 409

    def test_diagnostics_200(self, client):
        ids = _seed_docs(4)
        run_id = _seed_run(ids, n_clusters=2)
        r = client.get(f"/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        data = r.json()
        assert "n_clusters" in data
        assert "drift_flags" in data
        assert "merge_suggestions" in data

    def test_diagnostics_404(self, client):
        assert client.get("/runs/99999/diagnostics").status_code == 404

    def test_trigger_run_queued(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        monkeypatch.setattr(
            "pka.clustering.engine.run_clustering",
            lambda **kw: MagicMock(run_id=99, n_clusters=3, n_noise=1),
        )
        r = client.post("/runs/trigger")
        assert r.status_code == 202
        data = r.json()
        assert data["status"] == "queued"
        assert isinstance(data["run_id"], int)

    def test_list_runs_includes_status(self, client):
        ids = _seed_docs(2)
        run_id = _seed_run(ids)
        r = client.get("/runs")
        assert r.status_code == 200
        row = next(x for x in r.json() if x["run_id"] == run_id)
        assert row["status"] == "finished"

    def test_cancel_run(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)

        def slow_cluster(**kw):
            import time

            from pka.clustering.run_progress import check_cancel
            for _ in range(50):
                if check_cancel(kw["run_id"]):
                    from pka.clustering.run_progress import ClusterRunCancelled
                    raise ClusterRunCancelled()
                time.sleep(0.05)
            return MagicMock(run_id=kw["run_id"], n_clusters=1, n_noise=0)

        monkeypatch.setattr("pka.clustering.engine.run_clustering", slow_cluster)
        trig = client.post("/runs/trigger")
        assert trig.status_code == 202
        run_id = trig.json()["run_id"]

        listed = client.get("/runs").json()
        row = next(x for x in listed if x["run_id"] == run_id)
        assert row["status"] == "running"

        cancel = client.post(f"/runs/{run_id}/cancel")
        assert cancel.status_code == 202
        assert cancel.json()["status"] == "cancel_requested"

    def test_cancel_run_not_running(self, client):
        ids = _seed_docs(2)
        run_id = _seed_run(ids)
        r = client.post(f"/runs/{run_id}/cancel")
        assert r.status_code == 409

    def test_trigger_run_rejects_empty_chroma(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 0
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post("/runs/trigger")
        assert r.status_code == 400

    def test_trigger_run_rejects_too_few_vectors(self, client, monkeypatch):
        mock_col = MagicMock()
        mock_col.count.return_value = 3
        monkeypatch.setattr("pka.storage.vector_store.get_collection", lambda: mock_col)
        r = client.post("/runs/trigger")
        assert r.status_code == 400


# ── Trends ────────────────────────────────────────────────────────────────────

class TestTrends:
    def test_timeline_returns_dict(self, client):
        r = client.get("/trends/timeline")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "timeline" in data
        assert "sizes" in data

    def test_sources_over_time_returns_dict(self, client):
        _seed_docs(3)
        r = client.get("/trends/sources")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_sources_contains_expected_keys(self, client):
        _seed_docs(3)
        r = client.get("/trends/sources")
        sources = set(r.json().keys())
        assert sources & {"zotero", "firefox", "calibre"}

    def test_timeline_with_cluster_run(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2)
        r = client.get("/trends/timeline")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["timeline"], dict)
        assert isinstance(data["sizes"], dict)
        assert sum(data["sizes"].values()) >= 1
        assert any(data["timeline"].values())

    def test_timeline_kernel_values_are_floats(self, client):
        ids = _seed_docs(2)
        _seed_run(ids)
        r = client.get("/trends/timeline")
        data = r.json()
        for periods in data["timeline"].values():
            for value in periods.values():
                assert isinstance(value, float)

    def test_timeline_excludes_level2_clusters(self, client):
        ids = _seed_docs(4)
        _seed_run(ids, n_clusters=2, with_l2=True)
        data = client.get("/trends/timeline").json()
        labels = set(data["timeline"].keys())
        assert "Subcluster 0" not in labels
        assert labels <= {"Cluster 0", "Cluster 1"}


# ── Ingestion ─────────────────────────────────────────────────────────────────

class TestIngestion:
    def setup_method(self):
        from pka.constants import ALL_SOURCES
        from pka.ingestion import sync_progress as sp
        for src in ALL_SOURCES:
            sp.reset(src)

    def test_status_returns_totals(self, client):
        _seed_docs(3)
        r = client.get("/ingestion/status")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert data["total"] >= 3

    def test_status_reports_unavailable_sources(self, client):
        r = client.get("/ingestion/status")
        assert r.status_code == 200
        data = r.json()
        unavailable = data.get("source_unavailable", {})
        assert "calibre" in unavailable
        assert "image" in unavailable
        assert unavailable["calibre"] is not None
        assert unavailable["image"] is not None
        assert "metadata.db" in unavailable["calibre"]
        assert "Image folder not found" in unavailable["image"]

    def test_unfetchable_returns_list(self, client):
        r = client.get("/ingestion/unfetchable")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_sync_valid_source_queued(self, client, monkeypatch):
        from pka.ingestion import sync_progress as sp

        def fake_sync(src: str) -> None:
            sp.finish(src)

        sp.reset("zotero")
        monkeypatch.setattr("pka.api.routers.ingestion._sync", fake_sync, raising=False)
        r = client.post("/ingestion/sync/zotero")
        assert r.status_code == 202

    def test_sync_invalid_source_400(self, client):
        r = client.post("/ingestion/sync/nonexistent")
        assert r.status_code == 400

    def test_pause_sync_requires_active_job(self, client):
        from pka.ingestion import sync_progress as sp
        sp.reset("zotero")
        r = client.post("/ingestion/sync/zotero/pause")
        assert r.status_code == 409

    def test_cancel_sync_requires_active_job(self, client):
        from pka.ingestion import sync_progress as sp
        sp.reset("zotero")
        r = client.post("/ingestion/sync/zotero/cancel")
        assert r.status_code == 409

    def test_pause_sync_when_running(self, client):
        from pka.ingestion import sync_progress as sp
        sp.reset("firefox")
        sp.begin("firefox", phase="fetching")
        sp.set_phase("firefox", "fetching", 10)
        r = client.post("/ingestion/sync/firefox/pause")
        assert r.status_code == 202
        assert r.json()["status"] == "pause_requested"
        assert sp.check_stop("firefox") == "pause"

    def test_cancel_sync_when_running(self, client):
        from pka.ingestion import sync_progress as sp
        sp.reset("zotero")
        sp.begin("zotero", phase="embedding")
        sp.set_phase("zotero", "embedding", 5)
        r = client.post("/ingestion/sync/zotero/cancel")
        assert r.status_code == 202
        assert r.json()["status"] == "cancel_requested"
        assert sp.check_stop("zotero") == "cancel"

    def test_sync_progress_snapshot(self, client):
        from pka.ingestion import sync_progress as sp
        sp.reset("firefox")
        sp.begin("firefox")
        sp.plan_pipeline("firefox", [("metadata", 2), ("fetching", 2)])
        sp.set_phase("firefox", "metadata", 2)
        sp.advance("firefox")
        r = client.get("/ingestion/sync/progress?source=firefox")
        assert r.status_code == 200
        data = r.json()["firefox"]
        assert data["overall_processed"] == 1
        assert data["overall_total"] == 4  # metadata + fetching (Firefox skips embed phase)
        assert len(data["phase_details"]) == 3
        assert data["phase_details"][0]["total"] == 2
        assert data["phase_details"][1]["total"] == 2
        assert data["phase_details"][2]["total"] == 0
        assert data["phase_details"][0]["processed"] == 1
        assert data["phase_details"][2]["processed"] == 0
        sp.reset("firefox")

    def test_sync_metadata_queued(self, client, monkeypatch):
        from pka.ingestion import sync_progress as sp

        def fake_meta(src: str) -> None:
            sp.finish(src)

        sp.reset("zotero")
        monkeypatch.setattr("pka.api.routers.ingestion._sync_metadata", fake_meta, raising=False)
        r = client.post("/ingestion/sync/zotero/metadata")
        assert r.status_code == 202
        assert r.json()["job"] == "metadata"

    def test_sync_ingest_queued(self, client, monkeypatch):
        from pka.ingestion import sync_progress as sp

        def fake_ingest(src: str) -> None:
            sp.finish(src)

        sp.reset("firefox")
        monkeypatch.setattr("pka.api.routers.ingestion._sync_ingest", fake_ingest, raising=False)
        r = client.post("/ingestion/sync/firefox/ingest")
        assert r.status_code == 202
        assert r.json()["job"] == "ingest"

    def test_sync_metadata_routes_zotero(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        called = []
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.sync_zotero_metadata",
            lambda progress_key=None: called.append(progress_key) or {"metadata": {}},
        )
        sp.reset("zotero")
        ing._sync_metadata("zotero")
        assert called == ["zotero"]
        assert sp.snapshot("zotero")["zotero"]["status"] == "done"
        assert sp.snapshot("zotero")["zotero"]["active_job"] is None

    def test_sync_ingest_routes_firefox(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        called = []
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.sync_firefox_ingest",
            lambda progress_key=None, **kw: called.append(progress_key) or {"embed": {}},
        )
        sp.reset("firefox")
        ing._sync_ingest("firefox")
        assert called == ["firefox"]
        snap = sp.snapshot("firefox")["firefox"]
        assert snap["status"] == "done"
        assert snap["active_job"] is None

    def test_sync_progress_unknown_source_400(self, client):
        r = client.get("/ingestion/sync/progress?source=invalid")
        assert r.status_code == 400

    def test_force_cancels_running_job_before_restart(self, client, monkeypatch):
        """force=true must stop the running worker, not run two jobs concurrently."""
        import threading as th

        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        started: list[th.Event] = []

        def fake_job(src: str) -> None:
            ev = th.Event()
            started.append(ev)
            sp.begin_job(src, "metadata")
            ev.set()
            while not sp.check_stop(src):
                time.sleep(0.005)
            sp.finish(src, stopped="cancel")

        monkeypatch.setitem(ing._JOB_TARGETS, "metadata", fake_job)
        sp.reset("zotero")

        r1 = client.post("/ingestion/sync/zotero/metadata")
        assert r1.status_code == 202
        assert started[0].wait(timeout=2.0)

        # Same job again without force → conflict, still exactly one worker.
        assert client.post("/ingestion/sync/zotero/metadata").status_code == 409
        assert len(started) == 1

        # force=true cancels the first worker and only then starts a second.
        r2 = client.post("/ingestion/sync/zotero/metadata?force=true")
        assert r2.status_code == 202
        deadline = time.time() + 2.0
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.005)
        assert len(started) == 2
        assert started[1].wait(timeout=2.0)

        # Clean up: stop the second worker too.
        sp.request_cancel("zotero")
        deadline = time.time() + 2.0
        while sp.is_running("zotero") and time.time() < deadline:
            time.sleep(0.005)
        assert not sp.is_running("zotero")

    def test_rebuild_vectors_queued(self, client, monkeypatch):
        from pka.api.routers import ingestion as ing

        ing._rebuild_running = False
        monkeypatch.setattr(
            "pka.storage.vector_store.rebuild_from_chunks",
            lambda **kw: {"chunks": 0, "processed": 0},
        )
        r = client.post("/ingestion/rebuild-vectors")
        assert r.status_code == 202
        assert r.json()["status"] == "queued"
        ing._rebuild_running = False

    def test_rebuild_vectors_409_when_busy(self, client):
        from pka.api.routers import ingestion as ing

        ing._rebuild_running = True
        try:
            r = client.post("/ingestion/rebuild-vectors")
            assert r.status_code == 409
        finally:
            ing._rebuild_running = False

    def test_sync_routes_zotero_via_sync_fn(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        called = []
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.sync_zotero",
            lambda progress_key=None: called.append(progress_key) or {"processed": 1},
        )
        sp.reset("zotero")
        ing._sync("zotero")
        assert called == ["zotero"]
        assert sp.snapshot("zotero")["zotero"]["status"] == "done"

    def test_sync_records_error_on_failure(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        def boom(**kw):
            raise RuntimeError("sync blew up")

        monkeypatch.setattr("pka.ingestion.zotero_sync.sync_zotero", boom)
        sp.reset("zotero")
        ing._sync("zotero")
        snap = sp.snapshot("zotero")["zotero"]
        assert snap["status"] == "error"
        assert "sync blew up" in snap["error"]

    def test_sync_firefox_source(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.sync_firefox",
            lambda progress_key=None, **kw: {"metadata": {}, "stopped": "pause"},
        )
        sp.reset("firefox")
        ing._sync("firefox")
        assert sp.snapshot("firefox")["firefox"]["status"] == "paused"

    def test_sync_calibre_source(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.sync_calibre",
            lambda progress_key=None, **kw: {"metadata": {}, "fulltext": {}},
        )
        sp.reset("calibre")
        ing._sync("calibre")
        assert sp.snapshot("calibre")["calibre"]["status"] == "done"

    def test_sync_image_source(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import sync_progress as sp

        monkeypatch.setattr(
            "pka.ingestion.image_sync.sync_images",
            lambda progress_key=None, **kw: {"processed": 2, "stopped": "cancel"},
        )
        sp.reset("image")
        ing._sync("image")
        assert sp.snapshot("image")["image"]["status"] == "cancelled"


# ── Images ────────────────────────────────────────────────────────────────────

class TestImages:
    def test_list_images_empty(self, client):
        r = client.get("/images")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_images_returns_seeded(self, client):
        _seed_image()
        r = client.get("/images")
        assert len(r.json()) == 1
        assert r.json()[0]["filename"] == "slide.png"
        assert "ml" in r.json()[0]["tags"]

    def test_list_images_filter_by_type(self, client):
        _seed_image()
        r = client.get("/images?image_type=slide")
        assert len(r.json()) == 1
        r2 = client.get("/images?image_type=poster")
        assert r2.json() == []

    def test_get_image_by_id(self, client):
        image_id = _seed_image()
        r = client.get(f"/images/{image_id}")
        assert r.status_code == 200
        assert r.json()["image_type"] == "slide"

    def test_get_image_404(self, client):
        assert client.get("/images/99999").status_code == 404

    def test_get_image_file_serves_bytes(self, client, tmp_path):
        from pka.db.queries import get_engine
        from pka.db.schema import images as images_tbl

        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        with get_engine().begin() as con:
            image_id = con.execute(images_tbl.insert().values(
                path=str(img), filename="pic.png", image_type="slide",
            )).inserted_primary_key[0]

        r = client.get(f"/images/{image_id}/file")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content == b"\x89PNG\r\n\x1a\nfake"

    def test_get_image_file_missing_on_disk(self, client):
        # _seed_image points at /tmp/slide.png, which does not exist on disk.
        image_id = _seed_image()
        assert client.get(f"/images/{image_id}/file").status_code == 404

    def test_get_image_file_unknown_id(self, client):
        assert client.get("/images/99999/file").status_code == 404

    def test_search_images(self, client, monkeypatch):
        _seed_image()
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.search_images_by_text",
            lambda q, n=10: [{
                "vector_id": "clip-1",
                "filename": "slide.png",
                "path": "/tmp/slide.png",
                "image_type": "slide",
                "distance": 0.15,
            }],
        )
        r = client.get("/images/search?q=neural")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["similarity"] == pytest.approx(0.85)


# ── Reading lists ─────────────────────────────────────────────────────────────

class TestReadingLists:
    def test_create_list(self, client):
        r = client.post("/reading-lists", json={"name": "My list"})
        assert r.status_code == 201
        assert "list_id" in r.json()

    def test_list_reading_lists(self, client):
        client.post("/reading-lists", json={"name": "A"})
        client.post("/reading-lists", json={"name": "B"})
        r = client.get("/reading-lists")
        assert len(r.json()) >= 2

    def test_add_and_retrieve_item(self, client):
        ids = _seed_docs(1)
        list_id = client.post("/reading-lists", json={"name": "test"}).json()["list_id"]
        client.post(f"/reading-lists/{list_id}/items",
                    json={"document_id": ids[0], "note": "read this"})
        items = client.get(f"/reading-lists/{list_id}/items").json()
        assert len(items) == 1
        assert items[0]["doc_id"] == ids[0]
        assert items[0]["note"] == "read this"

    def test_remove_item(self, client):
        ids = _seed_docs(1)
        list_id = client.post("/reading-lists", json={"name": "r"}).json()["list_id"]
        item_id = client.post(f"/reading-lists/{list_id}/items",
                              json={"document_id": ids[0]}).json()["id"]
        r = client.delete(f"/reading-lists/{list_id}/items/{item_id}")
        assert r.status_code == 204
        assert client.get(f"/reading-lists/{list_id}/items").json() == []

    def test_delete_list(self, client):
        list_id = client.post("/reading-lists", json={"name": "del"}).json()["list_id"]
        r = client.delete(f"/reading-lists/{list_id}")
        assert r.status_code == 204
        lists = client.get("/reading-lists").json()
        assert not any(item["list_id"] == list_id for item in lists)

    def test_items_order_by_position(self, client):
        ids = _seed_docs(3)
        list_id = client.post("/reading-lists", json={"name": "order"}).json()["list_id"]
        for did in ids:
            client.post(f"/reading-lists/{list_id}/items", json={"document_id": did})
        items = client.get(f"/reading-lists/{list_id}/items").json()
        positions = [i["position"] for i in items]
        assert positions == sorted(positions)
