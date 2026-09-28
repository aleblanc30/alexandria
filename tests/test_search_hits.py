"""Unit tests for the ``/search`` stages in :mod:`pka.api.search_hits`.

These exercise the stages directly, without FastAPI. ``tests/test_api.py``'s
``TestSearch`` still covers the endpoint end to end; what lives here is the
ordering and merge behaviour that is hard to pin through HTTP, and that an
extraction can break while still returning a plausible-looking result list.
"""

import pytest

from pka.api.schemas.search import SearchRequest
from pka.api.search_hits import (
    _MAX_SEMANTIC_HITS,
    apply_browse_filters,
    apply_row_filters,
    fulltext_hits,
    merge_clip_hits,
    merge_new,
    semantic_hits,
)
from pka.db.queries import get_engine, init_db
from tests.conftest import make_document


@pytest.fixture()
def con(empty_vector_store):
    """A connection to a freshly initialised archive."""
    init_db()
    with get_engine().connect() as connection:
        yield connection


def _req(**kw) -> SearchRequest:
    return SearchRequest(**{"query": "alpha", **kw})


def _chroma_hit(doc_id: int, distance: float) -> dict:
    return {"metadata": {"document_id": doc_id}, "distance": distance}


# ── semantic_hits ─────────────────────────────────────────────────────────────


class TestSemanticHits:
    def test_collapses_chunks_to_best_similarity_per_document(self, monkeypatch):
        """Several chunks of one document yield one row, at the highest score."""
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda *a, **k: [_chroma_hit(7, 0.6), _chroma_hit(7, 0.1), _chroma_hit(9, 0.4)],
        )
        assert semantic_hits(_req()) == [(7, pytest.approx(0.9)), (9, pytest.approx(0.6))]

    def test_sorted_by_similarity_descending(self, monkeypatch):
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda *a, **k: [_chroma_hit(1, 0.9), _chroma_hit(2, 0.2), _chroma_hit(3, 0.5)],
        )
        assert [d for d, _ in semantic_hits(_req())] == [2, 3, 1]

    def test_vector_store_failure_returns_empty_rather_than_raising(self, monkeypatch):
        """The fulltext fallback depends on this degrading instead of 500ing."""

        def _boom(*a, **k):
            raise RuntimeError("chroma is down")

        monkeypatch.setattr("pka.storage.vector_store.query", _boom)
        assert semantic_hits(_req()) == []

    def test_source_filter_becomes_a_chroma_where_clause(self, monkeypatch):
        seen: dict = {}

        def _capture(query, n_results, where=None):
            seen["where"] = where
            return []

        monkeypatch.setattr("pka.storage.vector_store.query", _capture)
        semantic_hits(_req(sources=["zotero", "firefox"]))
        assert seen["where"] == {"source": {"$in": ["zotero", "firefox"]}}

    def test_over_fetches_three_times_the_page(self, monkeypatch):
        seen: dict = {}

        def _capture(query, n_results, where=None):
            seen["n"] = n_results
            return []

        monkeypatch.setattr("pka.storage.vector_store.query", _capture)
        semantic_hits(_req(offset=40, limit=20))
        assert seen["n"] == 180

    def test_over_fetch_is_capped_at_deep_offsets(self, monkeypatch):
        """Without the ceiling this would ask Chroma for 6060 hits to return 20."""
        seen: dict = {}

        def _capture(query, n_results, where=None):
            seen["n"] = n_results
            return []

        monkeypatch.setattr("pka.storage.vector_store.query", _capture)
        semantic_hits(_req(offset=2000, limit=20))
        assert seen["n"] == _MAX_SEMANTIC_HITS

    def test_over_fetch_cap_also_bounds_a_large_limit(self, monkeypatch):
        seen: dict = {}

        def _capture(query, n_results, where=None):
            seen["n"] = n_results
            return []

        monkeypatch.setattr("pka.storage.vector_store.query", _capture)
        semantic_hits(_req(limit=100_000))
        assert seen["n"] == _MAX_SEMANTIC_HITS

    def test_no_source_filter_passes_where_none(self, monkeypatch):
        seen: dict = {}

        def _capture(query, n_results, where=None):
            seen["where"] = where
            return []

        monkeypatch.setattr("pka.storage.vector_store.query", _capture)
        semantic_hits(_req())
        assert seen["where"] is None


# ── fulltext_hits / merge_new ─────────────────────────────────────────────────


