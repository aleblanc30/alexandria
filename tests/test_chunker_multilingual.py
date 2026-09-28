"""Chunking and sentence splitting for English, French and Spanish text.

Chunk boundaries are Unicode's (UAX #29), which need no language; sentence
splitting for trimming is pysbd's English rules, which the archive's French
and Spanish also split correctly under.
"""

import pytest

from pka.ingestion.chunker import chunk_text, split_sentences
from tests.conftest import FakeEmbedder


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Llegó tarde. ¿Qué pasó después? Nadie lo sabe.",
            ["Llegó tarde.", "¿Qué pasó después?", "Nadie lo sabe."],
        ),
        (
            "Il est parti. Été comme hiver, il revient.",
            ["Il est parti.", "Été comme hiver, il revient."],
        ),
        (
            "Il a dit non. « Pourquoi ? » demanda-t-elle.",
            ["Il a dit non.", "« Pourquoi ? » demanda-t-elle."],
        ),
    ],
)
def test_french_and_spanish_sentences(text, expected):
    assert split_sentences(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Llegó tarde. ¿Qué pasó después? Nadie lo sabe. Ángel no dijo nada.",
        "Il est parti. Été comme hiver, il revient. Ça ne change rien.",
    ],
)
def test_an_accented_or_inverted_opening_starts_a_chunk(text):
    """Budgets of a few words force a break at every sentence that fits."""
    chunks = chunk_text(
        text, max_tokens=7, overlap_tokens=0, min_chars=1, embedder=FakeEmbedder("words")
    )
    assert len(chunks) > 2
    assert all(c[0].isupper() or c[0] in "¿¡«" for c in chunks)
