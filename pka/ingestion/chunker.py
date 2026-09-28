"""
Text cleaning and sentence-window chunking, for any script.

Sentences are found by :func:`_sentence_spans`:

- Text mostly in spaced scripts (Latin, Cyrillic, Greek, Arabic, Devanagari…)
  uses spaCy's sentencizer when the ``spacy`` package is importable, else a
  punctuation scan. spaCy tokenises with English rules, which cannot split a
  run of CJK text that has no spaces in it, so dense-script text always takes
  the scan.
- The scan splits after ``.!?…`` when the next letter starts a sentence in
  any script (uppercase, or a letter of a script without case), keeping the
  English abbreviation list, and after terminators such as ``。।؟`` whether
  or not a space follows, as it does after ``.!?`` straight after a
  dense-script character (``clean_text``'s NFKC folds ``！？`` into ASCII).
- Any run longer than ``max_sentence_chars`` with no boundary in it (Thai,
  which marks no sentence ends, or unpunctuated OCR) is cut, at whitespace
  where it has any and by characters where it has none. Without that cut a
  document with no recognised boundary became a single chunk.

Lengths are weighted by :func:`_weight`: a character of a dense script (CJK,
kana, Hangul, Thai…) carries roughly what three Latin characters do, so
``min_chunk_chars`` and ``max_sentence_chars`` mean about the same amount of
text in every script.
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

# Terminators that end a sentence whether or not whitespace follows: CJK
# full-width and half-width stops, Devanagari danda, Arabic question mark,
# Urdu full stop, Ethiopic, Myanmar and Tibetan-style stops.
_HARD_TERMINATORS = "。！？｡।॥؟۔።။"
# Terminators that end a sentence only before whitespace and a sentence start.
_SOFT_TERMINATORS = ".!?…"
# Closing marks that stay with the sentence they close.
_CLOSERS = "\"'”’)]}»」』）】》〉"

_HARD_RE = re.compile(f"[{re.escape(_HARD_TERMINATORS)}]+[{re.escape(_CLOSERS)}]*")
_SOFT_RE = re.compile(f"[{re.escape(_SOFT_TERMINATORS)}]+[{re.escape(_CLOSERS)}]*(?=\\s)")

# Scripts written without spaces between words, or with a syllable per
# character: CJK ideographs, kana, Hangul, Thai, Lao, Khmer, Myanmar.
_DENSE_RANGES = (
    (0x1000, 0x109F),  # Myanmar
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1780, 0x17FF),  # Khmer
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2FA1F),  # CJK Extensions B+
)
_DENSE_WEIGHT = 3
_DENSE_CLASS = "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _DENSE_RANGES)
# `clean_text`'s NFKC folds the full-width ！？ into ASCII !?, which CJK writes
# with no space after. So `.!?` straight after a dense-script character ends a
# sentence whether or not whitespace follows.
_DENSE_SOFT_RE = re.compile(
    f"(?<=[{_DENSE_CLASS}])[{re.escape(_SOFT_TERMINATORS)}]+[{re.escape(_CLOSERS)}]*"
)
# Share of letters in dense scripts above which spaCy's English tokeniser is
# not trusted with the text.
_DENSE_SHARE_FOR_SCAN = 0.3


def _is_dense(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _DENSE_RANGES)


def _weight(text: str) -> int:
    """Length in Latin-equivalent characters: a dense-script character counts 3."""
    return len(text) + (_DENSE_WEIGHT - 1) * sum(1 for ch in text if _is_dense(ch))


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate PDF line breaks
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse excess blank lines
    text = re.sub(r"[ \t]+", " ", text)  # normalise whitespace
    return text.strip()


def _starts_sentence(text: str, pos: int) -> bool:
    """Whether the text after the whitespace at *pos* opens a new sentence."""
    i = pos
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        return False
    ch = text[i]
    # Uppercase in any cased script, or any letter of a script without case
    # (Arabic, Hebrew, Devanagari, CJK…), where there is no capital to wait for.
    return ch.isupper() or (ch.isalpha() and not ch.islower())


def _is_abbreviation(text: str, end: int) -> bool:
    """Whether the word ending at *end* (just past its full stop) is a known abbreviation."""
    start = end
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    return text[start:end] in _ABBREV


def _scan_boundaries(text: str) -> list[int]:
    """Offsets just past each sentence end found by punctuation."""
    ends = {m.end() for m in _HARD_RE.finditer(text)}
    ends.update(m.end() for m in _DENSE_SOFT_RE.finditer(text))
    for m in _SOFT_RE.finditer(text):
        if _is_abbreviation(text, m.end()) or not _starts_sentence(text, m.end()):
            continue
        ends.add(m.end())
    return sorted(e for e in ends if 0 < e < len(text))


def _spans_from_ends(text: str, ends: list[int]) -> list[tuple[int, int]]:
    """Stripped ``(start, end)`` spans between consecutive boundaries."""
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


def _mostly_dense(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    return sum(1 for ch in letters if _is_dense(ch)) / len(letters) > _DENSE_SHARE_FOR_SCAN


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
    """Cut a span heavier than *limit* into pieces no heavier than it.

    At whitespace where the span has any, packing whole words into each piece;
    a single word still over the limit (and a dense run with no spaces at all)
    is cut by characters.
    """
    start, end = span
    if _weight(text[start:end]) <= limit:
        return [span]
    pieces: list[tuple[int, int]] = []
    piece_start = start
    weight = 0
    i = start
    while i < end:
        ch_weight = _DENSE_WEIGHT if _is_dense(text[i]) else 1
        if weight + ch_weight > limit and i > piece_start:
            # Back up to the last whitespace in this piece, if there is one.
            cut = text.rfind(" ", piece_start, i)
            if cut <= piece_start:
                cut = i
            pieces.append((piece_start, cut))
            piece_start = cut
            while piece_start < end and text[piece_start].isspace():
                piece_start += 1
            i = piece_start
            weight = 0
            continue
        weight += ch_weight
        i += 1
    if piece_start < end:
        pieces.append((piece_start, end))
    return [(s, e) for s, e in pieces if s < e]


def _sentence_spans(text: str, max_sentence_chars: int | None = None) -> list[tuple[int, int]]:
    """``(start, end)`` of each sentence of already-cleaned *text*."""
    if not text.strip():
        return []
    limit = max_sentence_chars if max_sentence_chars is not None else cfg.max_sentence_chars
    nlp = None if _mostly_dense(text) else _get_spacy()
    spans = (
        _spacy_spans(nlp, text)
        if nlp is not None
        else _spans_from_ends(text, _scan_boundaries(text))
    )
    if limit <= 0:
        return spans
    return [piece for span in spans for piece in _cap_span(text, span, limit)]


def _split_sentences(text: str) -> list[str]:
    cleaned = clean_text(text)
    return [cleaned[s:e] for s, e in _sentence_spans(cleaned)]


def _join(text: str, spans: list[tuple[int, int]]) -> str:
    """The spans' text, separated by a space only where the source had whitespace.

    A plain ``" ".join`` would space out CJK sentences, and pieces of a word
    cut by characters, that the source wrote with nothing between them.
    """
    out = text[spans[0][0] : spans[0][1]]
    for (_, prev_end), (start, end) in zip(spans, spans[1:], strict=False):
        gap = text[prev_end:start]
        out += (" " if any(ch.isspace() for ch in gap) else gap) + text[start:end]
    return out


def sentence_window_chunks(
    text: str,
    window: int = 5,
    overlap: int = 1,
    min_chars: int = 80,
    max_sentence_chars: int | None = None,
) -> list[str]:
    """Overlapping windows of *window* sentences, dropping any lighter than *min_chars*.

    ``min_chars`` and ``max_sentence_chars`` are :func:`_weight` lengths.
    ``max_sentence_chars`` defaults to the setting of the same name.
    """
    cleaned = clean_text(text)
    spans = _sentence_spans(cleaned, max_sentence_chars)
    if not spans:
        return []
    step = max(1, window - overlap)
    out: list[str] = []
    for i in range(0, len(spans), step):
        chunk = _join(cleaned, spans[i : i + window])
        if _weight(chunk) >= min_chars:
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
    spans = _sentence_spans(cleaned)
    if len(spans) <= max_sentences:
        return cleaned
    return _join(cleaned, spans[:max_sentences])