class TestFulltextHits:
    def test_matches_title_substring_case_insensitively(self, con):
        hit = make_document("zotero", "K1", "Alpha Centauri")
        make_document("zotero", "K2", "Something else")
        assert fulltext_hits(con, _req(query="alpha")) == [(hit, None)]

    def test_similarity_is_none_not_zero(self, con):
        """``None`` and ``0.0`` are not interchangeable downstream."""
        make_document("zotero", "K1", "Alpha")
        ((_, sim),) = fulltext_hits(con, _req())
        assert sim is None

    def test_equally_ranked_matches_keep_document_id_order(self, con):
        first = make_document("zotero", "K1", "Alpha one")
        second = make_document("firefox", "K2", "Alpha two")
        assert [d for d, _ in fulltext_hits(con, _req())] == sorted([first, second])

    def test_a_title_match_ranks_ahead_of_a_body_only_match(self, con):
        from pka.db.chunks import insert_chunks

        body_only = make_document("zotero", "K1", "Unrelated title")
        insert_chunks(
            [
                {
                    "document_id": body_only,
                    "chunk_index": 0,
                    "text": "The alpha particle appears here.",
                    "token_count": 5,
                    "vector_id": "v1",
                }
            ]
        )
        titled = make_document("zotero", "K2", "Alpha particles")
        assert [d for d, _ in fulltext_hits(con, _req())] == [titled, body_only]

    def test_a_short_query_falls_back_to_the_title_scan(self, con):
        """Trigram cannot match under three characters."""
        hit = make_document("zotero", "K1", "Go programming")
        make_document("zotero", "K2", "Rust")
        assert fulltext_hits(con, _req(query="go")) == [(hit, None)]

    def test_source_filter_applied(self, con):
        keep = make_document("zotero", "K1", "Alpha one")
        make_document("firefox", "K2", "Alpha two")
        assert fulltext_hits(con, _req(sources=["zotero"])) == [(keep, None)]


class TestMergeNew:
    def test_appends_only_unseen_document_ids(self):
        assert merge_new([(1, 0.9)], [(1, None), (2, None)]) == [(1, 0.9), (2, None)]

    def test_preserves_base_order_and_scores(self):
        base = [(3, 0.5), (1, 0.2)]
        assert merge_new(base, [(9, None)]) == [(3, 0.5), (1, 0.2), (9, None)]

    def test_empty_base_keeps_extra_verbatim(self):
        assert merge_new([], [(4, None), (5, None)]) == [(4, None), (5, None)]


# ── merge_clip_hits ───────────────────────────────────────────────────────────


def _clip(monkeypatch, hits, *, record=None):
    def _fake(q, n=10):
        if record is not None:
            record.append(n)
        return hits

    monkeypatch.setattr("pka.ingestion.image_pipeline.search_images_by_text", _fake)


class TestMergeClipHits:
    def test_visual_hit_added_with_its_similarity(self, monkeypatch):
        _clip(monkeypatch, [{"document_id": 5, "distance": 0.2}])
        assert merge_clip_hits([], _req()) == [(5, pytest.approx(0.8))]

    def test_existing_document_keeps_the_higher_score(self, monkeypatch):
        _clip(monkeypatch, [{"document_id": 5, "distance": 0.9}])
        assert merge_clip_hits([(5, 0.7)], _req()) == [(5, pytest.approx(0.7))]

    def test_clip_score_wins_when_higher(self, monkeypatch):
        _clip(monkeypatch, [{"document_id": 5, "distance": 0.1}])
        assert merge_clip_hits([(5, 0.4)], _req()) == [(5, pytest.approx(0.9))]

    def test_scored_entries_precede_unscored_ones(self, monkeypatch):
        """A CLIP hit reorders a fulltext result list. This is intended."""
        _clip(monkeypatch, [{"document_id": 20, "distance": 0.3}])
        merged = merge_clip_hits([(10, None), (20, None), (30, None)], _req())
        assert merged == [(20, pytest.approx(0.7)), (10, None), (30, None)]

    def test_unscored_entries_keep_their_relative_order(self, monkeypatch):
        _clip(monkeypatch, [{"document_id": 99, "distance": 0.5}])
        merged = merge_clip_hits([(3, None), (1, None), (2, None)], _req())
        assert [d for d, s in merged if s is None] == [3, 1, 2]

    def test_skipped_when_images_out_of_scope(self, monkeypatch):
        record: list = []
        _clip(monkeypatch, [{"document_id": 5, "distance": 0.1}], record=record)
        results = [(1, 0.5)]
        assert merge_clip_hits(results, _req(sources=["zotero"])) == results
        assert record == []  # not merely filtered afterwards; never called

    def test_runs_when_image_source_explicitly_requested(self, monkeypatch):
        _clip(monkeypatch, [{"document_id": 5, "distance": 0.2}])
        assert merge_clip_hits([], _req(sources=["image"])) == [(5, pytest.approx(0.8))]

    def test_skipped_for_a_blank_query(self, monkeypatch):
        record: list = []
        _clip(monkeypatch, [{"document_id": 5, "distance": 0.1}], record=record)
        assert merge_clip_hits([(1, 0.5)], _req(query="   ")) == [(1, 0.5)]
        assert record == []

    def test_clip_failure_leaves_results_untouched(self, monkeypatch):
        def _boom(q, n=10):
            raise RuntimeError("clip is down")

        monkeypatch.setattr("pka.ingestion.image_pipeline.search_images_by_text", _boom)
        assert merge_clip_hits([(1, 0.5)], _req()) == [(1, 0.5)]

    def test_hit_without_a_document_id_is_ignored(self, monkeypatch):
        _clip(monkeypatch, [{"document_id": None, "distance": 0.1}])
        assert merge_clip_hits([(1, 0.5)], _req()) == [(1, 0.5)]


