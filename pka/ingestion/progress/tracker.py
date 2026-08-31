"""The registry of live sync states and every write into it.

One lock guards ``_states``. Nothing in here touches the database or the
connectors: callers compute their counts first and hand them in. Reading is
:func:`snapshot_states`, which returns detached copies, so serialization
(:mod:`pka.ingestion.progress.view`) never runs under the lock.
"""

from __future__ import annotations

import copy
import threading
import time

from pka.constants import ALL_SOURCES
from pka.ingestion.progress.state import (
    STANDARD_PHASES,
    JobKind,
    StopReason,
    SyncState,
    apply_db_counts,
    apply_monotonic_processed,
    apply_monotonic_total,
    ensure_standard_phases,
    idle_state,
    normalize_phases,
    phase_map,
)
from pka.ingestion.registry import phase_spec

_lock = threading.Lock()
_states: dict[str, SyncState] = {}


def _get_or_create(source: str, **defaults) -> SyncState:
    """Caller must hold ``_lock``."""
    state = _states.get(source)
    if not state:
        state = SyncState(source=source, **defaults)
        _states[source] = state
    return state


# ── Reads ────────────────────────────────────────────────────────────────────


def is_running(source: str) -> bool:
    with _lock:
        state = _states.get(source)
        return state is not None and state.status == "running"


def check_stop(source: str) -> StopReason | None:
    with _lock:
        state = _states.get(source)
        if state and state.status == "running":
            return state.stop_requested
    return None


def should_stop(progress_key: str | None) -> StopReason | None:
    """:func:`check_stop` for callers whose progress key is optional."""
    return check_stop(progress_key) if progress_key else None


def snapshot_states(source: str | None = None) -> dict[str, SyncState]:
    """Detached copies of the requested states — safe to read without the lock."""
    with _lock:
        sources = [source] if source else list(ALL_SOURCES)
        return {
            src: copy.deepcopy(_states[src]) if src in _states else idle_state(src)
            for src in sources
        }


# ── Job lifecycle ────────────────────────────────────────────────────────────


def begin_job(source: str, job: JobKind, phase: str = "loading") -> None:
    with _lock:
        state = _get_or_create(source)
        ensure_standard_phases(state)
        state.status = "running"
        state.active_job = job
        state.phase = phase
        state.error = None
        state.failed = 0
        state.stop_requested = None
        state.started_at = time.time()
        state.finished_at = None


def begin(source: str, phase: str = "loading") -> None:
    begin_job(source, "metadata", phase=phase)


def begin_ingest(source: str, corpus: int) -> None:
    """Pin shared corpus totals before slow connector work.

    A source that plans its own phases is left alone: its ingest discovers the
    work first and sets the totals itself.
    """
    if phase_spec(source).plans_own_phases:
        return
    if corpus > 0:
        set_corpus_total(source, corpus)
    skip_phase(source, "fetching")


def begin_metadata_sync(source: str, pending: int, baseline: int) -> None:
    """Start metadata import progress using live archive counts vs source corpus size."""
    corpus = baseline + pending
    with _lock:
        state = _get_or_create(source, status="running", active_job="metadata")
        ensure_standard_phases(state)
        state.metadata_baseline = baseline
        state.metadata_pending = pending
        state.metadata_sync_active = True
        meta = phase_map(state)["metadata"]
        meta.total = corpus
        meta.processed = baseline
        state.phase_index = 0
        state.phase = "metadata"
        state.total = corpus
        state.processed = baseline
        state.status = "running"
        state.active_job = "metadata"


def finish(
    source: str,
    error: str | None = None,
    *,
    stopped: StopReason | None = None,
) -> None:
    with _lock:
        state = _states.get(source)
        if not state:
            return
        if error:
            state.status = "error"
            state.error = error
        elif stopped == "cancel":
            state.status = "cancelled"
        elif stopped == "pause":
            state.status = "paused"
        else:
            state.status = "done"
        state.finished_at = time.time()
        state.stop_requested = None
        state.active_job = None
        state.metadata_sync_active = False


def reset(source: str) -> None:
    with _lock:
        _states.pop(source, None)


