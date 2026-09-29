import pytest

from pka.ingestion import chunker
from pka.ingestion.chunker import chunk_text, clean_text, split_sentences, trim_to_sentences
from tests.conftest import FakeEmbedder


class TestCleanText:
    def test_dehyphenates_pdf_linebreaks(self):
        assert clean_text("distrib-\nuted") == "distributed"

    def test_normalises_whitespace(self):
        assert clean_text("foo   bar\t baz") == "foo bar baz"

    def test_collapses_excess_blank_lines(self):
        result = clean_text("a\n\n\n\nb")
        assert "\n\n\n" not in result

    def test_strips_leading_trailing(self):
        assert clean_text("  hello  ") == "hello"

    def test_unicode_normalisation(self):
        # NFKC: ligature fi → f + i
        assert clean_text("ﬁle") == "file"

    def test_empty_string(self):
        assert clean_text("") == ""


@pytest.fixture()
def words():
    """An embedder whose tokenizer counts one token per word or punctuation run."""
    return FakeEmbedder("word-counter")


def _tokens(embedder, text: str) -> int:
    return len(embedder.tokenizer.encode(text).ids)


class TestChunkText:
    SAMPLE = (
        "Raft is a consensus algorithm. "
        "It was designed to be more understandable than Paxos. "
        "Leader election is a key component. "
        "Log replication follows leader election. "
        "Safety is guaranteed by the commit rule. "
        "Raft clusters typically have an odd number of nodes. "
        "A majority quorum is required for any commit."
    )

    def test_empty_or_blank_text_has_no_chunks(self, words):
        assert chunk_text("", embedder=words) == []
        assert chunk_text("   \n\t  ", embedder=words) == []

    def test_a_short_text_is_one_chunk(self, words):
        assert chunk_text(self.SAMPLE, min_chars=1, embedder=words) == [clean_text(self.SAMPLE)]

    def test_min_chars_filters_short_chunks(self, words):
        assert chunk_text("Hi. Ok.", min_chars=200, embedder=words) == []

    def test_no_chunk_exceeds_the_token_budget(self, words):
        chunks = chunk_text(
            self.SAMPLE, max_tokens=12, overlap_tokens=0, min_chars=1, embedder=words
        )
        assert len(chunks) > 1
        assert all(_tokens(words, c) <= 12 for c in chunks)

    def test_chunks_break_between_sentences_when_they_fit(self, words):
        chunks = chunk_text(
            self.SAMPLE, max_tokens=12, overlap_tokens=0, min_chars=1, embedder=words
        )
        assert all(c.endswith(".") for c in chunks)
        assert " ".join(chunks) == clean_text(self.SAMPLE)

    def test_overlap_repeats_whole_sentences_that_fit_in_it(self, words):
        """Up to the overlap budget: a sentence that fits is repeated, a longer one is not."""
        chunks = chunk_text(
            self.SAMPLE, max_tokens=40, overlap_tokens=8, min_chars=1, embedder=words
        )
        assert chunks == [
            "Raft is a consensus algorithm. It was designed to be more understandable than"
            " Paxos. Leader election is a key component. Log replication follows leader"
            " election. Safety is guaranteed by the commit rule.",
            "Safety is guaranteed by the commit rule. Raft clusters typically have an odd"
            " number of nodes. A majority quorum is required for any commit.",
        ]

    def test_the_budget_is_capped_at_what_the_model_reads(self, words, monkeypatch):
        monkeypatch.setattr(words, "max_chunk_tokens", 10)
        chunks = chunk_text(
            self.SAMPLE, max_tokens=500, overlap_tokens=0, min_chars=1, embedder=words
        )
        assert all(_tokens(words, c) <= 10 for c in chunks)

    def test_an_overlap_as_large_as_the_budget_is_reduced(self, words):
        chunks = chunk_text(
            self.SAMPLE, max_tokens=8, overlap_tokens=8, min_chars=1, embedder=words
        )
        assert chunks

    def test_a_run_with_no_boundary_is_cut_between_words(self, words):
        text = " ".join(["ocr"] * 3000)
        chunks = chunk_text(text, max_tokens=256, overlap_tokens=0, min_chars=1, embedder=words)
        assert len(chunks) > 1
        assert all(set(c.split()) == {"ocr"} for c in chunks)

    def test_defaults_come_from_the_settings_and_the_active_model(self, mock_chroma, monkeypatch):
        from pka.config import settings as cfg

        monkeypatch.setattr(cfg, "chunk_tokens", 12)
        monkeypatch.setattr(cfg, "chunk_overlap_tokens", 0)
        monkeypatch.setattr(cfg, "min_chunk_chars", 1)
        embedder = FakeEmbedder("default")
        assert all(_tokens(embedder, c) <= 12 for c in chunk_text(self.SAMPLE))
        assert len(chunk_text(self.SAMPLE)) > 1

    def test_one_splitter_per_model_and_size(self, words, monkeypatch):
        monkeypatch.setattr(chunker, "_splitters", {})
        chunk_text(self.SAMPLE, max_tokens=12, overlap_tokens=0, embedder=words)
        chunk_text(self.SAMPLE, max_tokens=12, overlap_tokens=0, embedder=words)
        chunk_text(self.SAMPLE, max_tokens=20, overlap_tokens=0, embedder=words)
        assert set(chunker._splitters) == {("word-counter", 12, 0), ("word-counter", 20, 0)}


class TestSplitSentences:
    def test_abbreviations_and_decimals_do_not_end_a_sentence(self):
        assert split_sentences("Ask Dr. Smith about it. He knows.") == [
            "Ask Dr. Smith about it.",
            "He knows.",
        ]
        assert split_sentences("It costs 3.5 million. That is a lot.") == [
            "It costs 3.5 million.",
            "That is a lot.",
        ]

    def test_empty_text_has_no_sentences(self):
        assert split_sentences("") == []
        assert split_sentences(None) == []

    def test_trim_keeps_the_first_sentences(self):
        assert trim_to_sentences("One here. Two here. Three here.", 2) == "One here. Two here."

    def test_trim_returns_short_text_whole(self):
        assert trim_to_sentences("One here.  Two here.", 5) == "One here. Two here."

    def test_trim_to_nothing_returns_the_cleaned_text(self):
        assert trim_to_sentences(" One. ", 0) == "One."