# ── apply_browse_filters ──────────────────────────────────────────────────────


class TestApplyBrowseFilters:
    def test_no_filters_is_a_passthrough(self, con):
        results = [(1, 0.5), (2, None)]
        assert apply_browse_filters(con, results, _req()) == results

    def test_source_filter_drops_non_matching(self, con):
        keep = make_document("zotero", "K1", "Alpha")
        drop = make_document("firefox", "K2", "Alpha")
        got = apply_browse_filters(con, [(keep, 0.9), (drop, 0.8)], _req(sources=["zotero"]))
        assert got == [(keep, pytest.approx(0.9))]

    def test_order_and_similarity_preserved(self, con):
        a = make_document("zotero", "K1", "Alpha")
        b = make_document("zotero", "K2", "Alpha")
        results = [(b, 0.3), (a, 0.9)]
        assert apply_browse_filters(con, results, _req(sources=["zotero"])) == results

    def test_empty_results_short_circuits(self, con):
        assert apply_browse_filters(con, [], _req(sources=["zotero"])) == []


# ── apply_row_filters ─────────────────────────────────────────────────────────


class TestApplyRowFilters:
    def test_no_filters_is_a_passthrough(self, con):
        results = [(1, 0.5), (2, None)]
        assert apply_row_filters(con, results, _req(), None) == results

    def test_fetch_status_filter(self, con):
        keep = make_document("zotero", "K1", "Alpha", fetch_status="fetched")
        drop = make_document("zotero", "K2", "Alpha", fetch_status="pending")
        got = apply_row_filters(con, [(keep, 0.9), (drop, 0.8)], _req(fetch_status="fetched"), None)
        assert got == [(keep, pytest.approx(0.9))]

    def test_date_range_is_inclusive_at_both_ends(self, con):
        early = make_document("zotero", "K1", "Alpha", date_added=100)
        mid = make_document("zotero", "K2", "Alpha", date_added=200)
        late = make_document("zotero", "K3", "Alpha", date_added=300)
        results = [(early, None), (mid, None), (late, None)]
        got = apply_row_filters(con, results, _req(date_from=200, date_to=300), None)
        assert [d for d, _ in got] == [mid, late]

    def test_missing_date_added_treated_as_zero(self, con):
        doc = make_document("zotero", "K1", "Alpha", date_added=None)
        assert apply_row_filters(con, [(doc, None)], _req(date_from=1), None) == []

    def test_hit_with_no_document_row_is_dropped(self, con):
        assert apply_row_filters(con, [(9999, 0.5)], _req(fetch_status="fetched"), None) == []

    def test_cluster_ids_without_an_active_run_drops_everything(self, con):
        """No run means no membership to test, so the filter excludes rather
        than silently matching everything."""
        doc = make_document("zotero", "K1", "Alpha")
        assert apply_row_filters(con, [(doc, 0.5)], _req(cluster_ids=[1]), None) == []

    def test_order_preserved_across_filtering(self, con):
        a = make_document("zotero", "K1", "Alpha", fetch_status="fetched")
        b = make_document("zotero", "K2", "Alpha", fetch_status="fetched")
        results = [(b, 0.2), (a, 0.8)]
        assert apply_row_filters(con, results, _req(fetch_status="fetched"), None) == results
