"""Local map-reduce summarisation (DESIGN.md §3.2 mechanism 3)."""
from __future__ import annotations

import pytest

from pka.config import settings as cfg
from pka.ingestion import summarize as sz


@pytest.fixture
def provider(monkeypatch):
    """Record every chat call and reply with a scripted summary."""

    class Recorder:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.reply: object = {"summary": "A study of bees. It covers hive behaviour."}
            self.error: str | None = None
            self.raises: Exception | None = None

        def __call__(self, prompt, model=None, temperature=None, timeout=90):
            self.prompts.append(prompt)
            if self.raises is not None:
                raise self.raises
            return (self.reply, self.error)

        @property
        def calls(self) -> int:
            return len(self.prompts)

    rec = Recorder()
    monkeypatch.setattr(sz, "chat_json", rec)
    return rec


def _sentences(n: int, word: str = "Something") -> str:
    return " ".join(f"{word} number {i} happened here." for i in range(n))


class TestShortCircuit:
    def test_input_within_cap_costs_no_call(self, provider):
        """Most bookmarks land here — paying a round-trip to re-say them is waste."""
        text = "Bees make honey. Hives are warm."
        assert sz.summarize_text(text, max_sentences=4) == text
        assert provider.calls == 0

    def test_exactly_at_cap_costs_no_call(self, provider):
        text = _sentences(3)
        assert sz.summarize_text(text, max_sentences=3) is not None
        assert provider.calls == 0

    def test_one_over_cap_does_call(self, provider):
        assert sz.summarize_text(_sentences(4), max_sentences=3) is not None
        assert provider.calls == 1


class TestDegenerateInput:
    @pytest.mark.parametrize("text", ["", "   ", "\n\n\t "])
    def test_empty_returns_none_without_calling(self, provider, text):
        assert sz.summarize_text(text) is None
        assert provider.calls == 0

    @pytest.mark.parametrize("limit", [0, -1])
    def test_meaningless_cap_returns_none(self, provider, limit):
        assert sz.summarize_text(_sentences(20), max_sentences=limit) is None
        assert provider.calls == 0

    def test_default_cap_comes_from_config(self, provider, monkeypatch):
        monkeypatch.setattr(cfg, "summary_max_sentences", 2)
        assert sz.summarize_text(_sentences(2)) is not None
        assert provider.calls == 0
        assert sz.summarize_text(_sentences(6)) is not None
        assert provider.calls == 1


class TestSingleCall:
    def test_returns_provider_summary(self, provider):
        out = sz.summarize_text(_sentences(20), max_sentences=4)
        assert out == "A study of bees. It covers hive behaviour."
        assert provider.calls == 1

    def test_reply_is_trimmed_to_cap(self, provider):
        provider.reply = {"summary": _sentences(9, "Fact")}
        out = sz.summarize_text(_sentences(20), max_sentences=2)
        assert out is not None
        assert out.count("Fact number") == 2

    @pytest.mark.parametrize("key", ["summary", "text", "content", "abstract"])
    def test_alternate_reply_keys_salvaged(self, provider, key):
        provider.reply = {key: "Bees and their hives."}
        assert sz.summarize_text(_sentences(20), max_sentences=4) == "Bees and their hives."

    def test_map_prompt_is_not_the_reduce_prompt(self, provider):
        sz.summarize_text(_sentences(20), max_sentences=4)
        assert "partial summaries" not in provider.prompts[0].lower()


class TestFailureModes:
    def test_provider_error_returns_none(self, provider):
        provider.error = "connection refused"
        assert sz.summarize_text(_sentences(20), max_sentences=4) is None

    def test_provider_exception_returns_none(self, provider):
        provider.raises = RuntimeError("backend exploded")
        assert sz.summarize_text(_sentences(20), max_sentences=4) is None

    @pytest.mark.parametrize("reply", [{}, {"summary": ""}, {"summary": 42}, None, "text"])
    def test_unusable_reply_returns_none(self, provider, reply):
        provider.reply = reply
        assert sz.summarize_text(_sentences(20), max_sentences=4) is None


