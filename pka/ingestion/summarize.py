"""Local map-reduce summarisation for retrieval enrichment (DESIGN.md §3.2).

Condenses a long document into the 2–4 topical sentences that §3.2 asks for:
enough to say what the document is *about*, short enough that MiniLM does not
truncate the tail away. The result is meant for the ``pass="summary"`` chunk and
for ``doc_embedding`` — it is **embedding text, not display text**, so it is
written for a semantic search to hit, not for a human to read.

Three properties the ingestion runners depend on:

* **Never raises.** Every entry point returns ``None`` on empty input, a provider
  outage, or an unparseable reply. Enrichment must not break an ingestion loop.
* **Never spends a call it does not need.** Input already within the sentence cap
  comes back as-is; most bookmarks and Reddit posts are that short, and paying a
  chat round-trip to re-say two sentences is pure waste.
* **Bounded cost.** Long input is chunked (``CHUNK_CHAR_LIMIT``), each chunk
  summarised, and the partial summaries summarised again — but the number of
  chunks per pass and the number of reduce passes are both capped, so the worst
  case is a fixed number of calls rather than a runaway loop.

**Gating lives at the call site, not here.** ``bookmark_summary_enabled`` is
deliberately *not* checked in this module: the runner that decides a document
deserves a summary is the one place that flag belongs, and checking it here too
would double-gate. Callers that reach this module have already decided.

All chat traffic goes through :func:`pka.ollama_chat.chat_json`, hence through the
configured chat provider — this module never talks to a backend directly and
makes no network calls of its own.
"""
from __future__ import annotations

import logging

from pka.config import settings as cfg
from pka.ingestion.chunker import _split_sentences, clean_text, trim_to_sentences
from pka.ollama_chat import chat_json

log = logging.getLogger(__name__)

# Characters of source text one summarisation call is allowed to see. Anything
# longer is chunked and map-reduced. Sized well under a small local model's
# context so the prompt plus the text still leave room for the answer.
CHUNK_CHAR_LIMIT = 6000

# Chunks a single map pass will summarise. A document longer than
# ``CHUNK_CHAR_LIMIT * MAX_CHUNKS_PER_PASS`` has its tail dropped rather than
# spending an unbounded number of calls: a topical summary is decided by the
# opening body of a document, and cost has to stay predictable during a bulk
# ingest.
MAX_CHUNKS_PER_PASS = 12

# Reduce passes above the map pass. Combined with the truncating base case this
# is what makes the recursion terminate on any input, however large.
MAX_REDUCE_DEPTH = 2

# Deterministic output: the same document should summarise the same way across
# re-ingests, since the result is cached.
_TEMPERATURE = 0.0

_MAP_INTRO = (
    "You are indexing a {noun} for a personal research library.\n"
    "Summarise the excerpt below so the summary can be embedded and retrieved by "
    "semantic search.\n"
)

_REDUCE_INTRO = (
    "You are indexing a {noun} for a personal research library.\n"
    "The numbered items below are partial summaries of consecutive sections of "
    "ONE {noun}. Merge them into a single summary of that {noun}, dropping "
    "repetition, so it can be embedded and retrieved by semantic search.\n"
)

# What the input actually is, and the extra rule that framing earns. Absent a
# material the summariser assumes a self-contained expository document, which is
# the wrong assumption for Reddit: a saved post is one person's writing rather
# than a reference text, and a saved comment is a single turn lifted out of a
# thread that may carry none of its own subject. Summarising either as a
# "document" spends the sentence budget on framing ("the author argues that…")
# instead of on the topics a search would match.
#
# Keys are supplied by the runners; an unknown or missing key falls back to the
# generic framing, so naming a source here is optional.
_MATERIALS: dict[str, tuple[str, str]] = {
    "reddit_post": (
        "Reddit post",
        "- The text is the body of a post written by one user. Summarise what it\n"
        "  is about; do not report that someone posted or asked something.\n",
    ),
    "reddit_comment": (
        "Reddit comment",
        "- The text is ONE comment taken out of its thread, so it may answer\n"
        "  something you cannot see. Summarise the topics and claims the comment\n"
        "  itself carries.\n"
        "- Where the comment only makes sense against the thread it replies to,\n"
        "  use the context line to name the subject.\n"
        "- Do not describe the commenter, their stance, or their tone.\n",
    ),
}

