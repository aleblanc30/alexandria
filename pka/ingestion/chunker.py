"""
Text cleaning and sentence-window chunking.

Default sentence splitter uses an abbreviation-aware regex; a spaCy
sentencizer is used instead when the ``spacy`` package is importable.
The choice is made lazily on first call.
"""
import logging
import re
import unicodedata

log = logging.getLogger(__name__)

_SIMPLE_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

_ABBREV = {
    "Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "Sr.", "Jr.",
    "Inc.", "Ltd.", "Co.", "Corp.",
    "vs.", "etc.", "e.g.", "i.e.",
    "U.S.", "U.K.",
    "Fig.", "fig.", "Eq.", "eq.", "Ref.", "ref.",
}


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)   # de-hyphenate PDF line breaks
    text = re.sub(r"\n{3,}", "\n\n", text)         # collapse excess blank lines
    text = re.sub(r"[ \t]+", " ", text)            # normalise whitespace
    return text.strip()


def _split_sentences_naive(text: str) -> list[str]:
    """Regex splitter with abbreviation protection."""
    for a in _ABBREV:
        text = text.replace(a, a.replace(".", "<ABBR>"))
    parts = _SIMPLE_SENT_RE.split(text)
    return [p.replace("<ABBR>", ".").strip() for p in parts if p.strip()]


_spacy_nlp: "object | bool | None" = None


def _get_spacy():
    """Lazy spaCy loader. Caches False to avoid re-attempting after ImportError."""
    global _spacy_nlp
    if _spacy_nlp is False:
        return None
    if _spacy_nlp is None:
        try:
            import spacy  # type: ignore
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


def _split_sentences(text: str) -> list[str]:
    nlp = _get_spacy()
    if nlp is None:
        return _split_sentences_naive(text)
    doc = nlp(text)
    return [s.text.strip() for s in doc.sents if s.text.strip()]


def sentence_window_chunks(
    text: str,
    window: int = 5,
    overlap: int = 1,
    min_chars: int = 80,
) -> list[str]:
    sentences = _split_sentences(clean_text(text))
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
