"""Remove all archived data for a source connector.

Usage::

    alexandria purge-source firefox
    alexandria purge-source zotero --dry-run
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator

import sqlalchemy as sa

from pka.cli._logging import setup_logging
from pka.constants import ALL_SOURCES, Source, TagOrigin
from pka.db.queries import get_engine
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
from pka.storage import vector_store

log = logging.getLogger("purge_source")

# overlay_tags is handled separately (see _delete_overlay_tags): by default a
# purge must not destroy user-authored data (Tier 1 — CLAUDE.md /
# PURGE_AND_PROVENANCE_PLAN.md §5.1), so it is filtered to machine origins
# unless include_user_data is set. reading_list_items is Tier 1 outright and is
# never touched here — a purged document simply leaves a dangling id that
# survives a later re-ingest.
_USER_TAG_ORIGINS = (str(TagOrigin.MANUAL), str(TagOrigin.LEARNED))

_CHILD_TABLES = (
    cluster_assignments,
    fetch_log,
    chunks,
    source_tags,
    source_collections,
)

# SQLite binds one variable per id in an ``IN (...)`` list and
# SQLITE_MAX_VARIABLE_NUMBER is 32766, so a source with more documents (or
# images) than that fails outright with "too many SQL variables". Every id list
# below goes through _batches, well under the ceiling.
_ID_BATCH_SIZE = 5_000


def _batches(ids: list) -> Iterator[list]:
    for i in range(0, len(ids), _ID_BATCH_SIZE):
        yield ids[i : i + _ID_BATCH_SIZE]


def purge_source(
    source: str, *, dry_run: bool = False, include_user_data: bool = False
) -> dict[str, int]:
    """Delete all archive rows (and vectors) for ``source``.

    By default, user-authored data survives: ``overlay_tags`` with
    ``origin in (manual, learned)`` and ``reading_list_items`` are left in
    place, since nothing about re-ingesting the source can recreate them. Pass
    ``include_user_data=True`` to delete those too.
    """
    src = str(source)
    if src not in ALL_SOURCES:
        raise ValueError(f"Unknown source {src!r}; expected one of {ALL_SOURCES}")

    if src == Source.IMAGE:
        return _purge_images(dry_run=dry_run, include_user_data=include_user_data)
    return _purge_documents(src, dry_run=dry_run, include_user_data=include_user_data)


def _overlay_tags_where(doc_ids: list, *, include_user_data: bool):
    in_batch = overlay_tags.c.document_id.in_(doc_ids)
    if include_user_data:
        return in_batch
    return sa.and_(in_batch, overlay_tags.c.origin.notin_(_USER_TAG_ORIGINS))


def _purge_documents(
    source: str, *, dry_run: bool = False, include_user_data: bool = False
) -> dict[str, int]:
    eng = get_engine()
    with eng.connect() as con:
        doc_ids = [
            r[0]
            for r in con.execute(
                sa.select(documents.c.id).where(documents.c.source == source)
            ).fetchall()
        ]
        vector_ids: list[str] = []
        for batch in _batches(doc_ids):
            vector_ids.extend(
                r[0]
                for r in con.execute(
                    sa.select(chunks.c.vector_id)
                    .where(chunks.c.document_id.in_(batch))
                    .where(chunks.c.vector_id.isnot(None))
                ).fetchall()
            )

    counts: dict[str, int] = {"documents": len(doc_ids), "vectors": len(vector_ids)}
    if not doc_ids:
        return counts

    if dry_run:
        with eng.connect() as con:
            for tbl in _CHILD_TABLES:
                counts[tbl.name] = sum(
                    con.execute(
                        sa.select(sa.func.count())
                        .select_from(tbl)
                        .where(tbl.c.document_id.in_(batch))
                    ).scalar()
                    or 0
                    for batch in _batches(doc_ids)
                )
            counts["overlay_tags"] = sum(
                con.execute(
                    sa.select(sa.func.count())
                    .select_from(overlay_tags)
                    .where(_overlay_tags_where(batch, include_user_data=include_user_data))
                ).scalar()
                or 0
                for batch in _batches(doc_ids)
            )
            if include_user_data:
                counts["reading_list_items"] = sum(
                    con.execute(
                        sa.select(sa.func.count())
                        .select_from(reading_list_items)
                        .where(reading_list_items.c.document_id.in_(batch))
                    ).scalar()
                    or 0
                    for batch in _batches(doc_ids)
                )
        return counts

    if vector_ids:
        counts["vectors_purged"] = vector_store.purge_vectors(vector_ids)

    with eng.begin() as con:
        for tbl in _CHILD_TABLES:
            counts[tbl.name] = sum(
                con.execute(tbl.delete().where(tbl.c.document_id.in_(batch))).rowcount
                for batch in _batches(doc_ids)
            )
        counts["overlay_tags"] = sum(
            con.execute(
                overlay_tags.delete().where(
                    _overlay_tags_where(batch, include_user_data=include_user_data)
                )
            ).rowcount
            for batch in _batches(doc_ids)
        )
        if include_user_data:
            counts["reading_list_items"] = sum(
                con.execute(
                    reading_list_items.delete().where(reading_list_items.c.document_id.in_(batch))
                ).rowcount
                for batch in _batches(doc_ids)
            )
        result = con.execute(documents.delete().where(documents.c.source == source))
        counts["documents"] = result.rowcount

    return counts


def _purge_images(*, dry_run: bool = False, include_user_data: bool = False) -> dict[str, int]:
    """Purge image data.

    Images are first-class documents (``source=image``): their ``documents``,
    ``chunks`` (text-search vectors), and overlay tags go through the shared
    :func:`_purge_documents` path. On top of that we clear the ``images`` /
    ``image_tags`` sidecar rows and the CLIP vectors, which live in a separate
    Chroma collection (``alexandria_clip``) rather than the chunk collection.
    """
    from pka.db.queries import clear_image_rejections
    from pka.ingestion import image_pipeline

    eng = get_engine()
    with eng.connect() as con:
        image_ids = [r[0] for r in con.execute(sa.select(images.c.id)).fetchall()]
        rejection_count = (
            con.execute(sa.select(sa.func.count()).select_from(image_rejections)).scalar() or 0
        )
        clip_vector_ids = [
            r[0]
            for r in con.execute(
                sa.select(images.c.clip_vector_id).where(images.c.clip_vector_id.isnot(None))
            ).fetchall()
        ]

    # documents / chunks / chunk vectors / overlay tags / etc.
    counts = _purge_documents(
        str(Source.IMAGE), dry_run=dry_run, include_user_data=include_user_data
    )
    counts["images"] = len(image_ids)
    counts["clip_vectors"] = len(clip_vector_ids)
    counts["image_rejections"] = rejection_count

    # The gate rejection cache is keyed by path and consulted by the metadata
    # pass, so it must be cleared even when no image rows remain — otherwise a
    # purge leaves previously-rejected paths skipped forever on re-sync.
    if not dry_run and rejection_count:
        clear_image_rejections()

    if not image_ids:
        return counts

    if dry_run:
        with eng.connect() as con:
            counts["image_tags"] = sum(
                con.execute(
                    sa.select(sa.func.count())
                    .select_from(image_tags)
                    .where(image_tags.c.image_id.in_(batch))
                ).scalar()
                or 0
                for batch in _batches(image_ids)
            )
        return counts

    if clip_vector_ids:
        counts["clip_vectors_purged"] = image_pipeline.delete_clip_vectors(clip_vector_ids)

    with eng.begin() as con:
        counts["image_tags"] = sum(
            con.execute(image_tags.delete().where(image_tags.c.image_id.in_(batch))).rowcount
            for batch in _batches(image_ids)
        )
        result = con.execute(images.delete())
        counts["images"] = result.rowcount

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alexandria purge-source",
        description="Remove archived data for a source.",
    )
    parser.add_argument(
        "source",
        choices=ALL_SOURCES,
        help="Source connector to purge (firefox, zotero, calibre, image)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report row counts without deleting",
    )
    parser.add_argument(
        "--include-user-data",
        action="store_true",
        help=(
            "Also delete manually-applied/learned tags and reading-list entries "
            "for this source (by default those survive the purge)"
        ),
    )
    args = parser.parse_args(argv)

    setup_logging()

    counts = purge_source(
        args.source, dry_run=args.dry_run, include_user_data=args.include_user_data
    )
    prefix = "Would delete" if args.dry_run else "Deleted"
    for name, count in counts.items():
        log.info("%s %s: %d", prefix, name, count)
    return 0


if __name__ == "__main__":
    main()
