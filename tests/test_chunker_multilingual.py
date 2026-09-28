"""Sentence splitting and chunking outside English.

Before this, the splitter needed an ASCII capital after ``.!?``, so a document
in a script without one (or without spaces) was a single sentence, and so a
single chunk: a silent retrieval failure.
"""

import re

import pytest

from pka.ingestion import chunker
from pka.ingestion.chunker import (
    _split_sentences,
    _weight,
    sentence_window_chunks,
    trim_to_sentences,
)


@pytest.fixture(autouse=True)
def no_spacy(monkeypatch):
    """The punctuation scan, whatever is installed; spaCy routing is tested below."""
    monkeypatch.setattr(chunker, "_spacy_nlp", False)


class TestSentenceEnds:
    def test_cjk_full_width_stops_split_without_a_following_space(self):
        text = "今日は晴れです。明日は雨でしょう！本当ですか？"
        # clean_text's NFKC folds ！？ to ASCII, which must still split here.
        assert _split_sentences(text) == ["今日は晴れです。", "明日は雨でしょう!", "本当ですか?"]

    def test_chinese(self):
        text = "深度学习改变了图像识别。模型需要大量数据。"
        assert len(_split_sentences(text)) == 2

    def test_devanagari_danda(self):
        text = "यह पहला वाक्य है। यह दूसरा वाक्य है।"
        assert len(_split_sentences(text)) == 2

    def test_arabic_question_mark(self):
        text = "ما هذا؟ هذا كتاب."
        assert len(_split_sentences(text)) == 2

    @pytest.mark.parametrize(
        "text",
        [
            "Il est parti. Été comme hiver, il revient.",  # accented capital
            "Это первое предложение. Это второе предложение.",  # Cyrillic
            "Αυτή είναι η πρώτη. Αυτή είναι η δεύτερη.",  # Greek
            "Das ist gut. Über alles andere reden wir später.",  # umlaut capital
        ],
    )
    def test_a_capital_in_any_cased_script_starts_a_sentence(self, text):
        assert len(_split_sentences(text)) == 2

    def test_an_uncased_script_after_a_full_stop_starts_a_sentence(self):
        """Hebrew has no capitals to wait for."""
        text = "Hello there. שלום עולם"
        assert len(_split_sentences(text)) == 2

    @pytest.mark.parametrize(
        "text",
        [
            "It costs 3.5 million. That is a lot.",  # decimal
            "Ask Dr. Smith about it. He knows.",  # abbreviation
            "Use e.g. a hammer. Or a stone.",  # lowercase after abbreviation
            "Version 2.0 is out. Upgrade now.",
        ],
    )
    def test_english_boundaries_are_unchanged(self, text):
        assert len(_split_sentences(text)) == 2


class TestRunsWithNoBoundary:
    def test_unpunctuated_cjk_is_cut_rather_than_kept_whole(self):
        text = "漢字" * 600  # 1200 dense characters, no terminator anywhere
        pieces = _split_sentences(text)
        assert len(pieces) > 1
        assert all(_weight(p) <= 1000 for p in pieces)
        assert "".join(pieces) == text  # cut, not lost

    def test_thai_is_cut_at_its_spaces(self):
        """Thai marks no sentence ends but separates clauses with spaces."""
        clause = "ภาษาไทยไม่มีเครื่องหมายจบประโยค"
        text = " ".join([clause] * 40)
        pieces = _split_sentences(text)
        assert len(pieces) > 1
        assert all(p.strip() == p and not p.startswith(" ") for p in pieces)
        assert " ".join(pieces) == text

    def test_a_long_run_of_words_is_cut_between_words(self):
        text = " ".join(["word"] * 400)  # 1999 characters, no full stop
        pieces = _split_sentences(text)
        assert len(pieces) > 1
        assert all(set(p.split()) == {"word"} for p in pieces)

    def test_a_document_with_no_boundary_is_no_longer_one_chunk(self):
        text = "数据" * 3000
        assert len(sentence_window_chunks(text, window=5, overlap=1)) > 1

    def test_a_cap_of_zero_disables_it(self):
        text = "漢字" * 600
        assert _split_sentences(text) != [text]
        assert chunker._sentence_spans(text, 0) == [(0, len(text))]


class TestChunks:
    def test_cjk_sentences_are_joined_without_spaces(self):
        text = "今日は晴れです。明日は雨でしょう。明後日は曇りです。"
        (chunk,) = sentence_window_chunks(text, window=5, overlap=0, min_chars=1)
        assert chunk == text

    def test_english_sentences_are_joined_with_one_space(self):
        text = "First one.\n\nSecond one.  Third one."
        (chunk,) = sentence_window_chunks(text, window=5, overlap=0, min_chars=1)
        assert chunk == "First one. Second one. Third one."

    def test_min_chars_weighs_a_cjk_character_as_three(self):
        text = "深度学习改变了图像识别领域的研究方法。"  # 19 characters, weight 57
        assert sentence_window_chunks(text, min_chars=50) == [text]
        assert sentence_window_chunks(text, min_chars=60) == []

    def test_trim_to_sentences_on_japanese(self):
        text = "一つ目です。二つ目です。三つ目です。"
        assert trim_to_sentences(text, 2) == "一つ目です。二つ目です。"


class TestSpacyRouting:
    def test_dense_text_never_reaches_spacy(self, monkeypatch):
        def boom():
            raise AssertionError("spaCy was asked to split CJK text")

        monkeypatch.setattr(chunker, "_get_spacy", boom)
        assert len(_split_sentences("一つ目です。二つ目です。")) == 2

    def test_spaced_text_uses_spacy_when_available(self, monkeypatch):
        class Sent:
            def __init__(self, text):
                self.text = text

        class Doc:
            def __init__(self, text):
                self.sents = [Sent(s) for s in re.split(r"(?<=;)\s*", text) if s.strip()]

        monkeypatch.setattr(chunker, "_get_spacy", lambda: lambda text: Doc(text))
        # A split the punctuation scan would never make: spaCy's answer is used.
        assert _split_sentences("one; two") == ["one;", "two"]
