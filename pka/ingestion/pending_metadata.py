"""Compare source connectors against the archive to count pending metadata imports."""
from __future__ import annotations

import sqlalchemy as sa

from pka.constants import Source
from pka.db.queries import document_index, get_engine
from pka.db.schema import documents, images
from pka.ingestion.dev_limits import take
from pka.ingestion.source_access import try_load_calibre_books, try_scan_images


def archive_document_count(source: Source | str) -> int:
    """Rows already stored in the archive for ``source``."""
    src = str(source)
    eng = get_engine()
    with eng.connect() as con:
        if src == Source.IMAGE:
            return con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar() or 0
        return con.execute(
            sa.select(sa.func.count()).select_from(documents)
            .where(documents.c.source == src)
        ).scalar() or 0


def count_pending_metadata(source: Source | str) -> int:
    """Source items not yet present in the archive (metadata still to import)."""
    src = str(source)
    if src == Source.FIREFOX:
        from pka.connectors.firefox import load_bookmarks

        known = set(document_index(Source.FIREFOX))
        return sum(
            1 for bm in take(load_bookmarks()) if bm.source_id not in known
        )

    if src == Source.ZOTERO:
        from pka.connectors.zotero import ensure_zotero_copy, load_item_keys

        dst = ensure_zotero_copy()
        keys = set(take(sorted(load_item_keys(copy_path=dst, skip_copy=True))))
        known = set(document_index(Source.ZOTERO))
        return sum(1 for key in keys if key not in known)

    if src == Source.CALIBRE:
        books, unavailable = try_load_calibre_books()
        if unavailable:
            return 0
        known = set(document_index(Source.CALIBRE))
        return sum(1 for book in take(books) if book.source_id not in known)

    if src == Source.IMAGE:
        from pka.ingestion.image_pipeline import _image_already_indexed

        images, unavailable = try_scan_images()
        if unavailable:
            return 0
        pending = 0
        for img in take(images):
            if _image_already_indexed(img.path) is None:
                pending += 1
        return pending

    return 0


def source_corpus_size(source: Source | str) -> int:
    """Source connector item count at job scope (respects dev ingestion cap).

    Used to pin ingest phase totals before slow connector I/O, matching the
    Firefox ``_plan_counts`` / ``set_corpus_total`` pattern.
    """
    src = str(source)
    if src == Source.FIREFOX:
        from pka.connectors.firefox import load_bookmarks

        return len(take(load_bookmarks()))

    if src == Source.ZOTERO:
        from pka.connectors.zotero import ensure_zotero_copy, load_item_keys

        dst = ensure_zotero_copy()
        return len(take(sorted(load_item_keys(copy_path=dst, skip_copy=True))))

    if src == Source.CALIBRE:
        books, unavailable = try_load_calibre_books()
        if unavailable:
            return 0
        books = take(books)
        n_files = sum(
            1 for b in books if b.preferred_path and b.preferred_path.exists()
        )
        return n_files or len(books)

    if src == Source.IMAGE:
        images, unavailable = try_scan_images()
        if unavailable:
            return 0
        return len(take(images))

    return 0


def metadata_job_progress(source: Source | str, baseline: int, pending_total: int) -> tuple[int, int]:
    """Return ``(archive_count, corpus_total)`` for an active metadata job.

    ``archive_count`` is the live row count in Alexandria; ``corpus_total`` is the
    source size at job start (``baseline + pending_total``), never below the
    current archive count.
    """
    current = archive_document_count(source)
    total = max(baseline + pending_total, current)
    return current, total
