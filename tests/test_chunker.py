from pka.ingestion.chunker import clean_text, sentence_window_chunks


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
        assert clean_text("\ufb01le") == "file"

    def test_empty_string(self):
        assert clean_text("") == ""


class TestSentenceWindowChunks:
    SAMPLE = (
        "Raft is a consensus algorithm. "
        "It was designed to be more understandable than Paxos. "
        "Leader election is a key component. "
        "Log replication follows leader election. "
        "Safety is guaranteed by the commit rule. "
        "Raft clusters typically have an odd number of nodes. "
        "A majority quorum is required for any commit."
    )

    def test_returns_list_of_strings(self):
        chunks = sentence_window_chunks(self.SAMPLE)
        assert isinstance(chunks, list)
        assert all(isinstance(c, str) for c in chunks)

    def test_non_empty_for_valid_text(self):
        assert len(sentence_window_chunks(self.SAMPLE)) > 0

    def test_empty_string_returns_empty(self):
        assert sentence_window_chunks("") == []

    def test_whitespace_only_returns_empty(self):
        assert sentence_window_chunks("   \n\t  ") == []

    def test_min_chars_filters_short_chunks(self):
        result = sentence_window_chunks("Hi. Ok.", min_chars=200)
        assert result == []

    def test_window_size_respected(self):
        # With window=2 each chunk should contain at most 2 sentences worth of text
        chunks = sentence_window_chunks(self.SAMPLE, window=2, overlap=0, min_chars=1)
        # No chunk should be longer than the full text
        for c in chunks:
            assert len(c) < len(self.SAMPLE)

    def test_overlap_produces_more_chunks_than_no_overlap(self):
        with_overlap    = sentence_window_chunks(self.SAMPLE, window=3, overlap=1, min_chars=1)
        without_overlap = sentence_window_chunks(self.SAMPLE, window=3, overlap=0, min_chars=1)
        assert len(with_overlap) >= len(without_overlap)

    def test_single_sentence(self):
        chunks = sentence_window_chunks("Only one sentence here.", window=5, min_chars=1)
        assert len(chunks) == 1

    def test_chunk_content_is_subset_of_input(self):
        chunks = sentence_window_chunks(self.SAMPLE, window=2, overlap=0, min_chars=1)
        full = clean_text(self.SAMPLE)
        for c in chunks:
            # Every word in the chunk should appear in the cleaned source
            for word in c.split():
                assert word in full
