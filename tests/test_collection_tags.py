"""Zotero collections and Firefox folders as ``collection`` overlay tags."""

import pytest
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import Source, TagOrigin
from pka.db.engine import get_engine
from pka.db.migrate import init_db
from pka.db.schema import overlay_tags, source_collections
from pka.db.tags import insert_source_collections, list_tags, sync_overlay_tags
from pka.ingestion.collection_tags import (
    backfill_collection_tags,
    normalize_collection_tags,
    sync_collection_tags,
)
from tests.conftest import make_document
from tests.test_pipeline import _make_firefox_bookmark, _make_zotero_item


def _tags(doc_id: int, origin: str = TagOrigin.COLLECTION) -> set[str]:
    with get_engine().connect() as con:
        return {
            r[0]
            for r in con.execute(
                sa.select(overlay_tags.c.tag).where(
                    (overlay_tags.c.document_id == doc_id) & (overlay_tags.c.origin == str(origin))
                )
            )
        }


@pytest.fixture()
def db():
    init_db()


class TestNormalize:
    @pytest.mark.parametrize(
        ("paths", "expected"),
        [
            (["toolbar/Research/Distributed Systems"], ["Research", "Distributed Systems"]),
            (["menu/Reading"], ["Reading"]),
            (["root/unfiled/Later"], ["Later"]),
            (["mobile"], []),
            # A folder named like a root is kept when it is not at the top.
            (["toolbar/Projects/menu/Cafés"], ["Projects", "menu", "Cafés"]),
            (["Research/toolbar"], ["Research", "toolbar"]),
            # Untitled folders join as empty segments.
            (["toolbar//Research///Web"], ["Research", "Web"]),
            (["toolbar/ Spaced  /x/Ok"], ["Spaced", "Ok"]),
            (["toolbar/a/b/c/d/e/f"], []),
            (["toolbar/One/Two/Three/Four/Five/Six"], ["One", "Two", "Three", "Four"]),
            (["toolbar/ML/Deep", "menu/ml/Other"], ["ML", "Deep", "Other"]),
            ([""], []),
        ],
    )
    def test_firefox_paths(self, paths, expected):
        assert normalize_collection_tags(paths, Source.FIREFOX, max_depth=4) == expected

    @pytest.mark.parametrize(
        ("paths", "expected"),
        [
            (["Thesis/Chapter 2"], ["Thesis", "Chapter 2"]),
            (["Reading list"], ["Reading list"]),
            # Zotero has no structural roots: a collection named "menu" is a tag.
            (["menu"], ["menu"]),
            (["Thesis/Chapter 2", "Thesis/Chapter 3"], ["Thesis", "Chapter 2", "Chapter 3"]),
        ],
    )
    def test_zotero_paths(self, paths, expected):
        assert normalize_collection_tags(paths, Source.ZOTERO, max_depth=4) == expected

    def test_depth_defaults_to_the_setting(self, monkeypatch):
        monkeypatch.setattr(cfg, "collection_tag_max_depth", 1)
        assert normalize_collection_tags(["Thesis/Chapter 2"], Source.ZOTERO) == ["Thesis"]

    @pytest.mark.parametrize("source", [Source.CALIBRE, Source.REDDIT, Source.YOUTUBE])
    def test_other_sources_are_not_tagged(self, source):
        assert normalize_collection_tags(["Some Series"], source) == []

    def test_excluded_names_are_skipped_but_their_subfolders_kept(self, monkeypatch):
        monkeypatch.setattr(cfg, "collection_tag_exclude", ["imported", "Misc"])
        assert normalize_collection_tags(
            ["toolbar/Imported/Research", "menu/misc"], Source.FIREFOX, max_depth=4
        ) == ["Research"]


class TestExcludeSetting:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Imported, Misc ,", ["Imported", "Misc"]),
            ('["Imported", "Other Bookmarks"]', ["Imported", "Other Bookmarks"]),
            ("", []),
        ],
    )
    def test_parsed_from_env(self, raw, expected, monkeypatch):
        from pka.config import Settings

        monkeypatch.setenv("ALEXANDRIA_COLLECTION_TAG_EXCLUDE", raw)
        assert Settings(_env_file=None).collection_tag_exclude == expected


