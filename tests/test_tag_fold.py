"""Tag folding: the normalised key, aliases, the scan and every read site."""

import numpy as np
import pytest
import sqlalchemy as sa

from pka.constants import TagOrigin
from pka.db import tag_aliases
from pka.db.engine import get_engine
from pka.db.migrate import init_db
from pka.db.schema import tag_aliases as tag_aliases_tbl
from pka.db.tag_fold import fold_map, tag_key
from pka.db.tags import insert_source_tags, list_tags, sync_overlay_tags
from tests.conftest import make_document


@pytest.fixture()
def db():
    init_db()


def _doc(key: str, source_tags: list[str] = (), source: str = "zotero") -> int:
    doc = make_document(source, key, f"Doc {key}")
    if source_tags:
        insert_source_tags(doc, list(source_tags), source)
    return doc


def _row(rows, tag, origin="source"):
    return next(r for r in rows if r["tag"] == tag and r["origin"] == origin)


class TestKey:
    @pytest.mark.parametrize(
        ("raw", "key"),
        [
            ("Machine Learning", "machine-learning"),
            ("machine-learning", "machine-learning"),
            ("MACHINE_LEARNING", "machine-learning"),
            ("  machine   learning ", "machine-learning"),
            ("Économie", "economie"),
            ("Aprendizaje automático", "aprendizaje-automatico"),
            ("NASA", "nasa"),
            ("C++", "c"),
            ("???", ""),
            ("x" * 70, "x" * 64),
        ],
    )
    def test_normalisation(self, raw, key):
        assert tag_key(raw) == key


class TestListTags:
    def test_spellings_fold_into_one_row_counting_documents_once(self, db):
        both = _doc("A", ["Machine Learning", "machine-learning"])
        _doc("B", ["machine-learning"], source="firefox")
        _doc("C", ["machine learning"])
        rows = list_tags(origin="source")
        row = _row(rows, "machine-learning")
        assert row["count"] == 3  # document A carries two spellings, counted once
        assert set(row["variants"]) == {"Machine Learning", "machine-learning", "machine learning"}
        assert both

    def test_the_display_form_is_the_most_used_spelling(self, db):
        _doc("A", ["NASA"])
        _doc("B", ["NASA"])
        _doc("C", ["nasa"])
        assert [r["tag"] for r in list_tags(origin="source")] == ["NASA"]

    def test_origins_are_never_folded_together(self, db):
        doc = _doc("A", ["Deep Learning"])
        sync_overlay_tags(doc, ["deep-learning"], TagOrigin.MANUAL)
        rows = list_tags()
        assert {(r["tag"], r["origin"]) for r in rows} == {
            ("Deep Learning", "source"),
            ("deep-learning", "manual"),
        }

    def test_an_alias_folds_two_keys(self, db):
        _doc("A", ["ml"])
        _doc("B", ["Machine Learning"])
        _doc("C", ["Machine Learning"])
        tag_aliases.merge("ml", "machine learning")
        rows = list_tags(origin="source")
        assert len(rows) == 1
        assert rows[0]["tag"] == "Machine Learning" and rows[0]["count"] == 3

    def test_aliases_do_not_touch_inferred_tags(self, db):
        doc = _doc("A")
        sync_overlay_tags(doc, ["paper", "preprint"], TagOrigin.INFERRED)
        tag_aliases.merge("paper", "preprint")
        assert {r["tag"] for r in list_tags(origin="inferred")} == {"paper", "preprint"}

    def test_the_text_filter_matches_any_spelling(self, db):
        _doc("A", ["Économie"])
        _doc("B", ["economie"])
        rows = list_tags(q="économie")
        assert len(rows) == 1 and rows[0]["count"] == 2

    def test_counts_are_scoped_by_the_browse_filters(self, db):
        _doc("A", ["Physics", "physics"], source="zotero")
        _doc("B", ["physics"], source="firefox")
        rows = list_tags(origin="source", sources=["zotero"])
        # The display form stays the archive-wide choice; the count follows the scope.
        assert [(r["tag"], r["count"]) for r in rows] == [("physics", 1)]


