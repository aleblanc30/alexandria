"""Tests for enrichment provenance (PURGE_AND_PROVENANCE_PLAN.md §6).

The point of stamping is that a purge can say "what the old model made" and
mean it. So the tests that matter are: the stamp records the *resolved* backend,
an artifact with no stamp reads as genuinely unknown rather than as belonging to
whatever is configured now, and a provenance-filtered purge touches only the
matching rows.
"""

import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import pka.enrichment_runs as er
import pka.ingestion.summarize as sz
from pka.config import settings as cfg
from pka.constants import EnrichmentKind, RunStatus, Source
from pka.db.queries import get_engine, init_db, insert_chunks
from pka.db.schema import documents, enrichment_runs
from pka.purge import purge_target
from tests.conftest import make_document


@pytest.fixture()
def db(empty_vector_store):
    init_db()


@pytest.fixture()
def summary_on(monkeypatch):
    monkeypatch.setattr(cfg, "bookmark_summary_enabled", True)


@pytest.fixture()
def chat(monkeypatch):
    """Scripted summariser, and a resolvable model name for the run row."""
    prompts: list[str] = []

    def _chat_json(prompt, model=None, temperature=None, timeout=90):
        prompts.append(prompt)
        return ({"summary": "Bees build hives. They also make honey."}, None)

    monkeypatch.setattr(sz, "chat_json", _chat_json)
    monkeypatch.setattr(
        er, "_summary_backend", lambda: ("ollama", "qwen2.5:3b", {"temperature": 0.0})
    )
    return prompts


def _doc(source: str = "firefox", source_id: str = "F1") -> int:
    doc_id = make_document(
        source, source_id, "A page", f"https://example.com/{source_id}", int(time.time())
    )
    insert_chunks(
        [
            {
                "document_id": doc_id,
                "chunk_index": 0,
                "text": "Body text long enough to be a real chunk of prose.",
                "token_count": 10,
                "vector_id": f"vec-{source_id}",
            }
        ]
    )
    return doc_id


def _runs() -> list[dict]:
    with get_engine().connect() as con:
        return [dict(r._mapping) for r in con.execute(sa.select(enrichment_runs)).fetchall()]


def _stamp_of(doc_id: int) -> int | None:
    with get_engine().connect() as con:
        return con.execute(
            sa.select(documents.c.summary_run_id).where(documents.c.id == doc_id)
        ).scalar()


def _summarise(doc_id: int) -> int:
    from pka.ingestion.core import attach_summary_chunk

    return attach_summary_chunk(doc_id, "Bees. " * 40, Source.FIREFOX, title="A page")


# ── Opening, stamping, closing ───────────────────────────────────────────────


class TestRunLifecycle:
    def test_a_summary_pass_opens_stamps_and_finishes(self, db, mock_chroma, summary_on, chat):
        doc_id = _doc()

        with er.run_scope(EnrichmentKind.SUMMARY):
            assert _summarise(doc_id) == 1

        runs = _runs()
        assert len(runs) == 1
        run = runs[0]
        assert run["kind"] == "summary"
        assert run["status"] == str(RunStatus.FINISHED)
        assert run["finished_at"] is not None
        assert _stamp_of(doc_id) == run["run_id"]

    def test_the_resolved_model_is_recorded_not_the_config_default(
        self, db, mock_chroma, summary_on, monkeypatch
    ):
        """``chat_model`` defaults to "" meaning "auto-detect from /api/tags", so
        storing the config value would record nothing useful on the most common
        local setup. The run must carry what the provider actually resolved.

        Deliberately does not use the ``chat`` fixture: that stubs the whole
        backend resolver, which is the thing under test here.
        """
        import pka.ollama_chat as oc

        monkeypatch.setattr(sz, "chat_json", lambda *a, **k: ({"summary": "A. B."}, None))
        monkeypatch.setattr(cfg, "chat_model", "")
        monkeypatch.setattr(oc, "resolve_chat_model", lambda explicit=None: "auto-detected:7b")

        with er.run_scope(EnrichmentKind.SUMMARY):
            _summarise(_doc())

        run = _runs()[0]
        assert run["provider"] == "ollama"
        assert run["model"] == "auto-detected:7b"

    def test_a_pass_that_summarises_nothing_leaves_no_run_row(self, db, mock_chroma, chat):
        """The flag is off, so nothing infers — and an empty run row would be a
        lie about work that never happened."""
        _doc()
        with er.run_scope(EnrichmentKind.SUMMARY):
            from pka.ingestion.enrich import enrich_summaries

            enrich_summaries()

        assert _runs() == []

    def test_a_failed_pass_closes_as_failed(self, db, mock_chroma, summary_on, chat):
        with pytest.raises(RuntimeError):
            with er.run_scope(EnrichmentKind.SUMMARY):
                _summarise(_doc())
                raise RuntimeError("pass blew up")

        run = _runs()[0]
        assert run["status"] == str(RunStatus.FAILED)
        assert run["finished_at"] is not None

    def test_a_cache_hit_neither_infers_nor_opens_a_run(self, db, mock_chroma, summary_on, chat):
        doc_id = _doc()
        with er.run_scope(EnrichmentKind.SUMMARY):
            _summarise(doc_id)
        first_run = _runs()[0]["run_id"]

        # Second pass: the summary is cached, so no call and no new run.
        with er.run_scope(EnrichmentKind.SUMMARY):
            _summarise(doc_id)

        assert [r["run_id"] for r in _runs()] == [first_run]
        assert len(chat) == 1

    def test_spend_counters_track_calls_and_characters(self, db, mock_chroma, summary_on, chat):
        with er.run_scope(EnrichmentKind.SUMMARY):
            _summarise(_doc())

        run = _runs()[0]
        assert run["calls"] == len(chat)
        assert run["chars_sent"] == sum(len(p) for p in chat)
        assert run["artifacts"] == 1

    def test_calls_outside_a_run_are_not_counted_and_open_nothing(self, db):
        er.record_call(EnrichmentKind.SUMMARY, 100)
        assert _runs() == []


