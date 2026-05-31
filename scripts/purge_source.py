#!/usr/bin/env python
"""Remove all archived data for a source connector.

Usage::

    python scripts/purge_source.py firefox
    python scripts/purge_source.py zotero --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlalchemy as sa

from pka.constants import ALL_SOURCES, Source
from pka.db.queries import get_engine
from pka.db.schema import (
    chunks,
    cluster_assignments,
    documents,
    fetch_log,
    image_tags,
    images,
    overlay_tags,
    reading_list_items,
    source_collections,
    source_tags,
)
from pka.storage import vector_store

log = logging.getLogger("purge_source")

_CHILD_TABLES = (
    reading_list_items,
    cluster_assignments,
    overlay_tags,
    fetch_log,
    chunks,
    source_tags,
    source_collections,
)


def purge_source(source: str, *, dry_run: bool = False) -> dict[str, int]:
    """Delete all archive rows (and vectors) for ``source``."""
    src = str(source)
    if src not in ALL_SOURCES:
        raise ValueError(f"Unknown source {src!r}; expected one of {ALL_SOURCES}")

    if src == Source.IMAGE:
        return _purge_images(dry_run=dry_run)
    return _purge_documents(src, dry_run=dry_run)


def _purge_documents(source: str, *, dry_run: bool = False) -> dict[str, int]:
    eng = get_engine()
    with eng.connect() as con:
        doc_ids = [
            r[0]
            for r in con.execute(
                sa.select(documents.c.id).where(documents.c.source == source)
            ).fetchall()
        ]
        vector_ids = [
            r[0]
            for r in con.execute(
                sa.select(chunks.c.vector_id)
                .where(chunks.c.document_id.in_(doc_ids))
                .where(chunks.c.vector_id.isnot(None))
            ).fetchall()
        ] if doc_ids else []

    counts: dict[str, int] = {"documents": len(doc_ids), "vectors": len(vector_ids)}
    if not doc_ids:
        return counts

    if dry_run:
        with eng.connect() as con:
            for tbl in _CHILD_TABLES:
                counts[tbl.name] = con.execute(
                    sa.select(sa.func.count())
                    .select_from(tbl)
                    .where(tbl.c.document_id.in_(doc_ids))
                ).scalar() or 0
        return counts

    if vector_ids:
        counts["vectors_purged"] = vector_store.purge_vectors(vector_ids)

    with eng.begin() as con:
        for tbl in _CHILD_TABLES:
            result = con.execute(tbl.delete().where(tbl.c.document_id.in_(doc_ids)))
            counts[tbl.name] = result.rowcount
        result = con.execute(documents.delete().where(documents.c.source == source))
        counts["documents"] = result.rowcount

    return counts


def _purge_images(*, dry_run: bool = False) -> dict[str, int]:
    eng = get_engine()
    with eng.connect() as con:
        image_ids = [r[0] for r in con.execute(sa.select(images.c.id)).fetchall()]
        vector_ids: list[str] = []
        for row in con.execute(
            sa.select(images.c.clip_vector_id, images.c.text_vector_id)
        ).fetchall():
            vector_ids.extend(vid for vid in row if vid)

    counts: dict[str, int] = {"images": len(image_ids), "vectors": len(vector_ids)}
    if not image_ids:
        return counts

    if dry_run:
        with eng.connect() as con:
            counts["image_tags"] = con.execute(
                sa.select(sa.func.count())
                .select_from(image_tags)
                .where(image_tags.c.image_id.in_(image_ids))
            ).scalar() or 0
        return counts

    if vector_ids:
        counts["vectors_purged"] = vector_store.purge_vectors(vector_ids)

    with eng.begin() as con:
        result = con.execute(image_tags.delete().where(image_tags.c.image_id.in_(image_ids)))
        counts["image_tags"] = result.rowcount
        result = con.execute(images.delete())
        counts["images"] = result.rowcount

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove archived data for a source.")
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    counts = purge_source(args.source, dry_run=args.dry_run)
    prefix = "Would delete" if args.dry_run else "Deleted"
    for name, count in counts.items():
        log.info("%s %s: %d", prefix, name, count)


if __name__ == "__main__":
    main()