class TestReadSites:
    def test_a_source_tag_filter_matches_every_spelling(self, db, client):
        a = _doc("A", ["Machine Learning"])
        b = _doc("B", ["machine-learning"])
        _doc("C", ["Physics"])
        r = client.get("/documents", params=[("source_tags", "machine learning")])
        assert {d["id"] for d in r.json()["documents"]} == {a, b}

    def test_a_filter_follows_an_alias(self, db, client):
        a = _doc("A", ["ml"])
        b = _doc("B", ["machine-learning"])
        tag_aliases.merge("ml", "machine-learning")
        r = client.get("/documents", params=[("source_tags", "machine-learning")])
        assert {d["id"] for d in r.json()["documents"]} == {a, b}

    def test_an_overlay_filter_matches_every_spelling_of_its_origin(self, db, client):
        a = _doc("A")
        b = _doc("B")
        sync_overlay_tags(a, ["Thesis"], TagOrigin.COLLECTION)
        sync_overlay_tags(b, ["thesis"], TagOrigin.COLLECTION)
        r = client.get("/documents", params=[("collection_tags", "Thesis")])
        assert {d["id"] for d in r.json()["documents"]} == {a, b}

    def test_card_chips_show_one_chip_per_tag(self, db, client):
        _doc("A", ["Machine Learning", "machine-learning", "Physics"])
        (doc,) = client.get("/documents").json()["documents"]
        assert sorted(doc["source_tags"]) == ["Machine Learning", "Physics"]

    def test_a_training_seed_takes_every_spelling(self, db):
        from pka.tag_training.lifecycle import document_ids_for_source_tag

        a = _doc("A", ["Machine Learning"])
        b = _doc("B", ["machine-learning"])
        assert sorted(document_ids_for_source_tag("machine learning")) == sorted([a, b])

    def test_the_map_is_cached_until_invalidated(self, db, monkeypatch):
        import pka.db.tag_fold as tf

        monkeypatch.setattr(tf, "_TTL_SECONDS", 3600.0)
        _doc("A", ["Physics"])
        first = fold_map()
        _doc("B", ["physics"])
        assert fold_map() is first
        tag_aliases.merge("physics", "science")
        assert fold_map() is not first


def _aliases(state=None):
    return {(r["alias"], r["canonical"], r["state"]) for r in tag_aliases.list_aliases(state)}


