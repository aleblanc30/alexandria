"""
Text cleaning, chunking and sentence splitting, on off-the-shelf libraries.

:func:`chunk_text` cuts retrieval chunks with ``semantic-text-splitter``: it
packs the largest semantic units that fit (paragraphs, then sentences, then
words) into chunks of at most ``chunk_tokens`` tokens, counted by the
embedding model's own tokenizer and capped at what the model reads, with up to
``chunk_overlap_tokens`` repeated from the previous chunk. Sentence boundaries
are Unicode's (UAX #29), which need no language: a French or Spanish sentence
opening on ``É`` or ``¿`` splits as an English one does, and a run with no
boundary at all (unpunctuated OCR) is cut between words.

:func:`split_sentences` is ``pysbd`` with its English rules, for the callers
that need whole sentences rather than chunks (trimming a summary or a
synopsis). Its abbreviation handling (``Dr.``, ``e.g.``) is finer than
Unicode's, which does break after ``Dr.``.
"""

import logging
import re
import threading
import unicodedata
from typing import Any

from pka.config import settings as cfg

log = logging.getLogger(__name__)


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate PDF line breaks
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse excess blank lines
    text = re.sub(r"[ \t]+", " ", text)  # normalise whitespace
    return text.strip()


_splitters: dict[tuple[str, int, int], Any] = {}
_splitters_lock = threading.Lock()


def _splitter(embedder: Any, max_tokens: int, overlap_tokens: int):
    """A token splitter for *embedder*, built once per model and size."""
    from semantic_text_splitter import TextSplitter

    capacity = max(1, min(max_tokens, embedder.max_chunk_tokens))
    overlap = max(0, min(overlap_tokens, capacity - 1))
    key = (embedder.name, capacity, overlap)
    with _splitters_lock:
        if key not in _splitters:
            _splitters[key] = TextSplitter.from_huggingface_tokenizer(
                embedder.tokenizer, capacity, overlap=overlap
            )
        return _splitters[key]


def chunk_text(
    text: str,
    *,
    max_tokens: int | None = None,
    overlap_tokens: int | None = None,
    min_chars: int | None = None,
    embedder: Any = None,
) -> list[str]:
    """Chunks of cleaned *text*, dropping any shorter than *min_chars*.

    Sizes default to the ``chunk_tokens``, ``chunk_overlap_tokens`` and
    ``min_chunk_chars`` settings; *embedder* to the one the chunk index uses,
    whose tokenizer counts the tokens.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    if embedder is None:
        from pka.storage.vector_store import active_embedder

        embedder = active_embedder()
    splitter = _splitter(
        embedder,
        max_tokens if max_tokens is not None else cfg.chunk_tokens,
        overlap_tokens if overlap_tokens is not None else cfg.chunk_overlap_tokens,
    )
    floor = min_chars if min_chars is not None else cfg.min_chunk_chars
    return [c for c in splitter.chunks(cleaned) if len(c) >= floor]


_segmenter: Any = None
_segmenter_lock = threading.Lock()


def split_sentences(text: str) -> list[str]:
    """The sentences of *text*, cleaned."""
    global _segmenter
    cleaned = clean_text(text or "")
    if not cleaned:
        return []
    with _segmenter_lock:
        if _segmenter is None:
            import pysbd

            _segmenter = pysbd.Segmenter(language="en", clean=False)
        pieces = _segmenter.segment(cleaned)
    return [s for s in (p.strip() for p in pieces) if s]


def trim_to_sentences(text: str, max_sentences: int) -> str:
    """First ``max_sentences`` sentences of *text*, cleaned."""
    cleaned = clean_text(text or "")
    if not cleaned or max_sentences <= 0:
        return cleaned
    sentences = split_sentences(cleaned)
    if len(sentences) <= max_sentences:
        return cleaned
    return " ".join(sentences[:max_sentences])
