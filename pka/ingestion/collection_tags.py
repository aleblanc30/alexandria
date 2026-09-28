"""Zotero collections and Firefox bookmark folders as ``collection`` overlay tags.

The source's own record stays in ``source_collections``, one slash-joined path
per row; this module derives tags from it (DESIGN.md §3.7). Every path segment
becomes a tag, so filtering on a parent folder also finds what is filed under
its subfolders:

    Firefox  "toolbar/Research/Distributed Systems" -> Research, Distributed Systems
    Zotero   "Thesis/Chapter 2"                     -> Thesis, Chapter 2

Deriving rather than storing means a change to these rules applies to the
whole archive through :func:`backfill_collection_tags`, without a resync.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from typing import Any

import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import Source, TagOrigin
from pka.db.engine import get_engine
from pka.db.schema import overlay_tags, source_collections
from pka.db.tags import sync_overlay_tags

log = logging.getLogger(__name__)

#: Sources whose collections become tags. Calibre series, subreddits and
#: YouTube playlists are in ``source_collections`` too, but not tagged.
TAGGED_SOURCES = (Source.ZOTERO, Source.FIREFOX)

# The top-level folders every Firefox profile has, under the names places.sqlite
# stores them by (the browser localises them only for display). They hold
# thousands of bookmarks each and say nothing about them. Their parent, the
# root folder, has an empty title, or "root" in some profiles.
_FIREFOX_ROOTS = frozenset({"menu", "toolbar", "unfiled", "mobile"})


def normalize_collection_tags(
    collections: list[str],
    source: Source | str,
    max_depth: int | None = None,
) -> list[str]:
    """The tags one document's collection paths stand for, in path order.

    Each path is split on ``/``; a Firefox root is dropped when it heads the
    path (a folder of the same name deeper down is kept); empty segments,
    single characters and names in ``collection_tag_exclude`` are skipped; at
    most *max_depth* segments are kept from the top. Case is kept as written,
    but a tag that differs from an earlier one only in case is dropped. A
    collection name that itself contains ``/`` splits into two tags.

    The document cap (``collection_tag_max_documents``) needs the whole
    archive, so it is applied by the callers, not here.
    """
    if str(source) not in {str(s) for s in TAGGED_SOURCES}:
        return []
    depth = cfg.collection_tag_max_depth if max_depth is None else max_depth
    excluded = {name.casefold() for name in cfg.collection_tag_exclude}
    seen: set[str] = set()
    out: list[str] = []
    for path in collections:
        segments = [s for s in (s.strip() for s in (path or "").split("/")) if s]
        if str(source) == Source.FIREFOX:
            if segments[:1] == ["root"]:
                segments = segments[1:]
            if segments[:1] and segments[0] in _FIREFOX_ROOTS:
                segments = segments[1:]
        segments = [s for s in segments if len(s) > 1 and s.casefold() not in excluded]
        for tag in segments[: max(depth, 0)]:
            if tag.casefold() not in seen:
                seen.add(tag.casefold())
                out.append(tag)
    return out


def _load_collections() -> dict[tuple[int, str], list[str]]:
    """Every tagged source's collection paths, by ``(document_id, source)``."""
    with get_engine().connect() as con:
        rows = con.execute(
            sa.select(
                source_collections.c.document_id,
                source_collections.c.source,
                source_collections.c.collection,
            )
            .where(source_collections.c.source.in_([str(s) for s in TAGGED_SOURCES]))
            .order_by(source_collections.c.document_id, source_collections.c.id)
        ).fetchall()
    by_doc: dict[tuple[int, str], list[str]] = defaultdict(list)
    for doc_id, src, collection in rows:
        by_doc[(doc_id, src)].append(collection)
    return by_doc


def _document_counts(by_doc: dict[tuple[int, str], list[str]]) -> Counter[str]:
    """Documents per tag across both sources, keyed by the case-folded tag."""
    counts: Counter[str] = Counter()
    for (_, src), collections in by_doc.items():
        counts.update(t.casefold() for t in normalize_collection_tags(collections, src))
    return counts