class TestAliases:
    def test_merge_takes_any_spelling_and_stores_keys(self, db):
        row = tag_aliases.merge("Machine Learning!", "ML")
        assert (row["alias"], row["canonical"], row["state"]) == (
            "machine-learning",
            "ml",
            "active",
        )

    def test_a_tag_cannot_fold_into_itself(self, db):
        with pytest.raises(tag_aliases.AliasError):
            tag_aliases.merge("Physics", "physics")

    def test_folds_stay_one_hop_deep(self, db):
        tag_aliases.merge("nn", "neural-nets")
        # Folding the canonical away repoints what was folded into it.
        tag_aliases.merge("neural-nets", "neural-networks")
        assert {(a, c) for a, c, s in _aliases("active")} == {
            ("nn", "neural-networks"),
            ("neural-nets", "neural-networks"),
        }
        # Folding into a key that is itself folded resolves to its canonical.
        tag_aliases.merge("ann", "nn")
        assert ("ann", "neural-networks", "active") in _aliases()

    def test_folding_both_ways_is_refused(self, db):
        tag_aliases.merge("ml", "machine-learning")
        with pytest.raises(tag_aliases.AliasError):
            tag_aliases.merge("machine-learning", "ml")

    def test_a_key_folds_one_way_at_a_time(self, db):
        tag_aliases.merge("ai", "artificial-intelligence")
        tag_aliases.merge("ai", "machine-learning")
        assert _aliases("active") == {("ai", "machine-learning", "active")}
        assert ("ai", "artificial-intelligence", "rejected") in _aliases()

    def test_accept_reject_and_unmerge(self, db):
        tag_aliases.add_candidates(
            [
                {"alias": "ml", "canonical": "machine-learning", "kind": "initialism"},
                {"alias": "ai", "canonical": "machine-learning", "kind": "semantic", "score": 0.93},
            ]
        )
        by_alias = {r["alias"]: r["id"] for r in tag_aliases.list_aliases("candidate")}
        tag_aliases.accept(by_alias["ml"])
        tag_aliases.reject(by_alias["ai"])
        assert _aliases() == {
            ("ml", "machine-learning", "active"),
            ("ai", "machine-learning", "rejected"),
        }
        assert tag_aliases.unmerge("ML")
        assert _aliases("active") == set()
        assert not tag_aliases.unmerge("ml")

    def test_known_pairs_are_not_proposed_again(self, db):
        tag_aliases.merge("ml", "machine-learning")
        added = tag_aliases.add_candidates(
            [{"alias": "machine-learning", "canonical": "ml", "kind": "semantic"}]
        )
        assert added == 0

    def test_the_database_refuses_two_active_folds_of_one_key(self, db):
        with get_engine().begin() as con:
            con.execute(
                tag_aliases_tbl.insert(),
                [
                    {"alias": "a", "canonical": "b", "kind": "manual", "state": "active"},
                    {"alias": "a", "canonical": "c", "kind": "manual", "state": "rejected"},
                ],
            )
        with pytest.raises(sa.exc.IntegrityError), get_engine().begin() as con:
            con.execute(
                tag_aliases_tbl.update()
                .where(tag_aliases_tbl.c.canonical == "c")
                .values(state="active")
            )


class _Vectors:
    """A stand-in embedder: the given vectors, and a distinct axis for any other text.

    ``same=True`` gives every text one vector, so every pair is similar.
    """

    DIM = 32

    def __init__(self, table: dict[str, list[float]], *, same: bool = False):
        self.table = table
        self.same = same

    def embed_documents(self, texts):
        out = []
        for i, t in enumerate(texts):
            v = np.zeros(self.DIM)
            if self.same:
                v[0] = 1.0
            elif t in self.table:
                v[: len(self.table[t])] = self.table[t]
            else:
                v[8 + i] = 1.0
            out.append(v.tolist())
        return out


