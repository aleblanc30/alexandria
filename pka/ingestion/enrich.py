"""Re-run an enrichment pass over already-ingested documents.

The retrigger that ``purge summaries`` needs. Every other purge target is
self-retriggering — clearing the artifact its pipeline's skip gate checks is
enough to make the next sync redo the work — but the summary gate is keyed on
"does this document have any chunk at all", which a summary purge deliberately
leaves true. Without this pass, purging summaries would be a trap: the artifact
is gone and only a full source purge and re-fetch brings it back.

**Where the body text comes from.** A document ingested since retention shipped
has it verbatim in ``document_texts``, and
that is what this pass summarises. Anything older has no stored row — retention
is not backfilled, because a reassembly stored as if it were the original would
be a lie the audit use case would then read — so for those documents the pass
falls back to text reassembled from ``chunks.text``, which is
whitespace-normalised and cut into overlapping chunks.
:func:`reassemble_chunk_text` undoes the overlap; the joins are imperfect where
the chunker's ``min_chars`` filter dropped a short chunk, and a summariser is
robust to that in a way an extractor would not be. Re-fetching instead was never
an option: it would destroy the expensive network work to redo the cheap
inference, which is the exact workflow this feature exists to eliminate.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from pka.constants import EnrichmentKind, Source
from pka.db.engine import get_engine
from pka.db.schema import chunks, documents
from pka.enrichment_runs import run_scope
from pka.ingestion.chunker import clean_text
from pka.ingestion.core import _SUMMARY_FLAGS, attach_summary_chunk
from pka.ingestion.text_store import load_document_text
from pka.purge import body_chunk_predicate

log = logging.getLogger(__name__)

# Only these sources generate a summary at all (pka.ingestion.core._SUMMARY_FLAGS
# / DESIGN.md §3.2); the rest would be counted as candidates and then skipped.
SUMMARY_SOURCES = tuple(_SUMMARY_FLAGS)


# A short overlap is more likely a coincidence ("the" ending one chunk and
# starting the next) than a repeat, and is kept rather than merged, unless it
# is a whole sentence, as the one-sentence overlap of older chunks often was.
_MIN_OVERLAP_CHARS = 12
_SENTENCE_END = tuple(".!?…\"'”’)]»")


def _overlap(prev: str, nxt: str) -> int:
    """Length of the longest head of *nxt* that *prev* ends with, on word bounds.

    One pass of the Knuth-Morris-Pratt failure function over ``nxt + sep +
    prev``: its final value is that length, in time linear in the two.
    """
    head = nxt[: len(prev)]
    s = head + "\x00" + prev[-len(head) :] if head else ""
    fail = [0] * len(s)
    for i in range(1, len(s)):
        k = fail[i - 1]
        while k and s[i] != s[k]:
            k = fail[k - 1]
        if s[i] == s[k]:
            k += 1
        fail[i] = k
    k = fail[-1] if s else 0
    # Shrink to an overlap that is whole words at both ends.
    while k:
        whole_words = (k == len(nxt) or nxt[k].isspace()) and (
            k == len(prev) or prev[-k - 1].isspace()
        )
        if whole_words and (k >= _MIN_OVERLAP_CHARS or nxt[:k].endswith(_SENTENCE_END)):
            return k
        k = fail[k - 1]
    return 0


def reassemble_chunk_text(texts: list[str]) -> str:
    """Join overlapping chunks back into prose.

    Consecutive chunks repeat the end of one at the start of the next: whole
    sentences for sentence-window chunks, whole words for the token chunks that
    replaced them. Rather than trusting the configured overlap (which may have
    changed since ingestion), each chunk is matched against the text so far by
    its longest repeated head and that head dropped. A chunk that repeats
    nothing, because the chunk between them was too short to be kept, is
    appended whole, leaving a gap rather than duplicated fragments.
    """
    out = ""
    for text in texts:
        piece = clean_text(text or "")
        if not piece:
            continue
        if not out:
            out = piece
            continue
        k = _overlap(out, piece)
        out = out + piece[k:] if k else f"{out} {piece}"
    return out


def _summary_candidates(con, source: str | None, limit: int | None) -> list[sa.Row]:
    """Documents with body chunks but no cached summary."""
    sources = [str(source)] if source is not None else [str(s) for s in SUMMARY_SOURCES]
    q = (
        sa.select(documents.c.id, documents.c.source, documents.c.title)
        .where(documents.c.generated_summary.is_(None))
        .where(documents.c.source.in_(sources))
        .where(
            sa.exists().where(
                sa.and_(chunks.c.document_id == documents.c.id, body_chunk_predicate())
            )
        )
        .order_by(documents.c.id)
    )
    if limit is not None:
        q = q.limit(limit)
    return list(con.execute(q).fetchall())


def _body_text(con, doc_id: int) -> str:
    """The document's body: the retained text when there is one, else a reassembly.

    The ladder, not a replacement: retention has no backfill, so every document
    ingested before it shipped still reaches the reassembly branch and must keep
    working.
    """
    stored = load_document_text(doc_id)
    if stored:
        return stored
    rows = con.execute(
        sa.select(chunks.c.text)
        .where(chunks.c.document_id == doc_id)
        .where(body_chunk_predicate())
        .order_by(chunks.c.chunk_index)
    ).fetchall()
    return reassemble_chunk_text([r[0] or "" for r in rows])


def enrich_summaries(
    *,
    source: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Generate a summary for every document missing one.

    Returns ``{"candidates", "summarised", "skipped"}``. ``skipped`` counts
    documents whose summary did not come back — an empty reassembly, or the
    source's summary flag being off, which :func:`attach_summary_chunk` checks
    (it is the single gate for the whole mechanism, so this pass must not
    second-guess it and must never enable inference the settings did not).
    """
    eng = get_engine()
    with eng.connect() as con:
        candidates = _summary_candidates(con, source, limit)

    stats = {"candidates": len(candidates), "summarised": 0, "skipped": 0}
    if dry_run:
        return stats

    # The run this pass's summaries are stamped with opens on the first one that
    # actually infers and closes here, so a pass that summarises nothing (every
    # candidate skipped, or the flag off) leaves no run row behind.
    with run_scope(EnrichmentKind.SUMMARY):
        for row in candidates:
            with eng.connect() as con:
                text = _body_text(con, row.id)
            if not text.strip():
                stats["skipped"] += 1
                continue
            added = attach_summary_chunk(
                row.id,
                text,
                Source(row.source),
                title=row.title or "",
            )
            if added:
                stats["summarised"] += 1
            else:
                stats["skipped"] += 1

    log.info(
        "Summary enrichment finished: %d/%d summarised, %d skipped",
        stats["summarised"],
        stats["candidates"],
        stats["skipped"],
    )
    return stats


KINDS = {"summary": enrich_summaries}


def enrich(kind: str, **kwargs) -> dict[str, int]:
    """Run one enrichment pass by name.

    Parameterised by ``kind`` from the start so this stays a single entry point
    rather than growing a parallel "regenerate X" pipeline per artifact.
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown enrichment kind {kind!r}; expected one of {sorted(KINDS)}")
    return KINDS[kind](**kwargs)
