"""Sentence splitting and chunking for English, French and Spanish text.

The splitter used to need an ASCII capital after ``.!?``, so a French or
Spanish sentence opening on ``É``, ``Á``, ``Ñ``, ``¿`` or ``«`` ran into the one
before it.
"""

import re

import pytest

from pka.ingestion import chunker
from pka.ingestion.chunker import _split_sentences, sentence_window_chunks


@pytest.fixture(autouse=True)
def no_spacy(monkeypatch):
    """The punctuation scan, whatever is installed; spaCy is tested below."""
    monkeypatch.setattr(chunker, "_spacy_nlp", False)


class TestSentenceStarts:
    @pytest.mark.parametrize(
        "text",
        [
            "Il est parti. Été comme hiver, il revient.",
            "Llegó tarde. Ángel no dijo nada.",
            "Das ist gut. Über alles andere reden wir später.",
        ],
    )
    def test_an_accented_capital_starts_a_sentence(self, text):
        assert len(_split_sentences(text)) == 2

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "Llegó tarde. ¿Qué pasó después? Nadie lo sabe.",
                ["Llegó tarde.", "¿Qué pasó después?", "Nadie lo sabe."],
            ),
            ("Fue increíble. ¡Qué día! Volvimos.", ["Fue increíble.", "¡Qué día!", "Volvimos."]),
            (
                "Il a dit non. « Pourquoi ? » demanda-t-elle.",
                ["Il a dit non.", "« Pourquoi ? » demanda-t-elle."],
            ),
        ],
    )
    def test_spanish_and_french_opening_marks(self, text, expected):
        assert _split_sentences(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "It costs 3.5 million. That is a lot.",  # decimal
            "Ask Dr. Smith about it. He knows.",  # abbreviation
            "Use e.g. a hammer. Or a stone.",  # lowercase after abbreviation
        ],
    )
    def test_english_boundaries_are_unchanged(self, text):
        assert len(_split_sentences(text)) == 2

    def test_english_quotes_and_brackets_do_not_open_a_sentence(self):
        """As before: a sentence opening on ``"`` or ``(`` runs into the last one."""
        assert _split_sentences('He said so. "Then go," she replied.') == [
            'He said so. "Then go," she replied.'
        ]

    def test_a_closing_quote_or_bracket_stays_with_its_sentence(self):
        """``.)`` and ``."`` end a sentence; the old splitter needed the stop last."""
        assert _split_sentences("It ended. (Nobody noticed.) Then it began.") == [
            "It ended. (Nobody noticed.)",
            "Then it began.",
        ]
        assert _split_sentences("« C'est fini. » Il part.") == ["« C'est fini. »", "Il part."]


class TestRunsWithNoBoundary:
    def test_a_long_run_is_cut_between_words(self):
        text = " ".join(["word"] * 400)  # 1999 characters, no full stop
        pieces = _split_sentences(text)
        assert len(pieces) > 1
        assert all(len(p) <= 1000 for p in pieces)
        assert " ".join(pieces) == text

    def test_a_single_word_over_the_cap_is_cut_inside_it(self):
        text = "x" * 2500
        pieces = _split_sentences(text)
        assert [len(p) for p in pieces] == [1000, 1000, 500]

    def test_a_document_with_no_boundary_is_no_longer_one_chunk(self):
        text = " ".join(["ocr"] * 3000)
        assert len(sentence_window_chunks(text, window=5, overlap=1)) > 1

    def test_a_cap_of_zero_disables_it(self):
        text = " ".join(["word"] * 400)
        assert chunker._sentence_spans(text, 0) == [(0, len(text))]


class TestSpacy:
    def test_spacy_is_used_when_available(self, monkeypatch):
        class Sent:
            def __init__(self, text):
                self.text = text

        class Doc:
            def __init__(self, text):
                self.sents = [Sent(s) for s in re.split(r"(?<=;)\s*", text) if s.strip()]

        monkeypatch.setattr(chunker, "_get_spacy", lambda: lambda text: Doc(text))
        # A split the punctuation scan would never make: spaCy's answer is used.
        assert _split_sentences("one; two") == ["one;", "two"]

    def test_the_cap_applies_to_spacy_sentences_too(self, monkeypatch):
        class Doc:
            def __init__(self, text):
                self.sents = [type("S", (), {"text": text})()]

        monkeypatch.setattr(chunker, "_get_spacy", lambda: lambda text: Doc(text))
        assert len(_split_sentences(" ".join(["word"] * 400))) > 1