class TestMapReduce:
    def test_long_input_maps_then_reduces(self, provider):
        text = _sentences(400)          # comfortably over CHUNK_CHAR_LIMIT
        assert len(text) > sz.CHUNK_CHAR_LIMIT
        out = sz.summarize_text(text, max_sentences=4)
        assert out is not None
        # >1 map call, plus a reduce pass over the partials.
        assert provider.calls > 2
        assert any("partial summaries" in p.lower() for p in provider.prompts)

    def test_call_count_is_bounded(self, provider):
        """Cost has to stay predictable during a bulk ingest."""
        huge = _sentences(20000)
        sz.summarize_text(huge, max_sentences=4)
        ceiling = sz.MAX_CHUNKS_PER_PASS * sz.MAX_REDUCE_DEPTH + sz.MAX_REDUCE_DEPTH + 1
        assert provider.calls <= ceiling

    def test_one_failed_chunk_does_not_lose_the_document(self, provider):
        calls = {"n": 0}
        original = provider.__call__

        def flaky(prompt, model=None, temperature=None, timeout=90):
            calls["n"] += 1
            if calls["n"] == 1:
                return ({}, "transient failure")
            return original(prompt, model=model, temperature=temperature)

        provider_text = _sentences(400)
        import pka.ingestion.summarize as mod
        mod.chat_json = flaky
        try:
            assert sz.summarize_text(provider_text, max_sentences=4) is not None
        finally:
            mod.chat_json = original

    def test_every_chunk_failing_returns_none(self, provider):
        provider.error = "down"
        assert sz.summarize_text(_sentences(400), max_sentences=4) is None

    def test_pathological_unpunctuated_text_terminates(self, provider):
        """A wall of OCR text with no sentence breaks must not defeat the bound."""
        wall = "x" * (sz.CHUNK_CHAR_LIMIT * 5)
        out = sz.summarize_text(wall, max_sentences=4)
        assert out is not None
        assert provider.calls <= sz.MAX_CHUNKS_PER_PASS + sz.MAX_REDUCE_DEPTH + 1


class TestChunking:
    def test_respects_the_chunk_ceiling(self):
        chunks = sz._chunk_for_summary(_sentences(20000))
        assert 0 < len(chunks) <= sz.MAX_CHUNKS_PER_PASS

    def test_chunks_stay_under_the_char_limit(self):
        chunks = sz._chunk_for_summary(_sentences(2000))
        assert all(len(c) <= sz.CHUNK_CHAR_LIMIT for c in chunks)

    def test_oversized_sentence_is_hard_sliced(self):
        chunks = sz._chunk_for_summary("y" * (sz.CHUNK_CHAR_LIMIT * 3))
        assert all(len(c) <= sz.CHUNK_CHAR_LIMIT for c in chunks)


class TestAttachSummaryChunk:
    """Wiring of the summary chunk into the fetched-text paths (DESIGN.md §3.2)."""

    @pytest.fixture(autouse=True)
    def _db(self):
        from pka.db.queries import init_db
        init_db()

    @pytest.fixture
    def doc_id(self):
        from pka.constants import FetchStatus, Source
        from pka.db.queries import upsert_document
        return upsert_document(
            source=Source.FIREFOX, source_id="s1", title="A Page",
            url_or_path="http://x", date_added=0, fetch_status=FetchStatus.FETCHED,
        )

    @pytest.fixture
    def summary_on(self, monkeypatch):
        monkeypatch.setattr(cfg, "bookmark_summary_enabled", True)

    def _long(self):
        return _sentences(40, "Bees")

    def test_off_by_default_adds_nothing(self, doc_id, provider, mock_chroma):
        from pka.constants import Source
        from pka.ingestion.core import attach_summary_chunk

        assert attach_summary_chunk(doc_id, self._long(), Source.FIREFOX) == 0
        assert provider.calls == 0

    def test_dry_run_adds_nothing(self, doc_id, provider, summary_on, mock_chroma):
        from pka.constants import Source
        from pka.ingestion.core import attach_summary_chunk

        assert attach_summary_chunk(
            doc_id, self._long(), Source.FIREFOX, dry_run=True
        ) == 0
        assert provider.calls == 0

    def test_adds_a_summary_chunk_when_enabled(
        self, doc_id, provider, summary_on, mock_chroma
    ):
        from pka.constants import Source
        from pka.ingestion.core import attach_summary_chunk

        assert attach_summary_chunk(doc_id, self._long(), Source.FIREFOX) == 1
        store, _col = mock_chroma
        metas = [i["meta"] for i in store.values() if i["meta"].get("pass") == "summary"]
        assert len(metas) == 1

    def test_summary_is_cached_and_not_re_inferred(
        self, doc_id, provider, summary_on, mock_chroma
    ):
        """A purge-and-reingest must replay without paying for inference twice."""
        from pka.constants import Source
        from pka.db.queries import get_generated_summary
        from pka.ingestion.core import attach_summary_chunk

        attach_summary_chunk(doc_id, self._long(), Source.FIREFOX)
        assert provider.calls == 1
        assert get_generated_summary(doc_id)

        attach_summary_chunk(doc_id, self._long(), Source.FIREFOX)
        assert provider.calls == 1, "cached summary must not re-call the model"

    def test_provider_failure_costs_the_document_nothing(
        self, doc_id, provider, summary_on, mock_chroma
    ):
        from pka.constants import Source
        from pka.ingestion.core import attach_summary_chunk

        provider.error = "backend down"
        assert attach_summary_chunk(doc_id, self._long(), Source.FIREFOX) == 0

    def test_empty_text_adds_nothing(self, doc_id, provider, summary_on, mock_chroma):
        from pka.constants import Source
        from pka.ingestion.core import attach_summary_chunk

        assert attach_summary_chunk(doc_id, "   ", Source.FIREFOX) == 0
        assert provider.calls == 0

    def test_chunk_index_does_not_collide_with_body_chunks(
        self, doc_id, provider, summary_on, mock_chroma
    ):
        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.queries import get_engine
        from pka.db.schema import chunks
        from pka.ingestion.core import attach_summary_chunk, ingest_text_block

        ingest_text_block(doc_id, self._long(), Source.FIREFOX)
        attach_summary_chunk(doc_id, self._long(), Source.FIREFOX)
        with get_engine().connect() as con:
            idxs = [r[0] for r in con.execute(sa.select(chunks.c.chunk_index)).fetchall()]
        assert len(idxs) == len(set(idxs)), f"duplicate chunk_index: {idxs}"


