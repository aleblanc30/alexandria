"""Characterization tests for the ``snapshot()`` payload.

Full-dict equality is deliberate. These pin the exact JSON the ingestion panel
consumes — one entry per (source shape x phase x status) combination the UI can
actually reach — so the progress refactor (``docs/history/sync_refactor.md``) can rewrite the
internals without silently changing what the frontend sees.

When a change to the payload is intentional, update the expected dict here in
the same commit; when it is not, this is the test that catches it.
"""

import pytest

from pka.ingestion import progress as sp


def _idle():
    return sp.snapshot("zotero")["zotero"]


def _firefox_fetching():
    sp.begin_job("firefox", "ingest")
    sp.begin_ingest("firefox", 10)
    sp.set_phase("firefox", "metadata", 10)
    for _ in range(10):
        sp.advance("firefox")
    sp.set_phase("firefox", "fetching", 10)
    sp.advance("firefox")
    sp.advance("firefox", failed=True)
    sp.advance("firefox")
    return sp.snapshot("firefox")["firefox"]


def _firefox_done():
    _firefox_fetching()
    sp.skip_phase("firefox", "fetching")
    sp.set_job_result("firefox", {"processed": 10})
    sp.finish("firefox")
    return sp.snapshot("firefox")["firefox"]


def _zotero_embedding():
    sp.begin_job("zotero", "ingest")
    sp.begin_ingest("zotero", 5)
    sp.set_phase("zotero", "metadata", 5)
    for _ in range(5):
        sp.advance("zotero")
    sp.set_phase("zotero", "embedding", 5)
    sp.advance("zotero")
    sp.advance("zotero")
    return sp.snapshot("zotero")["zotero"]


def _zotero_cancelled():
    _zotero_embedding()
    sp.request_cancel("zotero")
    sp.finish("zotero", stopped="cancel")
    return sp.snapshot("zotero")["zotero"]


def _zotero_paused():
    _zotero_embedding()
    sp.request_pause("zotero")
    sp.finish("zotero", stopped="pause")
    return sp.snapshot("zotero")["zotero"]


def _zotero_error():
    _zotero_embedding()
    sp.finish("zotero", error="boom")
    return sp.snapshot("zotero")["zotero"]


def _metadata_job():
    sp.begin_metadata_sync("firefox", pending=3, baseline=1)
    sp.advance("firefox")
    return sp.snapshot("firefox")["firefox"]


def _hydrated_idle():
    sp.hydrate(
        "calibre",
        {"metadata": 8, "fetching": 8, "embedding": 8},
        {"metadata": 8, "fetching": 8, "embedding": 6},
        None,
    )
    return sp.snapshot("calibre")["calibre"]


def _hydrated_firefox_outcomes():
    sp.hydrate(
        "firefox",
        {"metadata": 6, "fetching": 6, "embedding": 6},
        {"metadata": 6, "fetching": 5, "embedding": 4},
        {"success": 4, "failure": 1},
    )
    return sp.snapshot("firefox")["firefox"]


SCENARIOS = {
    "idle": _idle,
    "firefox_fetching": _firefox_fetching,
    "firefox_done": _firefox_done,
    "zotero_embedding": _zotero_embedding,
    "zotero_cancelled": _zotero_cancelled,
    "zotero_paused": _zotero_paused,
    "zotero_error": _zotero_error,
    "metadata_job": _metadata_job,
    "hydrated_idle": _hydrated_idle,
    "hydrated_firefox_outcomes": _hydrated_firefox_outcomes,
}