def set_job_result(source: str, result: dict | None) -> None:
    with _lock:
        state = _states.get(source)
        if state:
            state.last_result = result


# ── Phase plan ───────────────────────────────────────────────────────────────


def set_corpus_total(source: str, total: int) -> None:
    """Set shared corpus size on all phases (authoritative job scope)."""
    with _lock:
        state = _get_or_create(source, status="running")
        ensure_standard_phases(state)
        for plan in state.phases:
            plan.total = total
        normalize_phases(state)


def plan_pipeline(source: str, phases: list[tuple[str, int]]) -> None:
    """Define pipeline phase totals (maps to standard metadata/fetching/embedding)."""
    with _lock:
        state = _get_or_create(source, status="running")
        ensure_standard_phases(state)
        plans = phase_map(state)
        for raw_name, total in phases:
            name = raw_name
            if name in plans:
                apply_monotonic_total(plans[name], total)
                if state.status == "running" and state.phase == name:
                    state.total = plans[name].total
        state.phases = [plans[n] for n in STANDARD_PHASES]
        normalize_phases(state)


def set_phase(source: str, phase: str, total: int) -> None:
    """Activate a sync phase without regressing cumulative progress."""
    if phase not in STANDARD_PHASES:
        raise ValueError(f"Unknown progress phase: {phase!r}")
    with _lock:
        state = _get_or_create(source, status="running")
        ensure_standard_phases(state)
        plans = phase_map(state)
        state.phase_index = STANDARD_PHASES.index(phase)
        plan = plans[phase]
        apply_monotonic_total(plan, total)
        state.phase = phase
        state.total = plan.total
        state.processed = plan.processed
        state.status = "running"
        normalize_phases(state)


def set_total(source: str, total: int, phase: str = "embedding") -> None:
    set_phase(source, phase, total)


def skip_phase(source: str, phase: str) -> None:
    """Mark a pipeline phase complete without processing items."""
    with _lock:
        state = _states.get(source)
        if not state:
            return
        for plan in state.phases:
            if plan.name == phase:
                if plan.total > 0:
                    apply_monotonic_processed(plan, plan.total)
                break
        normalize_phases(state)


# ── Item progress ────────────────────────────────────────────────────────────


def advance(source: str, *, failed: bool = False, phase: str | None = None) -> None:
    with _lock:
        state = _states.get(source)
        if not state:
            return
        idx = state.phase_index
        if phase and phase in STANDARD_PHASES:
            idx = STANDARD_PHASES.index(phase)
        if state.phases and idx < len(state.phases):
            plan = state.phases[idx]
            if plan.name == "fetching":
                if failed:
                    plan.failure += 1
                else:
                    plan.success += 1
                plan.processed = plan.success + plan.failure
            else:
                plan.processed += 1
            if plan.total > 0 and not state.metadata_sync_active:
                plan.processed = min(plan.processed, plan.total)
            state.processed = plan.processed
            state.total = plan.total
        else:
            state.processed += 1
        if failed:
            state.failed += 1
        normalize_phases(state)


def tick(progress_key: str | None, *, failed: bool = False, phase: str | None = None) -> None:
    """:func:`advance` for callers whose progress key is optional."""
    if progress_key:
        advance(progress_key, failed=failed, phase=phase)


# ── DB-derived display counts ────────────────────────────────────────────────


def hydrate(
    source: str,
    totals: dict[str, int],
    processed: dict[str, int] | None = None,
    fetch_outcomes: dict[str, int] | None = None,
) -> None:
    """Replace display totals/processed from DB counts (authoritative when not running)."""
    with _lock:
        state = _states.get(source)
        if state and state.status == "running":
            return
        state = _get_or_create(source, status="idle")
        apply_db_counts(state, totals, processed, fetch_outcomes)


# ── Stop requests ────────────────────────────────────────────────────────────


def request_cancel(source: str) -> bool:
    with _lock:
        state = _states.get(source)
        if not state or state.status != "running":
            return False
        state.stop_requested = "cancel"
        return True


def request_pause(source: str) -> bool:
    with _lock:
        state = _states.get(source)
        if not state or state.status != "running":
            return False
        state.stop_requested = "pause"
        return True
