"""Duplicate documents: the keys, the scan, the links and every read site."""

import numpy as np
import pytest
import sqlalchemy as sa

from pka.clustering.doc_embeddings import embedding_to_blob
from pka.constants import TagOrigin
from pka.db import duplicates
from pka.db.chunks import insert_chunks
from pka.db.engine import get_engine
from pka.db.migrate import init_db
from pka.db.schema import document_duplicates as dd
from pka.db.schema import documents
from pka.db.tags import insert_source_tags, list_tags, sync_overlay_tags
from pka.dedupe import canonical_url, isbn13, scan
from tests.conftest import make_document


@pytest.fixture()
def db():
    init_db()


def _links(state="merged"):
    with get_engine().connect() as con:
        return {
            (r.canonical_id, r.duplicate_id, r.match_key)
            for r in con.execute(sa.select(dd).where(dd.c.state == state))
        }


def _chunk(doc_id: int, n: int = 1) -> None:
    insert_chunks(
        [
            {
                "document_id": doc_id,
                "chunk_index": i,
                "text": f"text {doc_id} {i}",
                "token_count": 2,
                "vector_id": f"v-{doc_id}-{i}",
            }
            for i in range(n)
        ]
    )


class TestCanonicalUrl:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("http://www.example.com/page/", "https://example.com/page"),
            ("https://example.com/p?utm_source=x&b=2&a=1", "https://example.com/p?a=1&b=2"),
            ("https://example.com/p#section", "https://example.com/p"),
            ("https://example.com:443/p?fbclid=abc", "https://example.com/p"),
            (
                "https://youtu.be/dQw4w9WgXcQ?t=10",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=x",
            ),
            ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/shorts/dQw4w9WgXcQ"),
            (
                "https://old.reddit.com/r/Python/comments/abc123/some_title/?context=3",
                "https://www.reddit.com/r/python/comments/abc123/",
            ),
            (
                "https://www.amazon.fr/Deep-Learning-Goodfellow/dp/0262035618/ref=sr_1?keywords=x",
                "https://amazon.fr/dp/0262035618",
            ),
        ],
    )
    def test_spellings_of_one_page_match(self, a, b):
        assert canonical_url(a) == canonical_url(b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("https://example.com/p?page=1", "https://example.com/p?page=2"),
            ("https://youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=aaaaaaaaaaa"),
            ("https://amazon.fr/dp/0262035618", "https://amazon.com/dp/0262035618"),
            ("https://example.com/a", "https://example.org/a"),
            ("http://example.com:8080/p", "http://example.com/p"),
        ],
    )
    def test_different_pages_do_not(self, a, b):
        assert canonical_url(a) != canonical_url(b)

    @pytest.mark.parametrize(
        "raw", [None, "", "C:/books/x.pdf", "zotero://select/items/1", "ftp://x/y"]
    )
    def test_no_key_for_non_web_locations(self, raw):
        assert canonical_url(raw) is None

    def test_isbn10_and_13_meet(self):
        assert isbn13("0-262-03561-8") == isbn13("9780262035613") == "9780262035613"
        assert isbn13("not an isbn") is None


