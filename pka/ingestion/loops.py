"""Shared iteration helpers for ingestion item loops."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Literal

log = logging.getLogger(__name__)

MetadataOutcome = Literal["skipped", "processed", "dry_run"]


def _metadata_tick(progress_key: str | None, *, failed: bool) -> None:
    if progress_key and failed:
        from pka.ingestion.sync_progress import advance
        advance(progress_key, failed=True)


def _embed_tick(progress_key: str | None, *, failed: bool) -> None:
    if progress_key:
        from pka.ingestion.sync_progress import advance
        advance(progress_key, failed=failed)


def _check_stop(progress_key: str | None) -> str | None:
    if not progress_key:
        return None
    from pka.ingestion.sync_helpers import should_stop
    return should_stop(progress_key)


def run_metadata_loop(
    items: Iterable,
    *,
    known: dict[str, int],
    get_source_id: Callable,
    persist: Callable[[object], MetadataOutcome],
    progress_key: str | None = None,
    skip_when_in_known: bool = True,
) -> dict:
    """Persist new metadata rows; progress is poll-driven (failure ticks only)."""
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    for item in items:
        if (stop := _check_stop(progress_key)):
            stats["stopped"] = stop
            break
        source_id = get_source_id(item)
        if skip_when_in_known and source_id in known:
            stats["skipped"] += 1
            continue
        failed = False
        try:
            outcome = persist(item)
            if outcome == "skipped":
                stats["skipped"] += 1
            elif outcome == "dry_run":
                stats["processed"] += 1
            else:
                stats["processed"] += 1
        except Exception as exc:
            log.exception("Metadata persist failed for %s: %s", source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            _metadata_tick(progress_key, failed=failed)

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
        if (stop := _check_stop(progress_key)):
            stats["stopped"] = stop
            break
        if should_skip(item):
            stats["skipped"] += 1
            continue
        failed = False
        tick = False
        try:
            processed, chunks = process(item)
            if processed:
                stats["processed"] += 1
                stats["chunks"] += chunks
                tick = True
            else:
                stats["skipped"] += 1
        except Exception as exc:
            if on_error_log:
                on_error_log(item, exc)
            else:
                log.exception("Embed failed: %s", exc)
            stats["failed"] += 1
            failed = True
            tick = True
        finally:
            if tick:
                _embed_tick(progress_key, failed=failed)

    return stats
