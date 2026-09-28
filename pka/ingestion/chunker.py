"""
Text cleaning and sentence-window chunking.

Sentences are found by :func:`_sentence_spans`: spaCy's sentencizer when the
``spacy`` package is importable, else a punctuation scan. The scan splits
after ``.!?…`` (and any closing quote or bracket) before whitespace and a
capital letter in any alphabet, so French and Spanish sentences opening on
``É``, ``Á`` or ``Ñ`` split as English ones do. The capital may follow an
opening ``¿``, ``¡`` or ``«``. A full stop ending a known abbreviation does not
split.

Any run longer than ``max_sentence_chars`` with no boundary in it (OCR text, a
list with no full stops) is then cut between words, so one such run cannot turn
a whole document into a single chunk.
"""

import logging
import re
import unicodedata

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_ABBREV = {
    "Dr.",
    "Mr.",
    "Mrs.",
    "Ms.",
    "Prof.",
    "Sr.",
    "Jr.",
    "Inc.",
    "Ltd.",
    "Co.",
    "Corp.",
    "vs.",
    "etc.",
    "e.g.",
    "i.e.",
    "U.S.",
    "U.K.",
    "Fig.",
    "fig.",
    "Eq.",
    "eq.",
    "Ref.",
    "ref.",
}

# Closing marks that stay with the sentence they close.
_CLOSERS = "\"'”’)]}»"
# Marks that open a Spanish question or exclamation, or a French quotation,
# ahead of its capital. English quotes and brackets are deliberately absent:
# the original splitter never broke before them either.
_OPENERS = "¿¡«"

# A closer may follow the stop directly (`.)`, `."`) or after the space French
# typography puts before it (`. »`).
_END_RE = re.compile(f"[.!?…]+(?: ?[{re.escape(_CLOSERS)}])*(?=\\s)")


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate PDF line breaks
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse excess blank lines
    text = re.sub(r"[ \t]+", " ", text)  # normalise whitespace
    return text.strip()


def _starts_sentence(text: str, pos: int) -> bool:
    """Whether a capital (after optional ``¿¡«``) follows the whitespace at *pos*."""
    i = pos
    while i < len(text) and text[i].isspace():
        i += 1
    while i < len(text) and text[i] in _OPENERS:
        i += 1
    while i < len(text) and text[i] == " ":  # French puts a space inside « »
        i += 1
    return i < len(text) and text[i].isupper()


def _is_abbreviation(text: str, end: int) -> bool:
    """Whether the word ending at *end* (just past its full stop) is a known abbreviation."""
    start = end
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    return text[start:end] in _ABBREV


def _scan_spans(text: str) -> list[tuple[int, int]]:
    """Sentence spans of *text* by punctuation, stripped of surrounding whitespace."""
    ends = [
        m.end()
        for m in _END_RE.finditer(text)
        if not _is_abbreviation(text, m.end()) and _starts_sentence(text, m.end())
    ]
    spans: list[tuple[int, int]] = []
    start = 0
    for end in [*ends, len(text)]:
        s, e = start, end
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if s < e:
            spans.append((s, e))
        start = end
    return spans


_spacy_nlp: "object | bool | None" = None


def _get_spacy():
    """Lazy spaCy loader. Caches False to avoid re-attempting after ImportError."""
    global _spacy_nlp
    if _spacy_nlp is False:
        return None
    if _spacy_nlp is None:
        try:
            import spacy

            try:
                _spacy_nlp = spacy.load(
                    "en_core_web_sm",
                    disable=["ner", "tagger", "parser"],
                )
                _spacy_nlp.add_pipe("sentencizer")
            except OSError:
                _spacy_nlp = spacy.blank("en")
                _spacy_nlp.add_pipe("sentencizer")
            log.debug("spaCy sentencizer loaded.")
        except ImportError:
            _spacy_nlp = False
            return None
    return _spacy_nlp


def _spacy_spans(nlp, text: str) -> list[tuple[int, int]]:
    """spaCy's sentences as spans of *text*, found in order by their text."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sent in nlp(text).sents:
        piece = sent.text.strip()
        if not piece:
            continue
        start = text.find(piece, cursor)
        if start < 0:
            continue
        spans.append((start, start + len(piece)))
        cursor = start + len(piece)
    return spans


def _cap_span(text: str, span: tuple[int, int], limit: int) -> list[tuple[int, int]]:
    """Cut a span longer than *limit* into pieces no longer than it.

    Between words, packing as many whole words into each piece as fit; a single
    word longer than the limit is cut inside it.
    """
    start, end = span
    pieces: list[tuple[int, int]] = []
    while end - start > limit:
        cut = text.rfind(" ", start, start + limit + 1)
        if cut <= start:
            cut = start + limit
        pieces.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if start < end:
        pieces.append((start, end))
    return pieces


def _sentence_spans(text: str, max_sentence_chars: int | None = None) -> list[tuple[int, int]]:
    """``(start, end)`` of each sentence of already-cleaned *text*."""
    if not text.strip():
        return []
    limit = max_sentence_chars if max_sentence_chars is not None else cfg.max_sentence_chars
    nlp = _get_spacy()
    spans = _spacy_spans(nlp, text) if nlp is not None else _scan_spans(text)
    if limit <= 0:
        return spans
    return [piece for span in spans for piece in _cap_span(text, span, limit)]


def _split_sentences(text: str) -> list[str]:
    cleaned = clean_text(text)
    return [cleaned[s:e] for s, e in _sentence_spans(cleaned)]


def sentence_window_chunks(
    text: str,
    window: int = 5,
    overlap: int = 1,
    min_chars: int = 80,
    max_sentence_chars: int | None = None,
) -> list[str]:
    """Overlapping windows of *window* sentences, dropping any shorter than *min_chars*.

    ``max_sentence_chars`` defaults to the setting of the same name.
    """
    cleaned = clean_text(text)
    sentences = [cleaned[s:e] for s, e in _sentence_spans(cleaned, max_sentence_chars)]
    if not sentences:
        return []
    step = max(1, window - overlap)
    out: list[str] = []
    for i in range(0, len(sentences), step):
        chunk = " ".join(sentences[i : i + window])
        if len(chunk) >= min_chars:
            out.append(chunk)
    return out


def trim_to_sentences(text: str, max_sentences: int) -> str:
    """First ``max_sentences`` sentences of *text*, cleaned.

    Lives here so sentence boundaries agree with :func:`sentence_window_chunks` —
    anything trimmed for embedding is later windowed by the same splitter.
    """
    cleaned = clean_text(text or "")
    if not cleaned or max_sentences <= 0:
        return cleaned
    sentences = _split_sentences(cleaned)
    if len(sentences) <= max_sentences:
        return cleaned
    return " ".join(sentences[:max_sentences])
