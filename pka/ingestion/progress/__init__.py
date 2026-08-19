"""In-memory sync progress for ingestion background jobs.

Four layers, each depending only on the ones above it:

``state``     dataclasses + pure reshaping functions
``tracker``   the locked registry; every write goes through here
``view``      state -> the payload the API serves
``baselines`` DB counts that seed the display at job start and at idle

Import this package (``from pka.ingestion import progress as sp``) for the
public API; reach for a submodule only when you need something narrower.
"""
from __future__ import annotations

from pka.ingestion.progress.state import (
    STANDARD_PHASES,
    JobKind,
    PhasePlan,
    Status,
    StopReason,
    SyncState,
)
from pka.ingestion.progress.tracker import (
    advance,
    begin,
    begin_ingest,
    begin_job,
    begin_metadata_sync,
    check_stop,
    finish,
    hydrate,
    is_running,
    plan_pipeline,
    request_cancel,
    request_pause,
    reset,
    set_corpus_total,
    set_job_result,
    set_phase,
    set_total,
    should_stop,
    skip_phase,
    tick,
)
from pka.ingestion.progress.view import snapshot, to_dict

__all__ = [
    "STANDARD_PHASES",
    "JobKind",
    "PhasePlan",
    "Status",
    "StopReason",
    "SyncState",
    "advance",
    "begin",
    "begin_ingest",
    "begin_job",
    "begin_metadata_sync",
    "check_stop",
    "finish",
    "hydrate",
    "is_running",
    "plan_pipeline",
    "request_cancel",
    "request_pause",
    "reset",
    "set_corpus_total",
    "set_job_result",
    "set_phase",
    "set_total",
    "should_stop",
    "skip_phase",
    "tick",
    "snapshot",
    "to_dict",
]