class TestSync:
    def test_tags_follow_the_collections(self, db):
        doc = make_document("zotero", "Z1")
        sync_collection_tags(doc, ["Thesis/Chapter 2"], Source.ZOTERO)
        assert _tags(doc) == {"Thesis", "Chapter 2"}

        # Renamed: the old tag goes, the new one comes.
        sync_collection_tags(doc, ["Thesis/Chapter Two"], Source.ZOTERO)
        assert _tags(doc) == {"Thesis", "Chapter Two"}

        sync_collection_tags(doc, [], Source.ZOTERO)
        assert _tags(doc) == set()

    def test_a_rerun_changes_nothing(self, db):
        doc = make_document("zotero", "Z1")
        sync_collection_tags(doc, ["Thesis"], Source.ZOTERO)
        with get_engine().connect() as con:
            before = con.execute(sa.select(overlay_tags.c.id, overlay_tags.c.tag)).fetchall()
        sync_collection_tags(doc, ["Thesis"], Source.ZOTERO)
        with get_engine().connect() as con:
            after = con.execute(sa.select(overlay_tags.c.id, overlay_tags.c.tag)).fetchall()
        assert before == after

    def test_other_origins_with_the_same_string_survive(self, db):
        doc = make_document("zotero", "Z1")
        sync_overlay_tags(doc, ["Thesis"], TagOrigin.MANUAL)
        sync_collection_tags(doc, ["Thesis"], Source.ZOTERO)
        sync_collection_tags(doc, [], Source.ZOTERO)
        assert _tags(doc, TagOrigin.MANUAL) == {"Thesis"}
        assert _tags(doc) == set()

    def test_disabled_writes_nothing(self, db, monkeypatch):
        monkeypatch.setattr(cfg, "collection_tags_enabled", False)
        doc = make_document("zotero", "Z1")
        sync_collection_tags(doc, ["Thesis"], Source.ZOTERO)
        assert _tags(doc) == set()

    def test_sync_within_leaves_the_rest_of_an_origin_alone(self, db):
        doc = make_document("firefox", "F1")
        sync_overlay_tags(doc, ["photo"], TagOrigin.INFERRED)
        sync_overlay_tags(doc, ["paper"], TagOrigin.INFERRED, within={"paper", "preprint"})
        sync_overlay_tags(doc, [], TagOrigin.INFERRED, within={"paper", "preprint"})
        assert _tags(doc, TagOrigin.INFERRED) == {"photo"}


class TestRunners:
    def test_zotero_ingest_tags_its_collections(self, db, mock_chroma):
        from pka.ingestion.runners.zotero import ingest_zotero_items

        ingest_zotero_items([_make_zotero_item(collections=["CS/Distributed Systems"])])
        doc = make_document("zotero", "Z001")
        assert _tags(doc) == {"CS", "Distributed Systems"}

    def test_zotero_metadata_tags_its_collections(self, db):
        from pka.ingestion.runners.zotero import ingest_zotero_metadata

        ingest_zotero_metadata([_make_zotero_item(collections=["Thesis"])])
        assert _tags(make_document("zotero", "Z001")) == {"Thesis"}

    def test_firefox_metadata_tags_its_folders(self, db):
        from pka.ingestion.runners.firefox import ingest_firefox_bookmarks

        ingest_firefox_bookmarks([_make_firefox_bookmark(folder_path="toolbar/Research/Web")])
        assert _tags(make_document("firefox", "F001")) == {"Research", "Web"}

    def test_a_sync_refreshes_the_collections_of_archived_items(self, db):
        """The metadata loop skips known items; the refresh pass must not."""
        from pka.ingestion.runners.zotero import (
            ingest_zotero_metadata,
            refresh_zotero_collections,
        )

        ingest_zotero_metadata([_make_zotero_item(collections=["Distributed Systems"])])
        moved = _make_zotero_item(collections=["CS/Distributed Systems"])
        unknown = _make_zotero_item(source_id="NEW1", collections=["Other"])

        assert refresh_zotero_collections([moved, unknown]) == 1
        doc = make_document("zotero", "Z001")
        assert _tags(doc) == {"CS", "Distributed Systems"}
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(source_collections.c.collection).where(
                    source_collections.c.document_id == doc
                )
            ).fetchall()
        assert [r[0] for r in rows] == ["CS/Distributed Systems"]

    def test_an_item_leaving_its_last_collection_loses_rows_and_tags(self, db, mock_chroma):
        from pka.ingestion.runners.zotero import ingest_zotero_items

        ingest_zotero_items([_make_zotero_item(collections=["Thesis"])])
        ingest_zotero_items([_make_zotero_item(collections=[])], skip_existing=True)
        doc = make_document("zotero", "Z001")
        assert _tags(doc) == set()
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(source_collections.c.id).where(source_collections.c.document_id == doc)
            ).fetchall()
        assert rows == []


def _collections(doc_id: int, source: Source, paths: list[str]) -> None:
    insert_source_collections(doc_id, paths, source)


