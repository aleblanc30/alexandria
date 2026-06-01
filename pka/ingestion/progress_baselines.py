"""DB-derived ingestion progress baselines and status aggregates."""
from __future__ import annotations

import logging

import sqlalchemy as sa

from pka.constants import ALL_SOURCES, FetchStatus, Source
from pka.db.schema import chunks, documents, images
from pka.ingestion import sync_progress as sp
from pka.ingestion.pending_metadata import count_pending_metadata, source_corpus_size
from pka.ingestion.source_access import calibre_available, images_available

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


def get_phase_baselines(
    engine,
    src: str,
) -> tuple[dict[str, int], dict[str, int], dict[str, int] | None]:
    """Return (totals, processed, fetch_outcomes) for progress bars from DB counts.

    Phase totals use archive row counts only (Firefox idle mechanism). Remaining
    source items not yet imported are surfaced via ``pending_metadata_by_source``
    in ``build_ingestion_status`` and the stats summary line. Active metadata
    jobs use ``metadata_job_progress`` instead of these totals.
    """
    fetch_outcomes: dict[str, int] | None = None

    with engine.connect() as con:
        if src == Source.IMAGE:
            archive_count = con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar() or 0
            embedded = con.execute(
                sa.select(sa.func.count()).select_from(images).where(
                    images.c.clip_vector_id.isnot(None)
                    | images.c.text_vector_id.isnot(None)
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
        if src == Source.FIREFOX:
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


def apply_progress_baselines(engine, src: str) -> None:
    """Hydrate idle state or refresh display during a running ingest job."""
    totals, processed, fetch_outcomes = get_phase_baselines(engine, src)
    if sp.is_running(src):
        sp.refresh_display_from_db(src, totals, processed, fetch_outcomes)
    else:
        sp.hydrate(src, totals, processed, fetch_outcomes)


def seed_progress_from_db(engine, src: str) -> None:
    """Replace progress display from authoritative DB counts."""
    totals, processed, fetch_outcomes = get_phase_baselines(engine, src)
    job_corpus = sp.job_corpus_total(src)
    if job_corpus > 0:
        totals = {name: job_corpus for name in totals}
    sp.hydrate(src, totals, processed, fetch_outcomes)


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
                        images.c.clip_vector_id.isnot(None)
                        | images.c.text_vector_id.isnot(None)
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
    calibre_ok, calibre_msg = calibre_available()
    images_ok, images_msg = images_available()
    return {
        "total": doc_total + image_total,
        "by_source": by_source,
        "pending_metadata_by_source": pending_metadata_by_source,
        "fetch_by_source": fetch_by_source,
        "unfetchable": unfetchable,
        "pending": pending,
        "source_unavailable": {
            Source.CALIBRE: None if calibre_ok else calibre_msg,
            Source.IMAGE: None if images_ok else images_msg,
        },
    }