class TestScan:
    @pytest.fixture()
    def vocabulary(self, db):
        for i in range(3):
            _doc(f"ML{i}", ["Machine Learning"])
        _doc("AP1", ["Apprentissage automatique"])
        _doc("AP2", ["apprentissage-automatique"])
        _doc("NN1", ["neural-networks", "Neural Network"])
        _doc("NN2", ["neural-networks"])
        _doc("ML-short", ["ml"])
        _doc("PH", ["physics", "Physic"])
        _doc("PH2", ["physics"])

    def test_morphology_and_initialisms(self, vocabulary):
        from pka.tag_dedup import scan

        stats = scan(("morphology", "initialism"))
        assert stats["by_kind"] == {"morphology": 1, "initialism": 1}
        assert _aliases("candidate") == {
            ("neural-network", "neural-networks", "candidate"),
            ("ml", "machine-learning", "candidate"),
        }

    def test_semantic_pairs_above_the_threshold(self, vocabulary, monkeypatch):
        from pka.config import settings as cfg
        from pka.tag_dedup import scan

        monkeypatch.setattr(cfg, "tag_dedup_min_documents", 1)
        s = 1 / np.sqrt(2)
        embedder = _Vectors(
            {
                "machine learning": [1.0, 0.0, 0.0],
                "apprentissage automatique": [0.99, 0.141, 0.0],
                "neural networks": [s, s, 0.0],
            }
        )
        stats = scan(("semantic",), threshold=0.95, embedder=embedder)
        assert stats["proposed"] == 1
        (row,) = tag_aliases.list_aliases("candidate")
        # The more used tag is canonical.
        assert (row["alias"], row["canonical"]) == ("apprentissage-automatique", "machine-learning")
        assert row["score"] == pytest.approx(0.99, abs=0.01)

    def test_tags_below_the_document_minimum_are_not_compared(self, vocabulary, monkeypatch):
        from pka.config import settings as cfg
        from pka.tag_dedup import scan

        monkeypatch.setattr(cfg, "tag_dedup_min_documents", 3)
        embedder = _Vectors({}, same=True)  # every text identical: all would pair
        assert scan(("semantic",), threshold=0.5, embedder=embedder)["proposed"] == 0

    def test_dry_run_writes_nothing_and_rejected_pairs_stay_out(self, vocabulary):
        from pka.tag_dedup import scan

        assert scan(("initialism",), dry_run=True)["proposed"] == 1
        assert _aliases() == set()
        scan(("initialism",))
        (row,) = tag_aliases.list_aliases("candidate")
        tag_aliases.reject(row["id"])
        assert scan(("initialism",))["proposed"] == 0

    def test_a_folded_key_is_not_scanned(self, vocabulary):
        from pka.tag_dedup import scan

        tag_aliases.merge("ml", "machine-learning")
        assert scan(("initialism",))["proposed"] == 0

    def test_the_variant_report_lists_what_the_key_folds(self, vocabulary):
        from pka.tag_dedup import variant_report

        groups = {(g["origin"], g["tag"]): g["variants"] for g in variant_report()}
        # A tie in use: the alphabetically first spelling is shown.
        assert groups[("source", "Apprentissage automatique")] == [
            "Apprentissage automatique",
            "apprentissage-automatique",
        ]
        assert ("source", "Neural Network") not in groups  # singular and plural: two keys


class TestApi:
    def test_merge_list_and_undo(self, db, client):
        _doc("A", ["ml"])
        _doc("B", ["Machine Learning"])
        r = client.post("/tags/aliases", json={"alias": "ML", "canonical": "machine learning"})
        assert r.status_code == 200
        body = r.json()
        assert body["alias"]["key"] == "ml"
        assert body["canonical"]["label"] == "Machine Learning"

        (active,) = client.get("/tags/aliases", params={"state": "active"}).json()
        assert client.post(f"/tags/aliases/{active['id']}/reject").status_code == 204
        assert client.get("/tags/aliases", params={"state": "active"}).json() == []

    def test_merging_a_tag_into_itself_is_a_422(self, db, client):
        r = client.post("/tags/aliases", json={"alias": "Physics", "canonical": "physics"})
        assert r.status_code == 422

    def test_scan_then_accept_a_candidate_with_examples(self, db, client):
        _doc("A", ["ml"])
        _doc("B", ["machine-learning"])
        r = client.post("/tags/aliases/scan", json={"kinds": ["initialism"]})
        assert r.json()["proposed"] == 1
        (cand,) = client.get("/tags/aliases", params={"state": "candidate"}).json()
        assert cand["alias"]["examples"] == ["Doc A"]
        assert client.post(f"/tags/aliases/{cand['id']}/accept").json()["state"] == "active"
        assert client.post("/tags/aliases/999/accept").status_code == 404

    def test_variants_endpoint(self, db, client):
        _doc("A", ["NASA", "nasa"])
        (group,) = client.get("/tags/variants").json()
        assert group["variants"] == ["NASA", "nasa"]

    def test_tag_rows_carry_their_variants(self, db, client):
        _doc("A", ["NASA"])
        _doc("B", ["nasa"])
        (row,) = client.get("/tags", params={"origin": "source"}).json()
        assert sorted(row["variants"]) == ["NASA", "nasa"]