class TestBackfill:
    @pytest.fixture()
    def archive(self, db):
        z = make_document("zotero", "Z1")
        f = make_document("firefox", "F1")
        c = make_document("calibre", "C1")
        _collections(z, Source.ZOTERO, ["Thesis/Chapter 2"])
        _collections(f, Source.FIREFOX, ["toolbar/Research"])
        _collections(c, Source.CALIBRE, ["Discworld"])
        return z, f, c

    def test_tags_every_document_from_its_stored_collections(self, archive):
        z, f, c = archive
        stats = backfill_collection_tags()
        assert _tags(z) == {"Thesis", "Chapter 2"}
        assert _tags(f) == {"Research"}
        assert _tags(c) == set()
        assert stats["sources"]["zotero"] == {
            "documents": 1,
            "tags": 2,
            "distinct": 2,
            "top": [("Thesis", 1), ("Chapter 2", 1)],
        }

    def test_dry_run_writes_nothing(self, archive):
        z, f, _ = archive
        stats = backfill_collection_tags(dry_run=True)
        assert stats["sources"]["firefox"]["documents"] == 1
        assert _tags(z) == set() and _tags(f) == set()

    def test_is_idempotent(self, archive):
        backfill_collection_tags()
        with get_engine().connect() as con:
            before = con.execute(sa.select(overlay_tags.c.id)).fetchall()
        backfill_collection_tags()
        with get_engine().connect() as con:
            after = con.execute(sa.select(overlay_tags.c.id)).fetchall()
        assert before == after

    def test_source_limits_it(self, archive):
        z, f, _ = archive
        stats = backfill_collection_tags(Source.FIREFOX)
        assert set(stats["sources"]) == {"firefox"}
        assert _tags(f) == {"Research"} and _tags(z) == set()

    def test_a_document_whose_collections_are_gone_loses_its_tags(self, archive):
        z, _, _ = archive
        backfill_collection_tags()
        insert_source_collections(z, [], Source.ZOTERO)
        backfill_collection_tags()
        assert _tags(z) == set()

    def test_a_tag_over_the_document_cap_is_dropped_everywhere(self, db, monkeypatch):
        """Counted across both sources; the ingest path then agrees with the backfill."""
        monkeypatch.setattr(cfg, "collection_tag_max_documents", 2)
        z = make_document("zotero", "Z1")
        f1 = make_document("firefox", "F1")
        f2 = make_document("firefox", "F2")
        _collections(z, Source.ZOTERO, ["Reading/Raft"])
        _collections(f1, Source.FIREFOX, ["toolbar/Reading"])
        _collections(f2, Source.FIREFOX, ["toolbar/reading/Paxos"])

        stats = backfill_collection_tags(Source.FIREFOX)

        assert stats["capped"] == [("reading", 3)]
        assert _tags(f1) == set() and _tags(f2) == {"Paxos"}
        assert _tags(z) == set()  # outside --source: untouched

        new = make_document("firefox", "F3")
        _collections(new, Source.FIREFOX, ["toolbar/Reading/Other"])
        sync_collection_tags(new, ["toolbar/Reading/Other"], Source.FIREFOX)
        assert _tags(new) == {"Other"}

    def test_a_cap_of_zero_keeps_everything(self, archive, monkeypatch):
        monkeypatch.setattr(cfg, "collection_tag_max_documents", 0)
        assert backfill_collection_tags()["capped"] == []

    def test_disabled_removes_them(self, archive, monkeypatch):
        z, f, _ = archive
        backfill_collection_tags()
        monkeypatch.setattr(cfg, "collection_tags_enabled", False)
        stats = backfill_collection_tags()
        assert stats["enabled"] is False
        assert _tags(z) == set() and _tags(f) == set()


class TestQueries:
    @pytest.fixture()
    def tagged(self, db):
        z = make_document("zotero", "Z1", "Raft")
        f = make_document("firefox", "F1", "Paxos")
        other = make_document("firefox", "F2", "Cooking")
        sync_collection_tags(z, ["Distributed/Consensus"], Source.ZOTERO)
        sync_collection_tags(f, ["toolbar/Distributed"], Source.FIREFOX)
        sync_collection_tags(other, ["toolbar/Recipes"], Source.FIREFOX)
        return z, f, other

    def test_list_tags_includes_collection_tags(self, tagged):
        rows = list_tags(origin="collection")
        assert {(r["tag"], r["count"]) for r in rows} == {
            ("Distributed", 2),
            ("Consensus", 1),
            ("Recipes", 1),
        }
        assert {"Distributed", "Consensus"} <= {r["tag"] for r in list_tags()}

    def test_list_tags_is_scoped_by_a_collection_filter(self, tagged):
        rows = list_tags(origin="collection", collection_tag_filter=["Consensus"])
        assert {r["tag"] for r in rows} == {"Distributed", "Consensus"}

    def test_browse_filters_by_collection_tag(self, tagged, client):
        z, f, _ = tagged
        r = client.get("/documents", params=[("collection_tags", "Distributed")])
        assert {d["id"] for d in r.json()["documents"]} == {z, f}
        r = client.get(
            "/documents", params=[("collection_tags", "Distributed"), ("sources", "firefox")]
        )
        assert [d["id"] for d in r.json()["documents"]] == [f]

    def test_tags_endpoint_takes_the_origin_and_filter(self, tagged, client):
        r = client.get("/tags", params=[("origin", "collection"), ("collection_tags", "Recipes")])
        assert [row["tag"] for row in r.json()] == ["Recipes"]

    def test_search_hits_are_filtered_by_collection_tag(self, tagged):
        from pka.api.schemas.search import SearchRequest
        from pka.api.search_hits import apply_browse_filters

        z, f, other = tagged
        req = SearchRequest(query="x", collection_tags=["Distributed"])
        with get_engine().connect() as con:
            hits = apply_browse_filters(con, [(other, 0.9), (f, 0.8), (z, 0.7)], req)
        assert hits == [(f, 0.8), (z, 0.7)]