class TestStaleRuns:
    def test_a_run_left_running_past_the_cutoff_is_failed(self, db):
        now = int(time.time())
        with get_engine().begin() as con:
            con.execute(
                enrichment_runs.insert().values(
                    kind="summary",
                    started_at=now - er.STALE_RUN_SECONDS - 60,
                    status=str(RunStatus.RUNNING),
                )
            )

        assert er.reap_stale_runs(now=now) == 1
        assert _runs()[0]["status"] == str(RunStatus.FAILED)

    def test_a_slow_run_this_process_still_holds_is_left_alone(self, db):
        """A long image pass on modest hardware genuinely runs for hours;
        wrongly failing a live run is worse than reaping late."""
        now = int(time.time())
        run_id = er.open_run(EnrichmentKind.SUMMARY)
        with get_engine().begin() as con:
            con.execute(
                enrichment_runs.update()
                .where(enrichment_runs.c.run_id == run_id)
                .values(started_at=now - er.STALE_RUN_SECONDS - 60)
            )

        assert er.reap_stale_runs(now=now) == 0
        assert _runs()[0]["status"] == str(RunStatus.RUNNING)

    def test_a_finished_run_is_never_reaped(self, db):
        now = int(time.time())
        with get_engine().begin() as con:
            con.execute(
                enrichment_runs.insert().values(
                    kind="summary",
                    started_at=now - er.STALE_RUN_SECONDS - 60,
                    status=str(RunStatus.FINISHED),
                    finished_at=now - 10,
                )
            )

        assert er.reap_stale_runs(now=now) == 0


# ── What stamping unlocks: provenance-filtered purge (§6.3) ─────────────────