def _over_cap(counts: Counter[str]) -> frozenset[str]:
    """Case-folded tags on more documents than ``collection_tag_max_documents``."""
    cap = cfg.collection_tag_max_documents
    if cap <= 0:
        return frozenset()
    return frozenset(t for t, n in counts.items() if n > cap)


# The over-cap set for ingestion, which tags one document at a time and cannot
# count the archive for each. Recomputed after _CAP_TTL_SECONDS; a document
# ingested meanwhile is counted at the next recompute or backfill.
_CAP_TTL_SECONDS = 300.0
_cap_cache: tuple[float, frozenset[str]] | None = None


def _cached_over_cap() -> frozenset[str]:
    global _cap_cache
    now = time.monotonic()
    if _cap_cache is None or now - _cap_cache[0] > _CAP_TTL_SECONDS:
        _cap_cache = (now, _over_cap(_document_counts(_load_collections())))
    return _cap_cache[1]


def sync_collection_tags(document_id: int, collections: list[str], source: Source | str) -> None:
    """Make a document's collection tags match its collections.

    Tags over ``collection_tag_max_documents`` are left out, by the same count
    :func:`backfill_collection_tags` uses. A no-op when
    ``collection_tags_enabled`` is off; the backfill is what clears the
    existing ones then.
    """
    if not cfg.collection_tags_enabled:
        return
    tags = normalize_collection_tags(collections, source)
    capped = _cached_over_cap() if tags else frozenset()
    tags = [t for t in tags if t.casefold() not in capped]
    sync_overlay_tags(document_id, tags, TagOrigin.COLLECTION)


def backfill_collection_tags(
    source: Source | str | None = None,
    *,
    dry_run: bool = False,
    top: int = 15,
) -> dict[str, Any]:
    """Derive collection tags for every document from ``source_collections``.

    Reads SQLite only, so it needs no source database and no network, and it
    converges: a re-run changes nothing, a renamed folder loses its old tag.
    With ``collection_tags_enabled`` off it removes every collection tag
    instead. ``dry_run`` reports what it would write and writes nothing.

    Returns per-source ``documents`` (with at least one tag), ``tags`` (rows)
    and ``distinct``, plus ``top``: the most used tags kept, with their
    document counts. ``capped`` lists the tags dropped by
    ``collection_tag_max_documents`` with the count that dropped them; the
    count is over both sources, since they share one tag namespace, whichever
    *source* is being written.
    """
    global _cap_cache
    sources = [str(source)] if source else [str(s) for s in TAGGED_SOURCES]
    by_doc = _load_collections()
    with get_engine().connect() as con:
        tagged_before = {
            r[0]
            for r in con.execute(
                sa.select(overlay_tags.c.document_id)
                .where(overlay_tags.c.origin == str(TagOrigin.COLLECTION))
                .distinct()
            )
        }

    enabled = cfg.collection_tags_enabled
    totals = _document_counts(by_doc)
    capped = _over_cap(totals)
    _cap_cache = (time.monotonic(), capped)

    stats: dict[str, Any] = {
        "enabled": enabled,
        "dry_run": dry_run,
        "capped": sorted(((t, totals[t]) for t in capped), key=lambda x: (-x[1], x[0])),
        "sources": {},
    }
    counts: dict[str, Counter[str]] = {s: Counter() for s in sources}
    documents: Counter[str] = Counter()
    for (doc_id, src), collections in by_doc.items():
        if src not in sources:
            continue
        tags = normalize_collection_tags(collections, src) if enabled else []
        tags = [t for t in tags if t.casefold() not in capped]
        if tags:
            documents[src] += 1
            counts[src].update(tags)
        if not dry_run:
            sync_overlay_tags(doc_id, tags, TagOrigin.COLLECTION)

    # A document tagged by an earlier run whose collections have all gone
    # would otherwise keep its tags.
    orphans = tagged_before - {doc_id for doc_id, _ in by_doc}
    if not dry_run and source is None:
        for doc_id in orphans:
            sync_overlay_tags(doc_id, [], TagOrigin.COLLECTION)

    for src in sources:
        stats["sources"][src] = {
            "documents": documents[src],
            "tags": sum(counts[src].values()),
            "distinct": len(counts[src]),
            "top": counts[src].most_common(top),
        }
    return stats
