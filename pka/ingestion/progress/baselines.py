"""DB counts feeding the progress display: job-start seeds and status aggregates.

The only module under ``progress/`` allowed to touch the database or the source
connectors. It reads counts and hands them to the tracker; it never reaches into
state itself.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa

from pka.constants import ALL_SOURCES, FetchStatus, Source
from pka.db.schema import chunks, documents, images
from pka.ingestion.pending_metadata import count_pending_metadata, source_corpus_size
from pka.ingestion.progress.state import apply_db_counts
from pka.ingestion.progress.tracker import hydrate, snapshot_states
from pka.ingestion.progress.view import to_dict
from pka.ingestion.registry import phase_spec
from pka.ingestion.source_access import (
    calibre_available,
    images_available,
    youtube_available,
)

log = logging.getLogger(__name__)


def _pending_metadata_count(src: str) -> int:
    try:
        return count_pending_metadata(src)
    except Exception as exc:
        log.warning("Pending metadata count failed for %s: %s", src, exc)
        return 0


def _embedded_doc_count(con, src: str) -> int:
    return con.execute(
        sa.select(sa.func.count(sa.distinct(chunks.c.document_id)))
        .select_from(chunks.join(documents, chunks.c.document_id == documents.c.id))
        .where(documents.c.source == src)
    ).scalar() or 0


def _fetch_status_counts(con, src: str) -> dict[str, int]:
    rows = con.execute(
        sa.select(documents.c.fetch_status, sa.func.count())
        .where(documents.c.source == src)
        .group_by(documents.c.fetch_status)
    ).fetchall()
    return {status: count for status, count in rows}


def _doc_source_stats(con, src: str) -> dict[str, int]:
    stats = _fetch_status_counts(con, src)
    stats["embedded"] = _embedded_doc_count(con, src)
    return stats


def _display_corpus_total(src: str, archive_count: int) -> int:
    """Shared phase total for progress bars (matches ingest ``set_corpus_total`` scope)."""
    try:
        n = source_corpus_size(src)
        if n > 0:
            return n
    except Exception as exc:
        log.warning("Source corpus size failed for %s: %s", src, exc)
    return archive_count


def _image_embedded():
    """Predicate for "this image has been through the embed pass".

    ``indexed_at`` is what ``ingest_image`` stamps at the end of that pass, and
    what every other surface uses to tell a registered image from an ingested
    one (``_image_already_embedded``, the ``/images`` gallery, the browse list).
    Counting vector ids instead would undercount as soon as a pass is disabled —
    with ``clip_enabled`` off nothing writes ``clip_vector_id``, and
    ``text_vector_id`` has never been written at all (image chunks are keyed by
    ``document_id`` in the ``chunks`` table), so the old check reported zero
    embedded images for a fully ingested library.
    """
    return images.c.indexed_at.isnot(None)


def get_phase_baselines(
    engine,
    src: str,
) -> tuple[dict[str, int], dict[str, int], dict[str, int] | None]:
    """Return (totals, processed, fetch_outcomes) for progress bars from DB counts.

    Phase totals use archive row counts only (Firefox idle mechanism). Remaining
    source items not yet imported are surfaced via ``pending_metadata_by_source``
    in ``build_ingestion_status`` and the stats summary line. A running job is
    never served from these counts — its worker owns them.
    """
    fetch_outcomes: dict[str, int] | None = None

    with engine.connect() as con:
        if src == Source.IMAGE:
            archive_count = con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar() or 0
            embedded = con.execute(
                sa.select(sa.func.count()).select_from(images).where(
                    _image_embedded()
                )
            ).scalar() or 0
            corpus = _display_corpus_total(src, archive_count)
            return (
                {"metadata": corpus, "fetching": corpus, "embedding": corpus},
                {"metadata": archive_count, "fetching": archive_count, "embedding": embedded},
                None,
            )

        doc_count = con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.source == src)
        ).scalar() or 0

        embedded = _embedded_doc_count(con, src)
        corpus = _display_corpus_total(src, doc_count)

        fetching_done = doc_count
        if not phase_spec(src).tracks_embedding:
            counts = _fetch_status_counts(con, src)
            success = counts.get(str(FetchStatus.FETCHED), 0) + counts.get(
                str(FetchStatus.SKIPPED), 0
            )
            failure = counts.get(str(FetchStatus.UNFETCHABLE), 0)
            fetching_done = success + failure
            fetch_outcomes = {"success": success, "failure": failure}

        return (
            {"metadata": corpus, "fetching": corpus, "embedding": corpus},
            {"metadata": doc_count, "fetching": fetching_done, "embedding": embedded},
            fetch_outcomes,
        )


# A job that has ended still owns its bars: it worked on a corpus it pinned, and
# archive row counts must not redraw what it left on screen.
_JOB_ENDED = ("done", "cancelled", "paused", "error")


def _pinned_corpus(state) -> int:
    """Corpus size the current or last job pinned on its phases, or 0."""
    return max((plan.total for plan in state.phases), default=0)


def display_snapshot(engine, src: str) -> dict:
    """Serialized progress for one source — the read path, with no side effects.

    While a job runs its worker owns the counters, so the state is served as-is.
    Otherwise the bars come from the archive, applied to the detached copy this
    call is serializing; the shared state is left alone.
    """
    state = snapshot_states(src)[src]
    if state.status == "running":
        return to_dict(state)

    totals, processed, fetch_outcomes = get_phase_baselines(engine, src)
    job_corpus = _pinned_corpus(state) if state.status in _JOB_ENDED else 0
    if job_corpus > 0:
        totals = dict.fromkeys(totals, job_corpus)
    apply_db_counts(state, totals, processed, fetch_outcomes)
    return to_dict(state)


def seed_progress_from_db(engine, src: str) -> None:
    """Replace progress display from authoritative DB counts."""
    totals, processed, fetch_outcomes = get_phase_baselines(engine, src)
    job_corpus = _pinned_corpus(snapshot_states(src)[src])
    if job_corpus > 0:
        totals = dict.fromkeys(totals, job_corpus)
    hydrate(src, totals, processed, fetch_outcomes)


def source_counts(engine, src: str) -> dict:
    """The per-source slice of ``/ingestion/status`` a source panel actually reads.

    Streamed alongside progress so a running sync needs no second endpoint. The
    unfiltered ``/ingestion/status`` still serves the sidebar and the index page.
    """
    with engine.connect() as con:
        if src == Source.IMAGE:
            registered = con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar() or 0
            embedded = con.execute(
                sa.select(sa.func.count()).select_from(images).where(_image_embedded())
            ).scalar() or 0
            fetch_stats = {
                "registered": registered,
                "embedded": embedded,
                "pending": max(0, registered - embedded),
            }
        else:
            fetch_stats = _doc_source_stats(con, src)
    return {
        "pending_metadata": _pending_metadata_count(src),
        "fetch": fetch_stats,
        "unavailable": _source_unavailable().get(src),
    }


def _source_unavailable() -> dict[str, str | None]:
    calibre_ok, calibre_msg = calibre_available()
    images_ok, images_msg = images_available()
    youtube_ok, youtube_msg = youtube_available()
    return {
        Source.CALIBRE: None if calibre_ok else calibre_msg,
        Source.IMAGE: None if images_ok else images_msg,
        Source.YOUTUBE: None if youtube_ok else youtube_msg,
    }


def build_ingestion_status(engine) -> dict:
    """Aggregate counts for ``GET /ingestion/status``."""
    with engine.connect() as con:
        doc_total = con.execute(
            sa.select(sa.func.count()).select_from(documents)
        ).scalar() or 0
        image_total = con.execute(
            sa.select(sa.func.count()).select_from(images)
        ).scalar() or 0
        by_source: dict[str, int] = {}
        pending_metadata_by_source: dict[str, int] = {}
        fetch_by_source: dict[str, dict[str, int]] = {}
        for src in ALL_SOURCES:
            pending_metadata_by_source[src] = _pending_metadata_count(src)
            if src == Source.IMAGE:
                n = image_total
                embedded = con.execute(
                    sa.select(sa.func.count()).select_from(images).where(
                        _image_embedded()
                    )
                ).scalar() or 0
                by_source[src] = n
                fetch_by_source[src] = {
                    "registered": n,
                    "embedded": embedded,
                    "pending": max(0, n - embedded),
                }
            else:
                n = con.execute(
                    sa.select(sa.func.count()).select_from(documents)
                    .where(documents.c.source == src)
                ).scalar() or 0
                by_source[src] = n
                fetch_by_source[src] = _doc_source_stats(con, src)
        unfetchable = con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.fetch_status == str(FetchStatus.UNFETCHABLE))
        ).scalar()
        pending = con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.fetch_status == str(FetchStatus.PENDING))
        ).scalar()
    return {
        "total": doc_total + image_total,
        "by_source": by_source,
        "pending_metadata_by_source": pending_metadata_by_source,
        "fetch_by_source": fetch_by_source,
        "unfetchable": unfetchable,
        "pending": pending,
        "source_unavailable": _source_unavailable(),
    }
