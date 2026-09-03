"""Model provenance for enrichment artifacts.

Records *which backend, which model, with which settings* produced a summary,
description or OCR pass, so a purge can target "the work the old model did" and
leave the work you are keeping alone (``planning/PURGE_AND_PROVENANCE_PLAN.md``
§6). Without this, "purge the summaries made by the old model" is not hard but
unimplementable: nothing in the schema records who made any artifact.

**A run spans a pass, not a document.** One row per source sync (or per enrich
pass), stamped onto every artifact it produces — the same shape as
``cluster_runs``, which this generalises.

**Runs open lazily, at the moment inference is about to happen.** A sync that
summarises nothing (the flag is off, everything was cached) opens no run and
leaves no empty row, and the model name recorded is the one actually resolved
for the call rather than whatever config said at startup. The corollary is that
callers do not open runs; they stamp, and opening happens underneath.

**An unstamped artifact means unknown provenance, and that is a real answer.**
Anything made before this shipped, or outside an open run, keeps ``NULL``. It is
never backfilled with a guess — "made by whatever is configured now" is exactly
the lie a provenance-filtered purge would then act on.

This is not a job history. Live job state belongs to :mod:`pka.ingestion.progress`;
this module must not grow into a task queue.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import EnrichmentKind, RunStatus
from pka.db.queries import get_engine
from pka.db.schema import enrichment_runs

log = logging.getLogger(__name__)

# A `running` row older than this is assumed abandoned by a crashed process.
# Generous on purpose: a full image pass over a large folder on modest hardware
# genuinely runs for many hours, and wrongly failing a live run is worse than
# leaving a dead one lying around a little longer.
STALE_RUN_SECONDS = 24 * 3600

_lock = threading.Lock()
#: kind -> open run_id. Ambient rather than threaded through every runner, the
#: same way pka.ingestion.progress carries per-source job state.
_current: dict[str, int] = {}


# ── Backend resolution ───────────────────────────────────────────────────────


def _summary_backend() -> tuple[str, str, dict]:
    from pka.ingestion import summarize as sz
    from pka.ollama_chat import resolve_chat_model

    # The *resolved* model, not cfg.chat_model: that defaults to "" meaning
    # "auto-detect the first non-embedding model from /api/tags", so storing the
    # config value would record nothing useful on the most common local setup.
    return (
        cfg.chat_provider,
        resolve_chat_model(),
        {
            "temperature": sz._TEMPERATURE,
            "chunk_char_limit": sz.CHUNK_CHAR_LIMIT,
            "max_chunks_per_pass": sz.MAX_CHUNKS_PER_PASS,
            "max_reduce_depth": sz.MAX_REDUCE_DEPTH,
        },
    )


def _resolve_backend(kind: str) -> tuple[str | None, str | None, dict]:
    """``(provider, model, parameters)`` for *kind*, best effort.

    Never raises: a provider that cannot report its model (an unreachable
    Ollama, say) must not cost the artifact its ingestion, and a run row with a
    NULL model is still better provenance than no row at all.
    """
    # Dispatched here rather than through a module-level table so the resolver
    # is looked up when the run opens, not when this module is imported.
    if str(kind) != str(EnrichmentKind.SUMMARY):
        return None, None, {}
    try:
        return _summary_backend()
    except Exception:
        log.debug("Could not resolve the backend for %s runs", kind, exc_info=True)
        return None, None, {}


# ── Lifecycle ────────────────────────────────────────────────────────────────


def reap_stale_runs(*, now: int | None = None) -> int:
    """Fail `running` rows old enough to be abandoned by a crashed process.

    Runs this process still holds open are skipped however old they are — a slow
    pass is not a dead one.
    """
    cutoff = (now or int(time.time())) - STALE_RUN_SECONDS
    with _lock:
        live = list(_current.values())
    stmt = (
        enrichment_runs.update()
        .where(enrichment_runs.c.status == str(RunStatus.RUNNING))
        .where(enrichment_runs.c.started_at < cutoff)
        .values(
            status=str(RunStatus.FAILED),
            finished_at=now or int(time.time()),
            notes="Assumed abandoned: still marked running after a restart",
        )
    )
    if live:
        stmt = stmt.where(enrichment_runs.c.run_id.notin_(live))
    with get_engine().begin() as con:
        n = con.execute(stmt).rowcount
    if n:
        log.info("Reaped %d stale enrichment run(s)", n)
    return n


def open_run(kind: str | EnrichmentKind) -> int:
    """Start a run for *kind* and make it the ambient one. Returns its id."""
    provider, model, parameters = _resolve_backend(str(kind))
    now = int(time.time())
    reap_stale_runs(now=now)
    with get_engine().begin() as con:
        result = con.execute(
            enrichment_runs.insert().values(
                kind=str(kind),
                provider=provider,
                model=model,
                parameters=json.dumps(parameters) if parameters else None,
                started_at=now,
                status=str(RunStatus.RUNNING),
            )
        )
        pk = result.inserted_primary_key
        if pk is None:  # pragma: no cover — a VALUES insert always yields one
            raise RuntimeError("enrichment_runs insert returned no primary key")
        run_id = int(pk[0])
    with _lock:
        _current[str(kind)] = run_id
    log.info(
        "Enrichment run #%d opened: kind=%s provider=%s model=%s", run_id, kind, provider, model
    )
    return run_id


def current_run_id(kind: str | EnrichmentKind) -> int:
    """The ambient run for *kind*, opening one if none is live."""
    with _lock:
        existing = _current.get(str(kind))
    return existing if existing is not None else open_run(kind)


def close_run(
    kind: str | EnrichmentKind,
    *,
    status: str | RunStatus = RunStatus.FINISHED,
    notes: str | None = None,
) -> None:
    """Close the ambient run for *kind*, if one is open. Idempotent."""
    with _lock:
        run_id = _current.pop(str(kind), None)
    if run_id is None:
        return
    with get_engine().begin() as con:
        con.execute(
            enrichment_runs.update()
            .where(enrichment_runs.c.run_id == run_id)
            .values(status=str(status), finished_at=int(time.time()), notes=notes)
        )
    log.info("Enrichment run #%d closed: %s", run_id, status)


def close_all(*, status: str | RunStatus = RunStatus.FINISHED) -> None:
    """Close every ambient run — called when a sync or enrich pass ends."""
    with _lock:
        kinds = list(_current)
    for kind in kinds:
        close_run(kind, status=status)


@contextmanager
def run_scope(kind: str | EnrichmentKind) -> Iterator[None]:
    """Bracket a pass so its run closes as ``finished`` or ``failed``.

    Nothing is opened on entry: if the body never stamps an artifact, no run row
    is written at all.
    """
    try:
        yield
    except Exception:
        close_run(kind, status=RunStatus.FAILED, notes="Pass raised")
        raise
    close_run(kind)


# ── Stamping and counters ────────────────────────────────────────────────────


def record_call(kind: str | EnrichmentKind, chars_sent: int) -> None:
    """Count one provider call against the ambient run for *kind*.

    A no-op when no run is open: counting must never be what opens one, or a
    stray call outside a pass would create a run row with no artifacts.
    """
    with _lock:
        run_id = _current.get(str(kind))
    if run_id is None:
        return
    try:
        with get_engine().begin() as con:
            con.execute(
                enrichment_runs.update()
                .where(enrichment_runs.c.run_id == run_id)
                .values(
                    calls=enrichment_runs.c.calls + 1,
                    chars_sent=enrichment_runs.c.chars_sent + max(0, chars_sent),
                )
            )
    except Exception:
        # Spend accounting must never cost an artifact its ingestion.
        log.debug("Could not record a call against run #%d", run_id, exc_info=True)


def count_artifact(kind: str | EnrichmentKind) -> None:
    """Count one produced artifact against the ambient run for *kind*."""
    with _lock:
        run_id = _current.get(str(kind))
    if run_id is None:
        return
    try:
        with get_engine().begin() as con:
            con.execute(
                enrichment_runs.update()
                .where(enrichment_runs.c.run_id == run_id)
                .values(artifacts=enrichment_runs.c.artifacts + 1)
            )
    except Exception:
        log.debug("Could not count an artifact against run #%d", run_id, exc_info=True)


# ── Reading ──────────────────────────────────────────────────────────────────


def list_runs(kind: str | None = None, limit: int = 100) -> list[dict]:
    """Recent runs, newest first — what made what, when, and at what cost."""
    reap_stale_runs()
    q = sa.select(enrichment_runs).order_by(enrichment_runs.c.run_id.desc()).limit(limit)
    if kind:
        q = q.where(enrichment_runs.c.kind == str(kind))
    with get_engine().connect() as con:
        rows = [dict(r._mapping) for r in con.execute(q).fetchall()]
    for row in rows:
        raw = row.get("parameters")
        row["parameters"] = json.loads(raw) if raw else None
    return rows


def reset_for_tests() -> None:
    """Drop the ambient run state (the suite reuses one process)."""
    with _lock:
        _current.clear()


def _close_on_exit() -> None:
    """Close ambient runs when the process ends normally.

    The API's job skeleton closes its own runs, but every ``alexandria <source>``
    CLI run would otherwise exit with a `running` row that only the 24-hour
    reaper cleans up. One hook here beats the same two lines in seven CLI mains.
    A hard kill still runs nothing — that is what the reaper is for.
    """
    try:
        close_all()
    except Exception:  # interpreter teardown: the engine may already be gone
        pass


atexit.register(_close_on_exit)
