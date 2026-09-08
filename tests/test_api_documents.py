"""``/documents`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

import time

from pka.db.queries import (
    DocumentWrite,
    insert_chunks,
    update_card_summary,
    upsert_document,
)
from pka.db.schema import cluster_runs
from tests.api_seed import image_document_id, seed_docs, seed_image, seed_run
from tests.conftest import make_document

# ── Documents ─────────────────────────────────────────────────────────────────


class TestDocuments:
    def test_list_documents_200(self, client):
        seed_docs(3)
        r = client.get("/documents")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["documents"]) == 3

    def test_list_documents_fields(self, client):
        ids = seed_docs(1)
        insert_chunks(
            [
                {
                    "document_id": ids[0],
                    "chunk_index": 0,
                    "text": "First chunk body text.",
                    "token_count": 4,
                    "vector_id": "v0",
                }
            ]
        )
        doc = client.get("/documents").json()["documents"][0]
        for key in (
            "id",
            "source",
            "source_id",
            "title",
            "description",
            "url_or_path",
            "archive_url",
            "zotero_attachment_key",
            "source_tags",
            "cluster_l1_tags",
            "cluster_l2_tags",
        ):
            assert key in doc
        assert doc["description"] == "First chunk body text."
        assert doc["source_tags"] == []
        assert doc["cluster_l1_tags"] == []
        assert doc["cluster_l2_tags"] == []

    def test_list_documents_snippet_truncation(self, client):
        from pka.card_summary import SUMMARY_MAX_LEN

        ids = seed_docs(1)
        long_text = "word " * 70
        insert_chunks(
            [
                {
                    "document_id": ids[0],
                    "chunk_index": 0,
                    "text": long_text,
                    "token_count": 70,
                    "vector_id": "v0",
                }
            ]
        )
        doc = client.get("/documents").json()["documents"][0]
        assert len(doc["description"]) <= SUMMARY_MAX_LEN + 1  # +1 for the ellipsis
        assert doc["description"].endswith("…")

    def test_list_documents_prefers_card_summary(self, client):
        ids = seed_docs(1)
        insert_chunks(
            [
                {
                    "document_id": ids[0],
                    "chunk_index": 0,
                    "text": "Test Paper by Alice",
                    "token_count": 4,
                    "vector_id": "v0",
                }
            ]
        )
        update_card_summary(ids[0], "This is the abstract for the paper.")
        doc = client.get("/documents").json()["documents"][0]
        assert doc["description"] == "This is the abstract for the paper."

    def test_list_documents_uses_first_chunk(self, client):
        ids = seed_docs(1)
        insert_chunks(
            [
                {
                    "document_id": ids[0],
                    "chunk_index": 1,
                    "text": "Second",
                    "token_count": 1,
                    "vector_id": "v1",
                },
                {
                    "document_id": ids[0],
                    "chunk_index": 0,
                    "text": "First",
                    "token_count": 1,
                    "vector_id": "v0",
                },
            ]
        )
        doc = client.get("/documents").json()["documents"][0]
        assert doc["description"] == "First"

    def test_list_documents_source_filter(self, client):
        seed_docs(3)
        r = client.get("/documents?sources=zotero")
        docs = r.json()["documents"]
        assert all(d["source"] == "zotero" for d in docs)

    def test_list_documents_wayback_only_filter(self, client):

        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table

        ids = seed_docs(3)
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/1"
        with get_engine().begin() as con:
            con.execute(
                docs_table.update().where(docs_table.c.id == ids[1]).values(archive_url=snapshot)
            )

        r = client.get("/documents?wayback_only=true")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert len(data["documents"]) == 1
        assert data["documents"][0]["source"] == "firefox"
        assert data["documents"][0]["archive_url"] == snapshot

    def test_list_documents_pagination(self, client):
        seed_docs(5)
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

        ids = seed_docs(3)
        insert_source_tags(ids[0], ["ml", "python"], source="zotero")
        insert_source_tags(ids[1], ["ml"], source="firefox")
        insert_source_tags(ids[2], ["python"], source="calibre")

        r = client.get("/documents", params=[("source_tags", "ml"), ("source_tags", "python")])
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["documents"][0]["id"] == ids[0]

    def test_list_documents_overlay_tags_and_source_filter(self, client):
        ids = seed_docs(3)
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

        ids = seed_docs(3)
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
        ids = seed_docs(4)
        seed_run(ids, n_clusters=2, with_l2=True)
        l1 = next(c for c in client.get("/clusters").json() if c["level"] == 1)
        l2 = next(c for c in client.get("/clusters").json() if c["level"] == 2)
        l1_tag = client.post(
            f"/clusters/{l1['cluster_id']}/apply-tag",
            json={"tag": "topic-l1"},
        ).json()["tag"]
        l2_tag = client.post(
            f"/clusters/{l2['cluster_id']}/apply-tag",
            json={"tag": "topic-l2"},
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
        ids = seed_docs(1)
        r = client.get(f"/documents/{ids[0]}")
        assert r.status_code == 200

    def test_get_document_fields(self, client):
        ids = seed_docs(1)
        data = client.get(f"/documents/{ids[0]}").json()
        for key in ("id", "title", "source", "source_tags", "overlay_tags", "chunks_count"):
            assert key in data

    def test_get_document_description(self, client):
        ids = seed_docs(1)
        insert_chunks(
            [
                {
                    "document_id": ids[0],
                    "chunk_index": 0,
                    "text": "First chunk body text.",
                    "token_count": 4,
                    "vector_id": "v0",
                }
            ]
        )
        data = client.get(f"/documents/{ids[0]}").json()
        assert data["description"] == "First chunk body text."

    def test_get_document_prefers_card_summary(self, client):
        ids = seed_docs(1)
        insert_chunks(
            [
                {
                    "document_id": ids[0],
                    "chunk_index": 0,
                    "text": "Title chunk only",
                    "token_count": 3,
                    "vector_id": "v0",
                }
            ]
        )
        update_card_summary(ids[0], "Stored card summary.")
        data = client.get(f"/documents/{ids[0]}").json()
        assert data["description"] == "Stored card summary."

    def test_get_document_with_cluster(self, client):
        ids = seed_docs(4)
        run_id = seed_run(ids, n_clusters=2)
        from pka.db.queries import get_engine

        with get_engine().begin() as con:
            con.execute(
                cluster_runs.update().where(cluster_runs.c.run_id == run_id).values(accepted=True)
            )
        data = client.get(f"/documents/{ids[0]}").json()
        assert data["cluster_id"] is not None
        assert data["cluster_label"] is not None

    def test_get_document_404(self, client):
        assert client.get("/documents/99999").status_code == 404

    def test_patch_tags_add(self, client):
        ids = seed_docs(1)
        r = client.patch(f"/documents/{ids[0]}/tags", json={"add": ["my-tag"], "remove": []})
        assert r.status_code == 200
        data = client.get(f"/documents/{ids[0]}").json()
        overlay = [t["tag"] for t in data["overlay_tags"]]
        assert "my-tag" in overlay

    def test_patch_tags_remove(self, client):
        ids = seed_docs(1)
        client.patch(f"/documents/{ids[0]}/tags", json={"add": ["bye"], "remove": []})
        client.patch(f"/documents/{ids[0]}/tags", json={"add": [], "remove": ["bye"]})
        data = client.get(f"/documents/{ids[0]}").json()
        overlay = [t["tag"] for t in data["overlay_tags"]]
        assert "bye" not in overlay

    def test_patch_tags_add_is_idempotent(self, client):
        """Adding the same manual tag twice must not create duplicate rows."""
        ids = seed_docs(1)
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

        doc_id = make_document(
            "calibre",
            "1",
            "Title",
            str(book_dir / "book.epub"),
            int(time.time()),
        )
        r = client.get(f"/documents/{doc_id}/cover")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == b"\xff\xd8\xff\xe0fakejpeg"

    def test_404_when_no_cover_file(self, client, tmp_path):
        book_dir = tmp_path / "Author" / "Title (1)"
        book_dir.mkdir(parents=True)
        (book_dir / "book.epub").write_bytes(b"epub")

        doc_id = make_document(
            "calibre",
            "1",
            "Title",
            str(book_dir / "book.epub"),
            int(time.time()),
        )
        r = client.get(f"/documents/{doc_id}/cover")
        assert r.status_code == 404

    def test_404_for_non_calibre_source(self, client):
        ids = seed_docs(3)
        firefox_id = next(
            i for i in ids if client.get(f"/documents/{i}").json()["source"] == "firefox"
        )
        r = client.get(f"/documents/{firefox_id}/cover")
        assert r.status_code == 404

    def test_404_for_unknown_document(self, client):
        r = client.get("/documents/999999/cover")
        assert r.status_code == 404

    def test_image_document_cover_streams_the_file(self, client, tmp_path):
        from PIL import Image as PILImage

        p = tmp_path / "photo.png"
        PILImage.new("RGB", (8, 8), color="blue").save(p)
        doc_id = make_document(
            "image",
            str(p),
            "photo.png",
            str(p),
            int(time.time()),
            fetch_status="available",
        )
        r = client.get(f"/documents/{doc_id}/cover")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"


class TestImageDocuments:
    def test_image_appears_in_document_list(self, client):
        seed_image()
        r = client.get("/documents?sources=image")
        docs = r.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["source"] == "image"
        assert docs[0]["title"] == "slide.png"

    def test_image_detail_served(self, client):
        image_id = seed_image()
        doc_id = image_document_id(image_id)
        r = client.get(f"/documents/{doc_id}")
        assert r.status_code == 200
        assert r.json()["source"] == "image"


# ── Reddit documents ──────────────────────────────────────────────────────────


def _seed_reddit(source_id: str, url: str, *, item_type: str | None = None) -> int:
    return upsert_document(
        DocumentWrite(
            "reddit",
            source_id,
            "Understanding Raft",
            url,
            int(time.time()),
            item_type=item_type,
        )
    )


class TestRedditDocuments:
    def test_detail_exposes_stored_reddit_fields(self, client):
        from pka.db.queries import upsert_reddit_item

        doc_id = _seed_reddit("t3_linkpost", "https://example.com/paxos.pdf")
        upsert_reddit_item(
            doc_id,
            kind="post",
            subreddit="distributed",
            permalink="https://www.reddit.com/r/distributed/comments/linkpost/x/",
            external_url="https://example.com/paxos.pdf",
            body="Line one.\n\nLine two.",
        )

        data = client.get(f"/documents/{doc_id}").json()

        assert data["reddit"] == {
            "kind": "post",
            "subreddit": "distributed",
            "permalink": "https://www.reddit.com/r/distributed/comments/linkpost/x/",
            "external_url": "https://example.com/paxos.pdf",
            "body": "Line one.\n\nLine two.",
        }

    def test_body_keeps_paragraph_breaks_that_the_description_loses(self, client):
        from pka.db.queries import update_card_summary, upsert_reddit_item

        body = "First paragraph.\n\nSecond paragraph."
        doc_id = _seed_reddit("t1_c1", "https://www.reddit.com/r/compsci/comments/a/b/")
        upsert_reddit_item(doc_id, kind="comment", subreddit="compsci", body=body)
        update_card_summary(doc_id, "First paragraph. Second paragraph.")

        data = client.get(f"/documents/{doc_id}").json()

        assert data["reddit"]["body"] == body
        assert "\n" not in data["description"]

    def test_non_reddit_document_has_no_reddit_block(self, client):
        ids = seed_docs(1)
        assert client.get(f"/documents/{ids[0]}").json()["reddit"] is None

    def test_link_post_permalink_derived_when_no_detail_row(self, client):
        """A library archived before ``reddit_items`` existed still reaches the thread."""
        doc_id = _seed_reddit(
            "t3_abc123",
            "https://example.com/paxos.pdf",
            item_type="post",
        )

        reddit = client.get(f"/documents/{doc_id}").json()["reddit"]

        assert reddit["kind"] == "post"
        assert reddit["permalink"] == "https://www.reddit.com/comments/abc123"
        assert reddit["external_url"] == "https://example.com/paxos.pdf"
        # Only the body is unrecoverable — that is why the column exists.
        assert reddit["body"] is None

    def test_self_post_fallback_is_not_mistaken_for_a_link_post(self, client):
        """A self-post stores its own permalink, so it has no external target."""
        url = "https://www.reddit.com/r/socialism/comments/1vqzgfx/book_suggestions/"
        doc_id = _seed_reddit("t3_1vqzgfx", url, item_type="post")

        reddit = client.get(f"/documents/{doc_id}").json()["reddit"]

        assert reddit["permalink"] == url
        assert reddit["external_url"] is None

    def test_comment_fallback_uses_its_url_as_the_permalink(self, client):
        url = "https://www.reddit.com/r/compsci/comments/xyz/raft/c1/"
        doc_id = _seed_reddit("t1_comment1", url)

        reddit = client.get(f"/documents/{doc_id}").json()["reddit"]

        assert reddit["kind"] == "comment"
        assert reddit["permalink"] == url
        assert reddit["external_url"] is None

    def test_fallback_recovers_subreddit_from_collections(self, client):
        from pka.db.queries import insert_source_collections

        doc_id = _seed_reddit("t3_xyz789", "https://example.com/a", item_type="post")
        insert_source_collections(doc_id, ["r/compsci"], source="reddit")

        reddit = client.get(f"/documents/{doc_id}").json()["reddit"]

        assert reddit["subreddit"] == "compsci"