class TestExactScan:
    def test_the_same_doi_across_sources_links(self, db):
        z = make_document("zotero", "Z1", "Paper", doi="10.1/abc")
        f = make_document(
            "firefox",
            "F1",
            "Paper page",
            "https://pub.org/x",
            fetch_status="fetched",
            doi="10.1/ABC",
        )
        _chunk(f, 3)
        stats = scan(embeddings=False)
        assert stats["linked"] == 1
        # The fetched row with the text is canonical, not a preferred source.
        assert _links() == {(f, z, "doi")}

    def test_an_arxiv_id_meets_its_derived_doi(self, db):
        z = make_document("zotero", "Z1", arxiv_id="2301.00001")
        f = make_document("firefox", "F1", doi="10.48550/arxiv.2301.00001")
        scan(embeddings=False)
        assert {(c, d) for c, d, _ in _links()} in ({(z, f)}, {(f, z)})

    def test_isbn10_and_isbn13_link(self, db):
        a = make_document("calibre", "C1", isbn="0262035618")
        b = make_document("image", "I1", isbn="9780262035613")
        scan(embeddings=False)
        assert len(_links()) == 1 and {a, b} == set(next(iter(_links()))[:2])

    def test_url_spellings_link(self, db):
        a = make_document("firefox", "F1", "A", "https://www.example.com/post/?utm_source=rss")
        b = make_document("reddit", "R1", "B", "http://example.com/post")
        scan(embeddings=False)
        assert {frozenset(x[:2]) for x in _links()} == {frozenset((a, b))}

    def test_three_copies_link_to_one_canonical_without_a_chain(self, db):
        a = make_document("zotero", "Z1", doi="10.1/x", url_or_path="https://site.org/p")
        b = make_document(
            "firefox", "F1", url_or_path="https://site.org/p/", fetch_status="fetched"
        )
        c = make_document("firefox", "F2", doi="10.1/x")
        _chunk(b, 2)
        scan(embeddings=False)
        links = _links()
        assert {(canon, dup) for canon, dup, _ in links} == {(b, a), (b, c)}

    def test_a_rescan_changes_nothing(self, db):
        make_document("zotero", "Z1", doi="10.1/x")
        make_document("firefox", "F1", doi="10.1/x")
        scan(embeddings=False)
        before = _links()
        assert scan(embeddings=False)["linked"] == 0
        assert _links() == before

    def test_a_rejected_pair_stays_apart(self, db):
        make_document("zotero", "Z1", doi="10.1/x")
        make_document("firefox", "F1", doi="10.1/x")
        scan(embeddings=False)
        (row,) = duplicates.list_links("merged")
        duplicates.reject(row["id"])
        assert scan(embeddings=False)["linked"] == 0
        assert _links() == set()

    def test_dry_run_writes_nothing(self, db):
        make_document("zotero", "Z1", doi="10.1/x")
        make_document("firefox", "F1", doi="10.1/x")
        assert scan(embeddings=False, dry_run=True)["linked"] == 1
        assert _links() == set()

    def test_documents_without_keys_are_left_alone(self, db):
        make_document("calibre", "C1", "Book", "C:/books/a.epub")
        make_document("calibre", "C2", "Book", "C:/books/b.epub")
        assert scan(embeddings=False)["linked"] == 0


class TestLinkWriter:
    def test_links_stay_one_hop_deep(self, db):
        a, b, c = (make_document("firefox", f"F{i}") for i in range(3))
        duplicates.link(b, c)
        # b becomes a duplicate: its own duplicate moves with it.
        duplicates.link(a, b)
        assert {(x, y) for x, y, _ in _links()} == {(a, b), (a, c)}
        # Linking into a duplicate resolves to its canonical.
        d = make_document("firefox", "F9")
        duplicates.link(c, d)
        assert (a, d, "manual") in _links()

    def test_a_document_is_not_its_own_duplicate(self, db):
        a = make_document("firefox", "F1")
        with pytest.raises(duplicates.LinkError):
            duplicates.link(a, a)

    def test_the_database_allows_one_canonical_per_duplicate(self, db):
        a, b, c = (make_document("firefox", f"F{i}") for i in range(3))
        with get_engine().begin() as con:
            con.execute(
                dd.insert(),
                [
                    {
                        "canonical_id": a,
                        "duplicate_id": c,
                        "match_key": "manual",
                        "state": "merged",
                    },
                    {
                        "canonical_id": b,
                        "duplicate_id": c,
                        "match_key": "manual",
                        "state": "candidate",
                    },
                ],
            )
        with pytest.raises(sa.exc.IntegrityError), get_engine().begin() as con:
            con.execute(dd.update().where(dd.c.canonical_id == b).values(state="merged"))

    def test_purging_a_document_drops_its_links(self, db):
        a, b = make_document("firefox", "F1"), make_document("firefox", "F2")
        duplicates.link(a, b)
        with get_engine().begin() as con:
            assert duplicates.delete_for_documents(con, [b]) == 1
        assert _links() == set()


def _embed(doc_id: int, vec) -> None:
    blob = embedding_to_blob(np.asarray(vec, dtype=np.float32))
    with get_engine().begin() as con:
        con.execute(documents.update().where(documents.c.id == doc_id).values(doc_embedding=blob))


class TestEmbeddingCandidates:
    def test_near_vectors_are_proposed_not_linked(self, db):
        a = make_document("firefox", "F1", "Post")
        b = make_document("firefox", "F2", "Same post elsewhere")
        c = make_document("firefox", "F3", "Unrelated")
        _embed(a, [1.0, 0.0, 0.0])
        _embed(b, [0.999, 0.02, 0.0])
        _embed(c, [0.0, 1.0, 0.0])
        stats = scan(threshold=0.98)
        assert stats["proposed"] == 1 and stats["linked"] == 0
        (row,) = duplicates.list_links("candidate")
        assert {row["canonical_id"], row["duplicate_id"]} == {a, b}
        assert row["match_key"] == "embedding" and row["score"] >= 0.98
        assert _links() == set()

        duplicates.accept(row["id"])
        assert len(_links()) == 1
        assert scan(threshold=0.98)["proposed"] == 0

    def test_the_candidate_count_is_capped(self, db, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "dedupe_max_candidates", 2)
        for i in range(4):
            _embed(make_document("firefox", f"F{i}"), [1.0, 0.0])
        assert scan(threshold=0.9)["proposed"] == 2