EXPECTED: dict[str, dict] = {
    "firefox_done": {
        "source": "firefox",
        "status": "done",
        "phase": "fetching",
        "active_job": None,
        "total": 10,
        "processed": 3,
        "failed": 1,
        "percent": 100,
        "overall_total": 20,
        "overall_processed": 20,
        "phase_index": 1,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 10, "processed": 10, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 10,
                "processed": 10,
                "percent": 100,
                "active": False,
                "breakdown": {"success": 2, "failure": 1, "pending": 7},
            },
            {"name": "embedding", "total": 0, "processed": 0, "percent": 0, "active": False},
        ],
        "error": None,
        "last_result": {"processed": 10},
    },
    "firefox_fetching": {
        "source": "firefox",
        "status": "running",
        "phase": "fetching",
        "active_job": "ingest",
        "total": 10,
        "processed": 3,
        "failed": 1,
        "percent": 65,
        "overall_total": 20,
        "overall_processed": 13,
        "phase_index": 1,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 10, "processed": 10, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 10,
                "processed": 3,
                "percent": 30,
                "active": True,
                "breakdown": {"success": 2, "failure": 1, "pending": 7},
            },
            {"name": "embedding", "total": 0, "processed": 0, "percent": 0, "active": False},
        ],
        "error": None,
        "last_result": None,
    },
    "hydrated_firefox_outcomes": {
        "source": "firefox",
        "status": "idle",
        "phase": "",
        "active_job": None,
        "total": 6,
        "processed": 6,
        "failed": 0,
        "percent": 92,
        "overall_total": 12,
        "overall_processed": 11,
        "phase_index": 0,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 6, "processed": 6, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 6,
                "processed": 5,
                "percent": 83,
                "active": False,
                "breakdown": {"success": 4, "failure": 1, "pending": 1},
            },
            {"name": "embedding", "total": 0, "processed": 0, "percent": 0, "active": False},
        ],
        "error": None,
        "last_result": None,
    },
    "hydrated_idle": {
        "source": "calibre",
        "status": "idle",
        "phase": "",
        "active_job": None,
        "total": 8,
        "processed": 8,
        "failed": 0,
        "percent": 92,
        "overall_total": 24,
        "overall_processed": 22,
        "phase_index": 0,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 8, "processed": 8, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 8,
                "processed": 8,
                "percent": 100,
                "active": False,
                "breakdown": {"success": 0, "failure": 0, "pending": 8},
            },
            {"name": "embedding", "total": 8, "processed": 6, "percent": 75, "active": False},
        ],
        "error": None,
        "last_result": None,
    },
    "idle": {
        "source": "zotero",
        "status": "idle",
        "phase": "",
        "active_job": None,
        "total": 0,
        "processed": 0,
        "failed": 0,
        "percent": 0,
        "overall_total": 0,
        "overall_processed": 0,
        "phase_index": 0,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 0, "processed": 0, "percent": 0, "active": False},
            {"name": "fetching", "total": 0, "processed": 0, "percent": 0, "active": False},
            {"name": "embedding", "total": 0, "processed": 0, "percent": 0, "active": False},
        ],
        "error": None,
        "last_result": None,
    },
    "metadata_job": {
        "source": "firefox",
        "status": "running",
        "phase": "metadata",
        "active_job": "metadata",
        "total": 4,
        "processed": 2,
        "failed": 0,
        "percent": 50,
        "overall_total": 4,
        "overall_processed": 2,
        "phase_index": 0,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 4, "processed": 2, "percent": 50, "active": True},
            {"name": "fetching", "total": 0, "processed": 0, "percent": 0, "active": False},
            {"name": "embedding", "total": 0, "processed": 0, "percent": 0, "active": False},
        ],
        "error": None,
        "last_result": None,
    },
    "zotero_cancelled": {
        "source": "zotero",
        "status": "cancelled",
        "phase": "embedding",
        "active_job": None,
        "total": 5,
        "processed": 2,
        "failed": 0,
        "percent": 60,
        "overall_total": 15,
        "overall_processed": 9,
        "phase_index": 2,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 5, "processed": 5, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 5,
                "processed": 2,
                "percent": 40,
                "active": False,
                "breakdown": {"success": 0, "failure": 0, "pending": 5},
            },
            {"name": "embedding", "total": 5, "processed": 2, "percent": 40, "active": False},
        ],
        "error": None,
        "last_result": None,
    },
    "zotero_embedding": {
        "source": "zotero",
        "status": "running",
        "phase": "embedding",
        "active_job": "ingest",
        "total": 5,
        "processed": 2,
        "failed": 0,
        "percent": 60,
        "overall_total": 15,
        "overall_processed": 9,
        "phase_index": 2,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 5, "processed": 5, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 5,
                "processed": 2,
                "percent": 40,
                "active": False,
                "breakdown": {"success": 0, "failure": 0, "pending": 5},
            },
            {"name": "embedding", "total": 5, "processed": 2, "percent": 40, "active": True},
        ],
        "error": None,
        "last_result": None,
    },
    "zotero_error": {
        "source": "zotero",
        "status": "error",
        "phase": "embedding",
        "active_job": None,
        "total": 5,
        "processed": 2,
        "failed": 0,
        "percent": 60,
        "overall_total": 15,
        "overall_processed": 9,
        "phase_index": 2,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 5, "processed": 5, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 5,
                "processed": 2,
                "percent": 40,
                "active": False,
                "breakdown": {"success": 0, "failure": 0, "pending": 5},
            },
            {"name": "embedding", "total": 5, "processed": 2, "percent": 40, "active": False},
        ],
        "error": "boom",
        "last_result": None,
    },
    "zotero_paused": {
        "source": "zotero",
        "status": "paused",
        "phase": "embedding",
        "active_job": None,
        "total": 5,
        "processed": 2,
        "failed": 0,
        "percent": 60,
        "overall_total": 15,
        "overall_processed": 9,
        "phase_index": 2,
        "phase_count": 3,
        "phases": ["metadata", "fetching", "embedding"],
        "phase_details": [
            {"name": "metadata", "total": 5, "processed": 5, "percent": 100, "active": False},
            {
                "name": "fetching",
                "total": 5,
                "processed": 2,
                "percent": 40,
                "active": False,
                "breakdown": {"success": 0, "failure": 0, "pending": 5},
            },
            {"name": "embedding", "total": 5, "processed": 2, "percent": 40, "active": False},
        ],
        "error": None,
        "last_result": None,
    },
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_snapshot_payload(name):
    assert SCENARIOS[name]() == EXPECTED[name]
