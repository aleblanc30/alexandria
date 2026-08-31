"""Serialization of sync state into the payload the frontend consumes.

Read-only with respect to the tracker: :func:`snapshot` works on detached
copies, so normalizing for display can never race — or leak into — the live
state a worker thread is writing.
"""

from __future__ import annotations

from pka.ingestion.progress.state import (
    STANDARD_PHASES,
    SyncState,
    normalize_phases,
    overall,
    phase_map,
    phase_percent,
)
from pka.ingestion.progress.tracker import snapshot_states


def snapshot(source: str | None = None) -> dict[str, dict]:
    return {src: to_dict(st) for src, st in snapshot_states(source).items()}


def to_dict(state: SyncState) -> dict:
    normalize_phases(state)
    done, total = overall(state)

    if state.status == "running" and total:
        percent = min(100, round(100 * done / total))
    elif state.status in ("done", "paused", "cancelled"):
        percent = round(100 * done / total) if total else 100
    else:
        percent = round(100 * done / total) if total else 0

    phase_details = []
    plans = phase_map(state)
    for name in STANDARD_PHASES:
        plan = plans[name]
        detail: dict = {
            "name": name,
            "total": plan.total,
            "processed": plan.processed,
            "percent": phase_percent(plan),
            "active": state.status == "running" and state.phase == name,
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
        "source": state.source,
        "status": state.status,
        "phase": state.phase,
        "active_job": state.active_job,
        "total": state.total,
        "processed": state.processed,
        "failed": state.failed,
        "percent": percent,
        "overall_total": total,
        "overall_processed": done,
        "phase_index": state.phase_index,
        "phase_count": len(STANDARD_PHASES),
        "phases": list(STANDARD_PHASES),
        "phase_details": phase_details,
        "error": state.error,
        "last_result": state.last_result,
    }
