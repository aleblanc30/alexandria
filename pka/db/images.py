"""The image gate's rejection cache, and removal of a rejected image's rows."""

import time
from typing import Any

import sqlalchemy as sa

from pka.db import engine
from pka.db.schema import (
    chunks,
    cluster_assignments,
    documents,
    fetch_log,
    image_rejections,
    image_tags,
    images,
    overlay_tags,
    reading_list_items,
    source_collections,
    source_tags,
)


def record_image_rejection(
    path: str,
    reason: str,
    text_coverage: float | None = None,
    image_type: str | None = None,
) -> None:
    """Cache an image path rejected by the admission gate (upsert by path)."""
    now = int(time.time())
    with engine.get_engine().begin() as con:
        con.execute(
            sa.text("""
            INSERT INTO image_rejections
                (path, reason, text_coverage, image_type, rejected_at)
            VALUES
                (:path, :reason, :cov, :itype, :now)
            ON CONFLICT(path) DO UPDATE SET
                reason        = excluded.reason,
                text_coverage = excluded.text_coverage,
                image_type    = excluded.image_type,
                rejected_at   = excluded.rejected_at
        """),
            {
                "path": path,
                "reason": reason,
                "cov": text_coverage,
                "itype": image_type,
                "now": now,
            },
        )


def get_rejected_paths() -> set[str]:
    """Return the set of image paths currently in the rejection cache."""
    with engine.get_engine().connect() as con:
        rows = con.execute(sa.select(image_rejections.c.path)).fetchall()
    return {r[0] for r in rows}


def clear_image_rejections() -> int:
    """Empty the gate rejection cache; return the number of rows removed.

    The cache is consulted by the metadata pass (``register_images``) as well as
    the embed pass, so a stale entry keeps a path skipped even after its image
    rows are gone. Cleared on a full image purge and by ``images
    --reset-rejections`` when re-tuning the gate.
    """
    with engine.get_engine().begin() as con:
        return con.execute(image_rejections.delete()).rowcount


# Child tables keyed by document_id, cleared before the parent document row.
_IMAGE_DOC_CHILD_TABLES = (
    reading_list_items,
    cluster_assignments,
    overlay_tags,
    fetch_log,
    chunks,
    source_tags,
    source_collections,
)


def delete_image_document(path: str) -> dict[str, Any]:
    """Delete the ``images`` sidecar + backing ``documents`` row for ``path``.

    Called when the admission gate rejects an image that an earlier metadata
    pass already registered, so no orphan document/image row lingers in browse.
    Returns ``{"chunk_vector_ids", "clip_vector_id"}`` so the caller can purge
    any Chroma vectors (present only if the image had been fully ingested
    before, e.g. under ``--force-reindex``). Idempotent: a no-op when ``path``
    is not registered.
    """
    empty: dict[str, Any] = {"chunk_vector_ids": [], "clip_vector_id": None}
    eng = engine.get_engine()
    with eng.connect() as con:
        row = con.execute(
            sa.select(images.c.id, images.c.document_id, images.c.clip_vector_id).where(
                images.c.path == path
            )
        ).fetchone()
    if row is None:
        return empty
    image_id, doc_id, clip_vid = row

    with eng.connect() as con:
        chunk_vids = [
            r[0]
            for r in con.execute(
                sa.select(chunks.c.vector_id)
                .where(chunks.c.document_id == doc_id)
                .where(chunks.c.vector_id.isnot(None))
            ).fetchall()
        ]

    with eng.begin() as con:
        con.execute(image_tags.delete().where(image_tags.c.image_id == image_id))
        con.execute(images.delete().where(images.c.id == image_id))
        for tbl in _IMAGE_DOC_CHILD_TABLES:
            con.execute(tbl.delete().where(tbl.c.document_id == doc_id))
        con.execute(documents.delete().where(documents.c.id == doc_id))

    return {"chunk_vector_ids": chunk_vids, "clip_vector_id": clip_vid}
