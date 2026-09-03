"""Re-run an enrichment pass over already-ingested documents.

The retrigger that ``purge summaries`` needs, per
``planning/PURGE_AND_PROVENANCE_PLAN.md`` §5.2.1. Every other purge target is
self-retriggering — clearing the artifact its pipeline's skip gate checks is
enough to make the next sync redo the work — but the summary gate is keyed on
"does this document have any chunk at all", which a summary purge deliberately
leaves true. Without this pass, purging summaries would be a trap: the artifact
is gone and only a full source purge and re-fetch brings it back.

**The body text is not retained verbatim.** After ingestion it survives only as
``chunks.text``: whitespace-normalised and cut into overlapping sentence
windows. So this pass summarises text reassembled from those chunks rather than
re-fetching every URL — re-fetching would destroy the expensive network work to
redo the cheap inference, which is the exact workflow this feature exists to
eliminate. :func:`reassemble_chunk_text` undoes the overlap; the joins are
imperfect where the chunker's ``min_chars`` filter dropped a short window, and
a summariser is robust to that in a way an extractor would not be. Retaining
raw text (plan §5.2.2) retires the compromise and makes this exact.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from pka.constants import EnrichmentKind, Source
from pka.db.queries import get_engine
from pka.db.schema import chunks, documents
from pka.enrichment_runs import run_scope
from pka.ingestion.chunker import _split_sentences
from pka.ingestion.core import _SUMMARY_FLAGS, attach_summary_chunk
from pka.purge import body_chunk_predicate

log = logging.getLogger(__name__)

# Only these sources generate a summary at all (pka.ingestion.core._SUMMARY_FLAGS
# / DESIGN.md §3.2); the rest would be counted as candidates and then skipped.
SUMMARY_SOURCES = tuple(_SUMMARY_FLAGS)


def reassemble_chunk_text(texts: list[str]) -> str:
    """Join overlapping sentence-window chunks back into prose.

    ``sentence_window_chunks`` advances by ``window - overlap`` sentences, so
    consecutive chunks share their boundary sentences verbatim. Rather than
    trusting the configured overlap (which may have changed since ingestion),
    each chunk is matched against the previous one by longest shared
    sentence run and the duplicate head dropped. A chunk that shares nothing —
    because the window between them was too short to be kept — is appended
    whole, leaving a gap rather than duplicated fragments.
    """
    out: list[str] = []
    for text in texts:
        sentences = _split_sentences(text or "")
        if not sentences:
            continue
        if out:
            overlap = min(len(out), len(sentences))
            while overlap > 0 and out[-overlap:] != sentences[:overlap]:
                overlap -= 1
            sentences = sentences[overlap:]
        out.extend(sentences)
    return " ".join(out)


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
