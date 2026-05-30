"""Tests for cluster tag suggestion helpers."""
import time

from pka.clustering.tag_suggestions import (
    COVERAGE_THRESHOLD,
    TagCandidate,
    _TAG_CACHE,
    build_tag_suggestions,
    pick_suggested_tag,
    slugify_tag,
)
from pka.db.queries import get_engine, init_db, upsert_document
from pka.db.schema import cluster_assignments, cluster_runs, clusters, source_tags
from pka.ollama_chat import resolve_chat_model


def _seed_cluster_with_tags(
    doc_tags: list[list[str]],
    label: str = "Cluster topic",
) -> tuple[int, int]:
    init_db()
    eng = get_engine()
    now = int(time.time())
    doc_ids = []
    for i, tags in enumerate(doc_tags):
        doc_ids.append(upsert_document(
            "zotero", f"T{i:03d}", f"Raft consensus paper {i}",
            f"https://example.com/{i}", now,
        ))

    with eng.begin() as con:
        run_res = con.execute(
            cluster_runs.insert().values(
                timestamp=now, algorithm="HDBSCAN",
                parameters="{}", accepted=True,
            )
        )
        run_id = run_res.inserted_primary_key[0]
        cl_res = con.execute(
            clusters.insert().values(
                label=label, description="", created_at=now, run_id=run_id,
            )
        )
        cluster_id = cl_res.inserted_primary_key[0]
        for did in doc_ids:
            con.execute(cluster_assignments.insert().values(
                document_id=did, cluster_id=cluster_id,
                run_id=run_id, score=0.9, assigned_at=now,
            ))
        for did, tags in zip(doc_ids, doc_tags):
            for tag in tags:
                con.execute(source_tags.insert().values(
                    document_id=did, tag_string=tag, source="zotero",
                ))
    return cluster_id, run_id


class TestSlugifyTag:
    def test_lowercases_and_hyphenates(self):
        assert slugify_tag("Distributed Systems") == "distributed-systems"


class TestPickSuggestedTag:
    def test_prefers_llm(self):
        candidates = [
            TagCandidate("consensus", "existing", 0.8, 4),
            TagCandidate("distributed-systems", "llm", 0.0, 0),
        ]
        assert pick_suggested_tag(candidates) == "distributed-systems"

    def test_falls_back_to_coverage(self):
        candidates = [
            TagCandidate("consensus", "existing", 0.8, 4),
        ]
        assert pick_suggested_tag(candidates) == "consensus"


class TestBuildTagSuggestions:
    def setup_method(self):
        _TAG_CACHE.clear()

    def test_llm_is_default(self, monkeypatch):
        cluster_id, run_id = _seed_cluster_with_tags([[], []], label="Raft")
        monkeypatch.setattr(
            "pka.clustering.tag_suggestions.suggest_tag_with_llm",
            lambda *a, **k: ("raft-consensus", None),
        )
        with get_engine().connect() as con:
            result = build_tag_suggestions(con, cluster_id, run_id, "Raft")
        assert result.suggested_tag == "raft-consensus"
        assert result.candidates[0].source == "llm"

    def test_includes_coverage_alternative(self, monkeypatch):
        cluster_id, run_id = _seed_cluster_with_tags([
            ["consensus"],
            ["consensus"],
            ["consensus"],
            ["other"],
        ])
        monkeypatch.setattr(
            "pka.clustering.tag_suggestions.suggest_tag_with_llm",
            lambda *a, **k: ("raft", None),
        )
        with get_engine().connect() as con:
            result = build_tag_suggestions(con, cluster_id, run_id, "Topic")
        existing = [c for c in result.candidates if c.source == "existing"]
        assert existing
        assert existing[0].coverage >= COVERAGE_THRESHOLD

    def test_surfaces_llm_error(self, monkeypatch):
        cluster_id, run_id = _seed_cluster_with_tags([[], []], label="Topic")
        monkeypatch.setattr(
            "pka.clustering.tag_suggestions.suggest_tag_with_llm",
            lambda *a, **k: ("", "model not found"),
        )
        with get_engine().connect() as con:
            result = build_tag_suggestions(con, cluster_id, run_id, "Topic")
        assert result.llm_error == "model not found"


class TestResolveChatModel:
    def test_skips_embedding_models(self, monkeypatch):
        class Resp:
            def raise_for_status(self): ...
            def json(self):
                return {"models": [
                    {"name": "nomic-embed-text:latest"},
                    {"name": "minimax-m2.5:cloud"},
                ]}

        import pka.ollama_chat as oc
        oc._cached_chat_model = None
        monkeypatch.setattr("httpx.get", lambda *a, **k: Resp())
        assert resolve_chat_model() == "minimax-m2.5:cloud"
