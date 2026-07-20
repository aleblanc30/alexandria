"""In-memory sync progress for ingestion background jobs."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Literal

Status = Literal["idle", "running", "done", "error", "paused", "cancelled"]
StopReason = Literal["cancel", "pause"]
JobKind = Literal["metadata", "ingest"]

STANDARD_PHASES = ("metadata", "fetching", "embedding")


@dataclass
class PhasePlan:
    name: str
    total: int = 0
    processed: int = 0
    success: int = 0   # fetching phase: resolved without error
    failure: int = 0   # fetching phase: unfetchable


@dataclass
class SyncState:
    source: str
    status: Status = "idle"
    phase: str = ""
    total: int = 0
    processed: int = 0
    failed: int = 0
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    phases: list[PhasePlan] = field(default_factory=list)
    phase_index: int = 0
    stop_requested: StopReason | None = None
    active_job: JobKind | None = None
    last_result: dict | None = None
    metadata_baseline: int = 0   # archive row count at metadata job start
    metadata_pending: int = 0    # source items not in archive at job start
    metadata_sync_active: bool = False


_lock = threading.Lock()
_states: dict[str, SyncState] = {}


def _idle(source: str) -> SyncState:
    return SyncState(source=source, status="idle")


def _phase_map(state: SyncState) -> dict[str, PhasePlan]:
    return {p.name: p for p in state.phases}


def _ensure_standard_phases(state: SyncState) -> None:
    existing = _phase_map(state)
    state.phases = [
        existing.get(name, PhasePlan(name=name))
        for name in STANDARD_PHASES
    ]


def _overall(state: SyncState) -> tuple[int, int]:
    if state.phases:
        done = sum(p.processed for p in state.phases)
        total = sum(p.total for p in state.phases)
        return done, total
    return state.processed, state.total


def _apply_monotonic_total(plan: PhasePlan, total: int) -> None:
    """Raise phase total to fit new work without shrinking below progress."""
    plan.total = max(plan.total, total)
    if plan.total > 0:
        plan.processed = min(plan.processed, plan.total)


def _apply_monotonic_processed(plan: PhasePlan, processed: int) -> None:
    plan.processed = max(plan.processed, processed)
    if plan.total > 0:
        plan.processed = min(plan.processed, plan.total)


def _normalize_phases(state: SyncState) -> None:
    """One shared corpus total; processed reflects sequential pipeline depth.

    Each item moves metadata → fetching → embedding, so every phase shares the
    same total (corpus size) while processed counts decrease downstream.
    Downstream progress must never inflate upstream phases.

    Firefox fetch+embed is interleaved; embedding progress is not tracked.
    """
    from pka.constants import Source

    _ensure_standard_phases(state)
    meta, fetch, embed = state.phases
    skip_embed = state.source == Source.FIREFOX

    corpus = max(meta.total, fetch.total, 0 if skip_embed else embed.total)
    if corpus > 0:
        meta.total = fetch.total = corpus
        if not skip_embed:
            embed.total = corpus

    if meta.total:
        meta.processed = min(meta.processed, meta.total)

    # Downstream progress implies upstream stages completed, but never
    # raise metadata/fetching because embedding overshot its total.
    if skip_embed:
        fetch.processed = max(fetch.processed, fetch.success + fetch.failure)
    else:
        fetch.processed = max(fetch.processed, embed.processed, fetch.success + fetch.failure)
    if fetch.total:
        upper = meta.processed if meta.processed else fetch.total
        fetch.processed = min(fetch.processed, upper, fetch.total)

    if skip_embed:
        embed.total = 0
        embed.processed = 0
    elif embed.total:
        embed.processed = min(embed.processed, fetch.processed, embed.total)


def _phase_percent(plan: PhasePlan) -> int:
    if plan.total <= 0:
        return 100 if plan.processed > 0 else 0
    return min(100, round(100 * plan.processed / plan.total))


def is_running(source: str) -> bool:
    with _lock:
        state = _states.get(source)
        return state is not None and state.status == "running"


def job_corpus_total(source: str) -> int:
    """Return pinned corpus size from the current/last job phases, or 0."""
    with _lock:
        state = _states.get(source)
        if not state or not state.phases:
            return 0
        return max(p.total for p in state.phases)


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
        if not state:
            state = SyncState(source=source, status="idle")
            _states[source] = state
        _apply_db_counts(state, totals, processed, fetch_outcomes)


def refresh_display_from_db(
    source: str,
    totals: dict[str, int],
    processed: dict[str, int] | None = None,
    fetch_outcomes: dict[str, int] | None = None,
) -> None:
    """Refresh phase processed counts from DB while an ingest job is running.

    Corpus totals are owned by ``set_corpus_total`` at job start (same as Firefox
    ingest); DB baselines must not overwrite them during polling.
    """
    with _lock:
        state = _states.get(source)
        if not state or state.status != "running" or state.active_job != "ingest":
            return
        _apply_db_counts(state, totals, processed, fetch_outcomes, update_totals=False)


def _apply_db_counts(
    state: SyncState,
    totals: dict[str, int],
    processed: dict[str, int] | None,
    fetch_outcomes: dict[str, int] | None,
    *,
    update_totals: bool = True,
) -> None:
    _ensure_standard_phases(state)
    proc = processed or {}
    phase_map = _phase_map(state)
    from pka.constants import Source

    skip_embed = state.source == Source.FIREFOX
    for name in STANDARD_PHASES:
        if skip_embed and name == "embedding":
            continue
        plan = phase_map[name]
        if update_totals and name in totals:
            plan.total = totals[name]
        if name in proc:
            if update_totals:
                plan.processed = proc[name]
            else:
                plan.processed = max(plan.processed, proc[name])
            if plan.total:
                plan.processed = min(plan.processed, plan.total)
    fetch = phase_map["fetching"]
    if fetch_outcomes:
        fetch.success = fetch_outcomes.get("success", 0)
        fetch.failure = fetch_outcomes.get("failure", 0)
        fetch.processed = fetch.success + fetch.failure
    else:
        fetch.success = 0
        fetch.failure = 0
    _normalize_phases(state)
    meta = phase_map["metadata"]
    state.total = meta.total
    state.processed = meta.processed


def begin_ingest(source: str, corpus: int) -> None:
    """Pin shared corpus totals before slow connector work (Firefox ingest pattern)."""
    from pka.constants import Source

    if corpus > 0:
        set_corpus_total(source, corpus)
    if source != Source.FIREFOX:
        skip_phase(source, "fetching")


def begin_job(source: str, job: JobKind, phase: str = "loading") -> None:
    with _lock:
        state = _states.get(source)
        if not state:
            state = SyncState(source=source)
            _states[source] = state
        _ensure_standard_phases(state)
        state.status = "running"
        state.active_job = job
        state.phase = phase
        state.error = None
        state.failed = 0
        state.stop_requested = None
        state.started_at = time.time()
        state.finished_at = None


def begin_metadata_sync(source: str, pending: int, baseline: int) -> None:
    """Start metadata import progress using live archive counts vs source corpus size."""
    corpus = baseline + pending
    with _lock:
        state = _states.get(source)
        if not state:
            state = SyncState(source=source, status="running", active_job="metadata")
            _states[source] = state
        _ensure_standard_phases(state)
        state.metadata_baseline = baseline
        state.metadata_pending = pending
        state.metadata_sync_active = True
        meta = _phase_map(state)["metadata"]
        meta.total = corpus
        meta.processed = baseline
        state.phase_index = 0
        state.phase = "metadata"
        state.total = corpus
        state.processed = baseline
        state.status = "running"
        state.active_job = "metadata"


def set_corpus_total(source: str, total: int) -> None:
    """Set shared corpus size on all phases (authoritative job scope)."""
    with _lock:
        state = _states.get(source)
        if not state:
            state = SyncState(source=source, status="running")
            _states[source] = state
        _ensure_standard_phases(state)
        for plan in state.phases:
            plan.total = total
        _normalize_phases(state)


def begin(source: str, phase: str = "loading") -> None:
    begin_job(source, "metadata", phase=phase)


def plan_pipeline(source: str, phases: list[tuple[str, int]]) -> None:
    """Define pipeline phase totals (maps to standard metadata/fetching/embedding)."""
    alias = {"fulltext": "embedding", "ingesting": "embedding"}
    with _lock:
        state = _states.get(source)
        if not state:
            state = SyncState(source=source, status="running")
            _states[source] = state
        _ensure_standard_phases(state)
        phase_map = _phase_map(state)
        for raw_name, total in phases:
            name = alias.get(raw_name, raw_name)
            if name in phase_map:
                _apply_monotonic_total(phase_map[name], total)
                if state.status == "running" and state.phase == name:
                    state.total = phase_map[name].total
        state.phases = [phase_map[n] for n in STANDARD_PHASES]
        _normalize_phases(state)


def set_phase(source: str, phase: str, total: int) -> None:
    """Activate a sync phase without regressing cumulative progress."""
    alias = {"fulltext": "embedding", "ingesting": "embedding"}
    phase = alias.get(phase, phase)
    with _lock:
        state = _states.get(source)
        if not state:
            state = SyncState(source=source, status="running")
            _states[source] = state
        _ensure_standard_phases(state)
        phase_map = _phase_map(state)
        if phase not in phase_map:
            state.phases.append(PhasePlan(name=phase, total=total))
            _ensure_standard_phases(state)
            phase_map = _phase_map(state)

        state.phase_index = STANDARD_PHASES.index(phase) if phase in STANDARD_PHASES else 0
        plan = phase_map[phase]
        _apply_monotonic_total(plan, total)
        state.phase = phase
        state.total = plan.total
        state.processed = plan.processed
        state.status = "running"
        _normalize_phases(state)


def clear_embed_progress(source: str) -> None:
    """Stop tracking embedding progress for Firefox (interleaved fetch+embed)."""
    with _lock:
        state = _states.get(source)
        if not state:
            state = SyncState(source=source)
            _states[source] = state
        _ensure_standard_phases(state)
        _normalize_phases(state)


def skip_phase(source: str, phase: str) -> None:
    """Mark a pipeline phase complete without processing items."""
    alias = {"fulltext": "embedding", "ingesting": "embedding"}
    phase = alias.get(phase, phase)
    with _lock:
        state = _states.get(source)
        if not state:
            return
        for plan in state.phases:
            if plan.name == phase:
                if plan.total > 0:
                    _apply_monotonic_processed(plan, plan.total)
                break
        _normalize_phases(state)


def set_total(source: str, total: int, phase: str = "embedding") -> None:
    set_phase(source, phase, total)


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
            if plan.total > 0:
                plan.processed = min(plan.processed, plan.total)
            state.processed = plan.processed
            state.total = plan.total
        else:
            state.processed += 1
        if failed:
            state.failed += 1
        _normalize_phases(state)


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


def check_stop(source: str) -> StopReason | None:
    with _lock:
        state = _states.get(source)
        if state and state.status == "running":
            return state.stop_requested
    return None


def set_job_result(source: str, result: dict | None) -> None:
    with _lock:
        state = _states.get(source)
        if state:
            state.last_result = result


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


def snapshot(source: str | None = None) -> dict[str, dict]:
    from pka.constants import ALL_SOURCES

    # Serialize under the lock: _to_dict normalizes (mutates) shared state and
    # must not race advance()/set_phase() from worker threads.
    with _lock:
        if source:
            items = {source: _states.get(source, _idle(source))}
        else:
            items = {
                src: _states.get(src, _idle(src))
                for src in ALL_SOURCES
            }
        return {src: _to_dict(st) for src, st in items.items()}


def _to_dict(state: SyncState) -> dict:
    from pka.ingestion.pending_metadata import metadata_job_progress

    _ensure_standard_phases(state)
    metadata_job = (
        state.status == "running"
        and state.active_job == "metadata"
        and state.metadata_sync_active
    )
    if not metadata_job:
        _normalize_phases(state)

    if metadata_job:
        meta_processed, meta_total = metadata_job_progress(
            state.source, state.metadata_baseline, state.metadata_pending,
        )
        done, total = meta_processed, meta_total
    else:
        done, total = _overall(state)

    if state.status == "running" and total:
        percent = min(100, round(100 * done / total))
    elif state.status in ("done", "paused", "cancelled"):
        percent = round(100 * done / total) if total else 100
    else:
        percent = round(100 * done / total) if total else 0

    phase_details = []
    for name in STANDARD_PHASES:
        plan = _phase_map(state)[name]
        if name == "metadata" and metadata_job:
            processed, phase_total = meta_processed, meta_total
            phase_pct = (
                100 if phase_total <= 0 and processed <= 0
                else min(100, round(100 * processed / phase_total)) if phase_total else 0
            )
        else:
            processed, phase_total = plan.processed, plan.total
            phase_pct = _phase_percent(plan)
        detail: dict = {
            "name":      name,
            "total":     phase_total,
            "processed": processed,
            "percent":   phase_pct,
            "active":    state.status == "running" and state.phase == name,
        }
        if name == "fetching" and plan.total > 0:
            pending = max(0, plan.total - plan.success - plan.failure)
            detail["breakdown"] = {
                "success": plan.success,
                "failure": plan.failure,
                "pending": pending,
            }
        phase_details.append(detail)

    return {
        "source":           state.source,
        "status":           state.status,
        "phase":            state.phase,
        "active_job":       state.active_job,
        "total":            meta_total if metadata_job else state.total,
        "processed":        meta_processed if metadata_job else state.processed,
        "failed":           state.failed,
        "percent":          percent,
        "overall_total":    total,
        "overall_processed": done,
        "phase_index":      state.phase_index,
        "phase_count":      len(STANDARD_PHASES),
        "phases":           list(STANDARD_PHASES),
        "phase_details":    phase_details,
        "error":            state.error,
        "last_result":      state.last_result,
    }
