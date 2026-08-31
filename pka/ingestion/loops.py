"""Shared iteration helpers for ingestion item loops."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Literal

from pka.ingestion.progress import should_stop, tick

log = logging.getLogger(__name__)

MetadataOutcome = Literal["skipped", "processed", "dry_run"]


def run_metadata_loop(
    items: Iterable,
    *,
    known: dict[str, int],
    get_source_id: Callable,
    persist: Callable[[object], MetadataOutcome],
    progress_key: str | None = None,
    skip_when_in_known: bool = True,
) -> dict:
    """Persist new metadata rows, ticking progress for each item it acts on.

    Items already in ``known`` are part of the baseline the job started from, so
    ticking for them would count them twice.
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    for item in items:
        if stop := should_stop(progress_key):
            stats["stopped"] = stop
            break
        source_id = get_source_id(item)
        if skip_when_in_known and source_id in known:
            stats["skipped"] += 1
            continue
        failed = False
        ticked = True
        try:
            outcome = persist(item)
            if outcome == "skipped":
                # ``persist`` found it already stored (or excluded): baseline work.
                stats["skipped"] += 1
                ticked = False
            else:
                stats["processed"] += 1
        except Exception as exc:
            log.exception("Metadata persist failed for %s: %s", source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            if ticked:
                tick(progress_key, failed=failed)

    return stats


def run_embed_loop(
    items: Iterable,
    *,
    should_skip: Callable[[object], bool],
    process: Callable[[object], bool],
    progress_key: str | None = None,
    on_error_log: Callable[[object, Exception], None] | None = None,
) -> dict:
    """Embed items; tick progress only when ``process`` returns True (work attempted)."""
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}

    for item in items:
        if stop := should_stop(progress_key):
            stats["stopped"] = stop
            break
        if should_skip(item):
            stats["skipped"] += 1
            continue
        failed = False
        ticked = False
        try:
            processed, chunks = process(item)
            if processed:
                stats["processed"] += 1
                stats["chunks"] += chunks
                ticked = True
            else:
                stats["skipped"] += 1
        except Exception as exc:
            if on_error_log:
                on_error_log(item, exc)
            else:
                log.exception("Embed failed: %s", exc)
            stats["failed"] += 1
            failed = True
            ticked = True
        finally:
            if ticked:
                tick(progress_key, failed=failed)

    return stats