class TestMaterialFraming:
    """Per-source framing (``material``) and background facts (``context``)."""

    def test_default_framing_calls_the_input_a_document(self, provider):
        sz.summarize_text(_sentences(20), max_sentences=2)
        assert "indexing a document" in provider.prompts[0]

    def test_reddit_comment_material_renames_the_noun(self, provider):
        sz.summarize_text(_sentences(20), max_sentences=2, material="reddit_comment")
        prompt = provider.prompts[0]
        assert "indexing a Reddit comment" in prompt
        assert "indexing a document" not in prompt

    def test_reddit_comment_material_adds_its_own_rules(self, provider):
        sz.summarize_text(_sentences(20), max_sentences=2, material="reddit_comment")
        prompt = provider.prompts[0]
        assert "ONE comment taken out of its thread" in prompt
        # The generic rules still apply on top of the material-specific ones.
        assert "Invent nothing that is not in the text." in prompt

    def test_reddit_post_material_differs_from_comment(self, provider):
        sz.summarize_text(_sentences(20), max_sentences=2, material="reddit_post")
        assert "indexing a Reddit post" in provider.prompts[0]

    def test_unknown_material_falls_back_to_the_generic_framing(self, provider):
        sz.summarize_text(_sentences(20), max_sentences=2, material="mastodon_toot")
        assert "indexing a document" in provider.prompts[0]

    def test_context_is_labelled_and_outside_the_quoted_text(self, provider):
        sz.summarize_text(
            _sentences(20),
            max_sentences=2,
            material="reddit_comment",
            context="Thread: Understanding Raft · Subreddit: r/compsci",
        )
        prompt = provider.prompts[0]
        assert "Context (background, do not summarise): Thread: Understanding Raft" in prompt
        # It must precede the quoted block, so it reads as background rather
        # than as material the summary should cover.
        assert prompt.index("Context (background") < prompt.index('Text:\n"""')

    def test_blank_context_adds_no_line(self, provider):
        sz.summarize_text(_sentences(20), max_sentences=2, context="   ")
        assert "Context (background" not in provider.prompts[0]

    def test_framing_survives_the_reduce_pass(self, provider):
        """Merging partials must keep calling the item a comment, not a document."""
        long_text = _sentences(1200)
        assert len(long_text) > sz.CHUNK_CHAR_LIMIT * 2

        sz.summarize_text(long_text, max_sentences=2, material="reddit_comment")

        reduce_prompts = [p for p in provider.prompts if "Partial summaries" in p]
        assert reduce_prompts, "expected at least one reduce pass"
        assert all("ONE Reddit comment" in p for p in reduce_prompts)

    def test_short_input_still_costs_no_call_with_material_set(self, provider):
        """Framing must not defeat the short-circuit that skips the provider."""
        out = sz.summarize_text(
            "One short comment.", max_sentences=cfg.summary_max_sentences,
            material="reddit_comment", context="Thread: whatever",
        )
        assert out == "One short comment."
        assert provider.calls == 0