_DEFAULT_NOUN = "document"


def _material_parts(material: str | None) -> tuple[str, str]:
    """``(noun, extra_rules)`` for *material*; generic framing when unknown."""
    return _MATERIALS.get(material or "", (_DEFAULT_NOUN, ""))


_RULES_TAIL = (
    "- State what the material is ABOUT: its subject, field, and the specific\n"
    "  topics, methods, tools, people, or technologies it covers.\n"
    "- Use the wording a searcher looking for this would type. Prefer concrete\n"
    "  nouns over abstractions.\n"
    "- No preamble and no framing: do not open with \"This document\", \"The text\",\n"
    "  or \"The author\". Do not comment on structure, length, or style.\n"
    "- Invent nothing that is not in the text.\n"
    'Respond with ONLY valid JSON: {"summary": "<the summary>"}\n'
    "No markdown, no explanation.\n\n"
)

# Reply keys accepted, in order. The grammar-constrained providers honour
# ``summary``; the fallbacks cost nothing and salvage a stubborn model.
_REPLY_KEYS = ("summary", "text", "content", "abstract")


def _build_prompt(
    text: str,
    max_sentences: int,
    *,
    reduce: bool,
    material: str | None = None,
    context: str | None = None,
) -> str:
    noun, extra_rules = _material_parts(material)
    intro = (_REDUCE_INTRO if reduce else _MAP_INTRO).format(noun=noun)
    label = "Partial summaries" if reduce else "Text"
    plural = "" if max_sentences == 1 else "s"
    # Context sits outside the quoted block on purpose: it is background for
    # reading the text, not material to summarise, and "invent nothing that is
    # not in the text" has to keep meaning the text.
    ctx = (context or "").strip()
    context_line = f"Context (background, do not summarise): {ctx}\n\n" if ctx else ""
    return (
        f"{intro}"
        "Rules:\n"
        f"- At most {max_sentences} sentence{plural}. Fewer is better.\n"
        f"{extra_rules}"
        f"{_RULES_TAIL}"
        f"{context_line}"
        f"{label}:\n\"\"\"\n{text}\n\"\"\"\n"
    )


def _extract_summary(parsed: object) -> str | None:
    """Pull the summary string out of a parsed reply, or ``None``."""
    if not isinstance(parsed, dict):
        return None
    for key in _REPLY_KEYS:
        value = parsed.get(key)
        if isinstance(value, str):
            cleaned = clean_text(value)
            if cleaned:
                return cleaned
    return None


def _summarize_once(
    text: str,
    max_sentences: int,
    model: str | None,
    *,
    reduce: bool = False,
    material: str | None = None,
    context: str | None = None,
) -> str | None:
    """One summarisation call. ``None`` on provider error or unusable reply."""
    prompt = _build_prompt(
        text[:CHUNK_CHAR_LIMIT],
        max_sentences,
        reduce=reduce,
        material=material,
        context=context,
    )
    parsed, err = chat_json(prompt, model=model, temperature=_TEMPERATURE)
    if err:
        log.warning("Summarisation call failed: %s", err)
        return None
    summary = _extract_summary(parsed)
    if summary is None:
        log.warning("Summarisation reply carried no usable summary: %r", parsed)
        return None
    return trim_to_sentences(summary, max_sentences) or None


def _chunk_for_summary(text: str) -> list[str]:
    """Split *text* into at most ``MAX_CHUNKS_PER_PASS`` chunks on sentence bounds.

    Uses the chunker's splitter so a summarisation chunk breaks where a retrieval
    chunk would. An individual sentence longer than the limit (a wall of OCR text
    with no punctuation, say) is hard-sliced — otherwise one pathological sentence
    would defeat the bound.
    """
    pieces: list[str] = []
    for sentence in _split_sentences(text) or [text]:
        if len(sentence) <= CHUNK_CHAR_LIMIT:
            pieces.append(sentence)
        else:
            pieces.extend(
                sentence[i : i + CHUNK_CHAR_LIMIT]
                for i in range(0, len(sentence), CHUNK_CHAR_LIMIT)
            )

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for piece in pieces:
        if current and length + len(piece) + 1 > CHUNK_CHAR_LIMIT:
            chunks.append(" ".join(current))
            if len(chunks) >= MAX_CHUNKS_PER_PASS:
                return chunks
            current, length = [], 0
        current.append(piece)
        length += len(piece) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks[:MAX_CHUNKS_PER_PASS]


