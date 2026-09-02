"""Compare source connectors against the archive to count pending metadata imports."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa

from pka.config import settings
from pka.constants import Source
from pka.db.queries import document_index, get_engine
from pka.db.schema import documents, images
from pka.ingestion.dev_limits import take
from pka.ingestion.source_access import try_load_calibre_books, try_scan_images

# ── Source-probe cache ────────────────────────────────────────────────────────
# ``count_pending_metadata`` and ``source_corpus_size`` re-probe the live source
# (Firefox parse, Zotero DB copy, image-folder walk + EXIF) on every call. The
# status/progress endpoints poll them ~2×/sec while a sync runs, so results are
# cached for ``settings.ingestion_probe_cache_ttl_seconds`` and invalidated at
# job start/finish/purge via ``invalidate_source_probes``.
#
# The same cache also backs ``load_firefox_bookmarks`` / ``load_calibre_books`` /
# ``load_scanned_images`` below (kind ``"raw"``): pending + corpus each used to
# re-read the connector independently, and the sync job itself read it again for
# real, so one job start could parse Firefox's ``places.sqlite`` three times.
_probe_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_probe_lock = threading.Lock()


def _cached_probe(kind: str, src: str, compute: Callable[[], Any]) -> Any:
    ttl = settings.ingestion_probe_cache_ttl_seconds
    if ttl <= 0:
        return compute()
    now = time.monotonic()
    key = (kind, src)
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    # Compute outside the lock: the probe is slow I/O and callers tolerate a
    # brief double-compute over serializing every poll behind one another.
    value = compute()
    with _probe_lock:
        _probe_cache[key] = (now + ttl, value)
    return value


def invalidate_source_probes(source: Source | str | None = None) -> None:
    """Drop cached pending/corpus probe results so the next read recomputes.

    Called when the source or archive changes (sync start/finish, purge). Pass a
    source to clear just that one, or ``None`` to clear all.
    """
    with _probe_lock:
        if source is None:
            _probe_cache.clear()
            return
        src = str(source)
        for key in [k for k in _probe_cache if k[1] == src]:
            del _probe_cache[key]


def load_firefox_bookmarks() -> list:
    """Firefox bookmarks connector read, cached like the probes above.

    ``count_pending_metadata``, ``source_corpus_size`` and the Firefox sync job
    itself all need this within one run; sharing the cache means only the first
    of those actually re-parses ``places.sqlite``.
    """

    def compute() -> list:
        from pka.connectors.firefox import load_bookmarks

        return load_bookmarks()

    return _cached_probe("raw", str(Source.FIREFOX), compute)


def load_calibre_books() -> tuple[list, str | None]:
    """Calibre library read, cached like ``load_firefox_bookmarks`` above."""
    return _cached_probe("raw", str(Source.CALIBRE), try_load_calibre_books)


def load_scanned_images() -> tuple[list, str | None]:
    """Image-folder scan, cached like ``load_firefox_bookmarks`` above."""
    return _cached_probe("raw", str(Source.IMAGE), try_scan_images)


def archive_document_count(source: Source | str) -> int:
    """Rows already stored in the archive for ``source``."""
    src = str(source)
    eng = get_engine()
    with eng.connect() as con:
        if src == Source.IMAGE:
            return con.execute(sa.select(sa.func.count()).select_from(images)).scalar() or 0
        return (
            con.execute(
                sa.select(sa.func.count()).select_from(documents).where(documents.c.source == src)
            ).scalar()
            or 0
        )


def count_pending_metadata(source: Source | str) -> int:
    """Source items not yet present in the archive (metadata still to import).

    Cached (see ``_cached_probe``): the underlying probe re-parses/re-scans the
    live source, which is too costly to run on every status poll.
    """
    src = str(source)
    return _cached_probe("pending", src, lambda: _compute_pending_metadata(src))


def _compute_pending_metadata(src: str) -> int:
    if src == Source.REDDIT:
        # Network source: probing here would hit the Reddit API on every status
        # poll. The metadata sync computes its own pending count instead.
        return 0

    if src == Source.FIREFOX:
        known = set(document_index(Source.FIREFOX))
        return sum(
            1 for bm in take(load_firefox_bookmarks(), Source.FIREFOX) if bm.source_id not in known
        )

    if src == Source.ZOTERO:
        from pka.connectors.zotero import ensure_zotero_copy, load_item_keys

        dst = ensure_zotero_copy()
        keys = set(take(sorted(load_item_keys(copy_path=dst, skip_copy=True)), Source.ZOTERO))
        known = set(document_index(Source.ZOTERO))
        return sum(1 for key in keys if key not in known)

    if src == Source.CALIBRE:
        books, unavailable = load_calibre_books()
        if unavailable:
            return 0
        known = set(document_index(Source.CALIBRE))
        return sum(1 for book in take(books, Source.CALIBRE) if book.source_id not in known)

    if src == Source.IMAGE:
        from pka.ingestion.image_pipeline import admitted_images, indexed_image_paths

        scanned, unavailable = load_scanned_images()
        if unavailable:
            return 0
        # ``admitted_images`` mirrors what ``register_images`` will actually
        # persist: gate-rejected paths are skipped there, so counting them as
        # pending leaves a permanent gap the metadata job can never close.
        known_paths = indexed_image_paths()
        return sum(
            1
            for img in admitted_images(take(scanned, Source.IMAGE))
            if str(img.path) not in known_paths
        )

    # YouTube is a network source; never hit the Data API on a status poll.
    # The real pending count is computed inline from loaded videos in
    # ``sync_youtube_metadata``.
    if src == Source.YOUTUBE:
        return 0

    return 0


def source_corpus_size(source: Source | str) -> int:
    """Source connector item count at job scope (respects dev ingestion cap).

    Used to pin ingest phase totals before slow connector I/O, matching the
    Firefox ``_plan_counts`` / ``set_corpus_total`` pattern. Cached (see
    ``_cached_probe``) since the status/progress polls hit it repeatedly and the
    source size is effectively static across a running job.
    """
    src = str(source)
    return _cached_probe("corpus", src, lambda: _compute_source_corpus_size(src))


def _compute_source_corpus_size(src: str) -> int:
    if src == Source.REDDIT:
        # Network source: don't call the Reddit API from status/baseline probes.
        # The ingest job sets its own phase totals from the loaded saved list.
        return 0

    if src == Source.FIREFOX:
        return len(take(load_firefox_bookmarks(), Source.FIREFOX))

    if src == Source.ZOTERO:
        from pka.connectors.zotero import ensure_zotero_copy, load_item_keys

        dst = ensure_zotero_copy()
        return len(take(sorted(load_item_keys(copy_path=dst, skip_copy=True)), Source.ZOTERO))

    if src == Source.CALIBRE:
        books, unavailable = load_calibre_books()
        if unavailable:
            return 0
        books = take(books, Source.CALIBRE)
        n_files = sum(1 for b in books if b.preferred_path and b.preferred_path.exists())
        return n_files or len(books)

    if src == Source.IMAGE:
        from pka.ingestion.image_pipeline import admitted_images

        scanned, unavailable = load_scanned_images()
        if unavailable:
            return 0
        # Gate-rejected paths are skipped by both passes; leaving them in the
        # corpus would hold every phase total above the reachable maximum.
        return len(admitted_images(take(scanned, Source.IMAGE)))

    return 0