class TestProvenanceFilteredPurge:
    @pytest.fixture()
    def two_backends(self, db, mock_chroma, summary_on, monkeypatch):
        """One document summarised by an 'old' model, one by the 'new' one, and
        one summarised before provenance shipped (no stamp at all)."""

        def _chat_json(prompt, model=None, temperature=None, timeout=90):
            return ({"summary": "A summary sentence. And another."}, None)

        monkeypatch.setattr(sz, "chat_json", _chat_json)

        monkeypatch.setattr(er, "_summary_backend", lambda: ("ollama", "old-model", {}))
        old_doc = _doc("firefox", "OLD")
        with er.run_scope(EnrichmentKind.SUMMARY):
            _summarise(old_doc)

        monkeypatch.setattr(er, "_summary_backend", lambda: ("ollama", "new-model", {}))
        new_doc = _doc("firefox", "NEW")
        with er.run_scope(EnrichmentKind.SUMMARY):
            _summarise(new_doc)

        legacy_doc = _doc("firefox", "LEGACY")
        with get_engine().begin() as con:
            con.execute(
                documents.update()
                .where(documents.c.id == legacy_doc)
                .values(generated_summary="A pre-provenance summary.", summary_run_id=None)
            )
        return {"old": old_doc, "new": new_doc, "legacy": legacy_doc}

    def _has_summary(self, doc_id: int) -> bool:
        with get_engine().connect() as con:
            return (
                con.execute(
                    sa.select(documents.c.generated_summary).where(documents.c.id == doc_id)
                ).scalar()
                is not None
            )

    def test_purge_by_model_keeps_the_backend_you_still_use(self, two_backends):
        counts = purge_target("summaries", model="old-model")

        assert counts["documents"] == 1
        assert not self._has_summary(two_backends["old"])
        assert self._has_summary(two_backends["new"])
        assert self._has_summary(two_backends["legacy"])

    def test_purge_by_run_id_targets_exactly_that_run(self, two_backends):
        run_id = _stamp_of(two_backends["new"])

        counts = purge_target("summaries", run_id=run_id)

        assert counts["documents"] == 1
        assert not self._has_summary(two_backends["new"])
        assert self._has_summary(two_backends["old"])

    def test_unknown_selects_exactly_the_pre_provenance_backlog(self, two_backends):
        counts = purge_target("summaries", unknown=True)

        assert counts["documents"] == 1
        assert not self._has_summary(two_backends["legacy"])
        assert self._has_summary(two_backends["old"])
        assert self._has_summary(two_backends["new"])

    def test_purge_by_provider_matches_every_model_it_ran(self, two_backends):
        counts = purge_target("summaries", provider="ollama")

        assert counts["documents"] == 2  # both stamped docs, not the legacy one
        assert self._has_summary(two_backends["legacy"])

    def test_a_provenance_purge_also_removes_the_matching_summary_chunks(self, two_backends):
        from pka.db.schema import chunks

        counts = purge_target("summaries", model="old-model")

        assert counts["chunks"] == 1
        with get_engine().connect() as con:
            passes = [
                r[0]
                for r in con.execute(
                    sa.select(chunks.c.chunk_pass).where(chunks.c.chunk_pass == "summary")
                ).fetchall()
            ]
        assert len(passes) == 1  # the new-model summary chunk survives

    def test_the_stamp_is_cleared_with_the_summary(self, two_backends):
        """A run id pointing at an absent summary would make the next count
        promise a deletion it cannot make."""
        purge_target("summaries", model="old-model")
        assert _stamp_of(two_backends["old"]) is None

    def test_dry_run_counts_match_the_filtered_purge(self, two_backends):
        counted = purge_target("summaries", model="old-model", dry_run=True)
        purged = purge_target("summaries", model="old-model")
        for name, n in counted.items():
            assert purged[name] == n

    def test_an_unstamped_target_refuses_a_provenance_filter(self, db):
        """Answering by ignoring the filter would turn "purge what the old model
        made" into "purge everything" — the one failure this feature prevents."""
        with pytest.raises(ValueError, match="records no provenance"):
            purge_target("fetched_text", model="old-model")

    def test_an_unstamped_target_still_accepts_a_plain_purge(self, db):
        purge_target("fetched_text")  # no provenance filter, no complaint


# ── API surface ──────────────────────────────────────────────────────────────


@pytest.fixture()
def client(empty_vector_store):
    init_db()
    from pka.api.main import app

    return TestClient(app, raise_server_exceptions=True)


def test_runs_endpoint_reports_what_made_what(client, mock_chroma, summary_on, chat):
    with er.run_scope(EnrichmentKind.SUMMARY):
        _summarise(_doc())

    r = client.get("/ingestion/enrichment-runs")

    assert r.status_code == 200
    run = r.json()["runs"][0]
    assert run["kind"] == "summary"
    assert run["model"] == "qwen2.5:3b"
    assert run["artifacts"] == 1
    assert run["parameters"] == {"temperature": 0.0}


def test_purge_endpoint_rejects_a_filter_the_target_cannot_honour(client):
    r = client.post("/ingestion/purge/fetched_text?model=old-model")
    assert r.status_code == 400
    assert "provenance" in r.json()["detail"]


def test_purge_endpoint_passes_the_provenance_filter_through(client, mock_chroma, summary_on, chat):
    doc_id = _doc()
    with er.run_scope(EnrichmentKind.SUMMARY):
        _summarise(doc_id)

    r = client.post("/ingestion/purge/summaries?model=nothing-made-this&dry_run=true")

    assert r.status_code == 200
    assert r.json()["counts"]["documents"] == 0  # the filter really narrowed it
