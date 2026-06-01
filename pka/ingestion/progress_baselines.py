"""DB-derived ingestion progress baselines and status aggregates."""
from __future__ import annotations

import logging

import sqlalchemy as sa

from pka.constants import ALL_SOURCES, FetchStatus, Source
from pka.db.schema import chunks, documents, images
from pka.ingestion import sync_progress as sp
from pka.ingestion.pending_metadata import count_pending_metadata
from pka.ingestion.source_access import calibre_available, images_available

log = logging.getLogger(__name__)


def get_phase_baselines(
    engine,
    src: str,
) -> tuple[dict[str, int], dict[str, int], dict[str, int] | None]:
    """Return (totals, processed, fetch_outcomes) for progress bars from DB counts."""
    fetch_outcomes: dict[str, int] | None = None
    with engine.connect() as con:
        if src == Source.IMAGE:
            total = con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar() or 0
            embedded = con.execute(
                sa.select(sa.func.count()).select_from(images).where(
                    images.c.clip_vector_id.isnot(None)
                    | images.c.text_vector_id.isnot(None)
                )
            ).scalar() or 0
            return (
                {"metadata": total, "fetching": total, "embedding": total},
                {"metadata": total, "fetching": total, "embedding": embedded},
                None,
            )

        doc_count = con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.source == src)
        ).scalar() or 0

        embedded = con.execute(
            sa.select(sa.func.count(sa.distinct(chunks.c.document_id)))
            .select_from(
                chunks.join(documents, chunks.c.document_id == documents.c.id)
            )
            .where(documents.c.source == src)
        ).scalar() or 0

        fetching_done = doc_count
        if src == Source.FIREFOX:
            rows = con.execute(
                sa.select(documents.c.fetch_status, sa.func.count())
                .where(documents.c.source == src)
                .group_by(documents.c.fetch_status)
            ).fetchall()
            counts = {status: count for status, count in rows}
            success = counts.get(str(FetchStatus.FETCHED), 0) + counts.get(
                str(FetchStatus.SKIPPED), 0
            )
            failure = counts.get(str(FetchStatus.UNFETCHABLE), 0)
            fetching_done = success + failure
            fetch_outcomes = {"success": success, "failure": failure}

        return (
            {"metadata": doc_count, "fetching": doc_count, "embedding": doc_count},
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
    sp.hydrate(src, totals, processed, fetch_outcomes)


def build_ingestion_status(engine) -> dict:
    """Aggregate counts for ``GET /ingestion/status``."""
    with engine.connect() as con:
        total = con.execute(
            sa.select(sa.func.count()).select_from(documents)
        ).scalar()
        by_source: dict[str, int] = {}
        pending_metadata_by_source: dict[str, int] = {}
        fetch_by_source: dict[str, dict[str, int]] = {}
        for src in ALL_SOURCES:
            n = con.execute(
                sa.select(sa.func.count()).select_from(documents)
                .where(documents.c.source == src)
            ).scalar()
            by_source[src] = n
            try:
                pending_metadata_by_source[src] = count_pending_metadata(src)
            except Exception as exc:
                log.warning("Pending metadata count failed for %s: %s", src, exc)
                pending_metadata_by_source[src] = 0
            if src == Source.FIREFOX:
                rows = con.execute(
                    sa.select(documents.c.fetch_status, sa.func.count())
                    .where(documents.c.source == src)
                    .group_by(documents.c.fetch_status)
                ).fetchall()
                fetch_by_source[src] = {status: count for status, count in rows}
                embedded = con.execute(
                    sa.select(sa.func.count(sa.distinct(chunks.c.document_id)))
                    .select_from(
                        chunks.join(documents, chunks.c.document_id == documents.c.id)
                    )
                    .where(documents.c.source == src)
                ).scalar() or 0
                fetch_by_source[src]["embedded"] = embedded
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
        "total": total,
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