def _summarize_recursive(
    text: str,
    max_sentences: int,
    model: str | None,
    depth: int,
    *,
    material: str | None = None,
    context: str | None = None,
) -> str | None:
    """Map-reduce with a hard depth bound.

    Terminates three ways, so no input can loop: the text fits one call; the
    depth budget is spent (the base case *truncates*, it does not recurse); or a
    pass produced a single chunk, meaning further splitting cannot shrink it.
    """
    if len(text) <= CHUNK_CHAR_LIMIT or depth >= MAX_REDUCE_DEPTH:
        return _summarize_once(
            text, max_sentences, model,
            reduce=depth > 0, material=material, context=context,
        )

    chunks = _chunk_for_summary(text)
    if len(chunks) <= 1:
        # Splitting bought nothing (one oversized chunk): summarise it directly
        # rather than recursing on the same text.
        return _summarize_once(
            chunks[0] if chunks else text, max_sentences, model,
            material=material, context=context,
        )

    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        partial = _summarize_once(
            chunk, max_sentences, model,
            reduce=depth > 0, material=material, context=context,
        )
        if partial:
            partials.append(partial)
        else:
            # One bad chunk must not lose the rest of the document.
            log.debug("Dropping chunk %d/%d: no summary returned", index, len(chunks))
    if not partials:
        return None
    if len(partials) == 1:
        # Nothing left to merge; the single surviving partial is the summary.
        return trim_to_sentences(partials[0], max_sentences) or None

    joined = "\n".join(f"{i}. {p}" for i, p in enumerate(partials, start=1))
    return _summarize_recursive(
        joined, max_sentences, model, depth + 1,
        material=material, context=context,
    )


def summarize_text(
    text: str,
    *,
    max_sentences: int | None = None,
    model: str | None = None,
    material: str | None = None,
    context: str | None = None,
) -> str | None:
    """Summarise *text* into at most *max_sentences* sentences for embedding.

    *max_sentences* defaults to ``cfg.summary_max_sentences``. *model* overrides
    the chat provider's configured model.

    *material* names what kind of text this is (see :data:`_MATERIALS`) so the
    prompt can stop calling a Reddit comment a document; an unknown value is
    ignored rather than rejected. *context* is background the text itself omits —
    a comment's thread title, say — handed to the model as a labelled line
    outside the quoted text, never as material to be summarised.

    Returns ``None`` — never raises — when there is nothing to summarise, when
    the cap is meaningless (``<= 0``), or when the provider errors out or answers
    with something unusable. Input already within the cap is returned cleaned and
    unchanged, costing no model call — so *material* and *context* only come into
    play once the text is long enough to be worth a call.
    """
    limit = max_sentences if max_sentences is not None else cfg.summary_max_sentences
    if limit is None or limit <= 0:
        log.debug("Refusing to summarise with a sentence cap of %r", limit)
        return None

    cleaned = clean_text(text or "")
    if not cleaned:
        return None

    # Already short enough to be its own summary — most bookmarks land here.
    if len(_split_sentences(cleaned)) <= limit:
        return trim_to_sentences(cleaned, limit) or None

    try:
        summary = _summarize_recursive(
            cleaned, limit, model, depth=0, material=material, context=context,
        )
    except Exception as exc:  # provider, parsing, anything — enrichment is optional
        log.warning("Summarisation failed: %s", exc)
        return None
    return summary or None


__all__ = [
    "CHUNK_CHAR_LIMIT",
    "MAX_CHUNKS_PER_PASS",
    "MAX_REDUCE_DEPTH",
    "summarize_text",
]