@pytest.fixture()
def pair(db):
    """A Firefox bookmark and its Zotero copy, linked; plus an unrelated document."""
    f = make_document("firefox", "F1", "Raft (bookmark)", "https://raft.github.io", date_added=2)
    z = make_document("zotero", "Z1", "Raft (paper)", date_added=1)
    other = make_document("calibre", "C1", "A book", date_added=0)
    insert_source_tags(f, ["web"], "firefox")
    insert_source_tags(z, ["consensus", "Web"], "zotero")
    duplicates.link(f, z, match_key="url")
    return f, z, other


class TestReadSites:
    def test_browse_shows_one_card_with_both_rows_tags(self, pair, client):
        f, z, other = pair
        body = client.get("/documents").json()
        assert body["total"] == 2
        cards = {d["id"]: d for d in body["documents"]}
        assert set(cards) == {f, other}
        # Each tag in its most used spelling; a tie shows the first alphabetically.
        assert sorted(cards[f]["source_tags"]) == ["Web", "consensus"]

    def test_a_duplicates_tag_finds_the_canonical(self, pair, client):
        f, _, _ = pair
        r = client.get("/documents", params=[("source_tags", "consensus")])
        assert [d["id"] for d in r.json()["documents"]] == [f]

    def test_a_duplicates_source_finds_the_canonical(self, pair, client):
        f, _, _ = pair
        r = client.get("/documents", params=[("sources", "zotero")])
        assert [d["id"] for d in r.json()["documents"]] == [f]

    def test_an_overlay_tag_on_the_duplicate_finds_the_canonical(self, pair, client):
        f, z, _ = pair
        sync_overlay_tags(z, ["Thesis"], TagOrigin.COLLECTION)
        r = client.get("/documents", params=[("collection_tags", "Thesis")])
        assert [d["id"] for d in r.json()["documents"]] == [f]

    def test_tag_counts_count_the_item_once(self, pair):
        rows = {r["tag"]: r["count"] for r in list_tags(origin="source")}
        assert rows == {"Web": 1, "consensus": 1}

    def test_the_detail_panel_lists_the_other_copy(self, pair, client):
        f, z, _ = pair
        also = client.get(f"/documents/{f}").json()["also_saved_in"]
        assert [(c["id"], c["source"]) for c in also] == [(z, "zotero")]
        # Opened directly, the duplicate lists its canonical.
        assert [c["id"] for c in client.get(f"/documents/{z}").json()["also_saved_in"]] == [f]

    def test_search_hits_fold_into_the_canonical(self, pair):
        from pka.api.search_hits import fold_duplicates

        f, z, other = pair
        with get_engine().connect() as con:
            hits = fold_duplicates(con, [(z, 0.9), (other, 0.5), (f, 0.4), (other, None)])
        assert hits == [(f, 0.9), (other, 0.5)]

    def test_clustering_and_tag_training_skip_the_duplicate(self, pair):
        from pka.clustering.embeddings import _candidate_document_ids
        from pka.tag_training.engine import unlabeled_doc_ids

        f, z, other = pair
        _chunk(f)
        _chunk(z)
        assert _candidate_document_ids(None) == [f]
        assert z not in unlabeled_doc_ids(session_id=999)

    def test_undoing_the_link_restores_both(self, pair, client):
        (row,) = duplicates.list_links("merged")
        duplicates.reject(row["id"])
        assert client.get("/documents").json()["total"] == 3


class TestApi:
    def test_scan_list_accept_reject(self, db, client):
        a = make_document("zotero", "Z1", "Paper", doi="10.1/x")
        b = make_document("firefox", "F1", "Page", doi="10.1/x")
        r = client.post("/duplicates/scan", json={"embeddings": False})
        assert r.json() == {"linked": 1, "by_key": {"doi": 1}, "proposed": 0}
        (link,) = client.get("/duplicates", params={"state": "merged"}).json()
        assert {link["canonical"]["id"], link["duplicate"]["id"]} == {a, b}
        assert client.post(f"/duplicates/{link['id']}/reject").status_code == 204
        assert client.post(f"/duplicates/{link['id']}/accept").json()["state"] == "merged"
        assert client.post("/duplicates/999/reject").status_code == 404

    def test_manual_link_and_self_link(self, db, client):
        a = make_document("firefox", "F1", "A")
        b = make_document("firefox", "F2", "B")
        r = client.post("/duplicates", json={"canonical_id": a, "duplicate_id": b})
        assert r.json()["match_key"] == "manual"
        assert (
            client.post("/duplicates", json={"canonical_id": a, "duplicate_id": a}).status_code
            == 422
        )
