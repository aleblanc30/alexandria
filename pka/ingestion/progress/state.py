"""Progress state: the dataclasses and the pure functions that reshape them.

Stdlib (plus :mod:`pka.constants`) only — no locking, no DB, no I/O. Everything
here operates on a single :class:`SyncState` handed in by the caller, which is
what makes it safe to run against a snapshot copy as well as the live object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pka.ingestion.registry import phase_spec

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


def idle_state(source: str) -> SyncState:
    return SyncState(source=source, status="idle")


def phase_map(state: SyncState) -> dict[str, PhasePlan]:
    return {p.name: p for p in state.phases}


def ensure_standard_phases(state: SyncState) -> None:
    existing = phase_map(state)
    state.phases = [
        existing.get(name, PhasePlan(name=name))
        for name in STANDARD_PHASES
    ]


def overall(state: SyncState) -> tuple[int, int]:
    if state.phases:
        done = sum(p.processed for p in state.phases)
        total = sum(p.total for p in state.phases)
        return done, total
    return state.processed, state.total


def apply_monotonic_total(plan: PhasePlan, total: int) -> None:
    """Raise phase total to fit new work without shrinking below progress."""
    plan.total = max(plan.total, total)
    if plan.total > 0:
        plan.processed = min(plan.processed, plan.total)


def apply_monotonic_processed(plan: PhasePlan, processed: int) -> None:
    plan.processed = max(plan.processed, processed)
    if plan.total > 0:
        plan.processed = min(plan.processed, plan.total)


def normalize_phases(state: SyncState) -> None:
    """One shared corpus total; processed reflects sequential pipeline depth.

    Each item moves metadata → fetching → embedding, so every phase shares the
    same total (corpus size) while processed counts decrease downstream.
    Downstream progress must never inflate upstream phases.

    Firefox fetch+embed is interleaved; embedding progress is not tracked.

    A metadata-only job is scoped to its own phase: fetching and embedding have
    no plan yet, and handing them the corpus size would sink the bar to a third
    of the progress actually made.
    """
    ensure_standard_phases(state)
    if state.metadata_sync_active:
        meta = state.phases[0]
        # ``pending`` came from a source probe; finding more items than that
        # means a bigger corpus, not a bar pinned at 100%.
        meta.total = max(meta.total, meta.processed)
        return

    meta, fetch, embed = state.phases
    skip_embed = not phase_spec(state.source).tracks_embedding

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


def phase_percent(plan: PhasePlan) -> int:
    if plan.total <= 0:
        return 100 if plan.processed > 0 else 0
    return min(100, round(100 * plan.processed / plan.total))


def apply_db_counts(
    state: SyncState,
    totals: dict[str, int],
    processed: dict[str, int] | None,
    fetch_outcomes: dict[str, int] | None,
    *,
    update_totals: bool = True,
) -> None:
    ensure_standard_phases(state)
    proc = processed or {}
    plans = phase_map(state)
    skip_embed = not phase_spec(state.source).tracks_embedding
    for name in STANDARD_PHASES:
        if skip_embed and name == "embedding":
            continue
        plan = plans[name]
        if update_totals and name in totals:
            plan.total = totals[name]
        if name in proc:
            if update_totals:
                plan.processed = proc[name]
            else:
                plan.processed = max(plan.processed, proc[name])
            if plan.total:
                plan.processed = min(plan.processed, plan.total)
    fetch = plans["fetching"]
    if fetch_outcomes:
        fetch.success = fetch_outcomes.get("success", 0)
        fetch.failure = fetch_outcomes.get("failure", 0)
        fetch.processed = fetch.success + fetch.failure
    else:
        fetch.success = 0
        fetch.failure = 0
    normalize_phases(state)
    meta = plans["metadata"]
    state.total = meta.total
    state.processed = meta.processed
