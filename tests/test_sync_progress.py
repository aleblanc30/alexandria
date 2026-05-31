"""Tests for sync progress tracking."""
from pka.ingestion import sync_progress as sp


def setup_function():
    for src in ("firefox", "zotero", "calibre", "image"):
        sp.reset(src)


def test_pipeline_overall_percent():
    sp.begin("firefox", phase="starting")
    sp.plan_pipeline("firefox", [
        ("metadata", 10),
        ("fetching", 10),
        ("embedding", 10),
    ])
    sp.set_phase("firefox", "metadata", 10)
    for _ in range(5):
        sp.advance("firefox")
    snap = sp.snapshot("firefox")["firefox"]
    assert snap["overall_processed"] == 5
    assert snap["overall_total"] == 30
    assert snap["percent"] == 17


def test_skip_phase_marks_complete():
    sp.begin("firefox")
    sp.plan_pipeline("firefox", [
        ("metadata", 4),
        ("fetching", 4),
        ("embedding", 4),
    ])
    sp.set_phase("firefox", "metadata", 4)
    for _ in range(4):
        sp.advance("firefox")
    sp.skip_phase("firefox", "fetching")
    sp.skip_phase("firefox", "embedding")
    sp.finish("firefox")
    snap = sp.snapshot("firefox")["firefox"]
    assert snap["status"] == "done"
    assert snap["percent"] == 100


def test_cancel_and_pause():
    sp.begin("zotero")
    sp.plan_pipeline("zotero", [("embedding", 5)])
    sp.set_phase("zotero", "embedding", 5)
    assert sp.request_cancel("zotero")
    assert sp.check_stop("zotero") == "cancel"
    sp.finish("zotero", stopped="cancel")
    assert sp.snapshot("zotero")["zotero"]["status"] == "cancelled"

    sp.reset("zotero")
    sp.begin("zotero")
    sp.plan_pipeline("zotero", [("embedding", 5)])
    sp.set_phase("zotero", "embedding", 5)
    assert sp.request_pause("zotero")
    sp.finish("zotero", stopped="pause")
    assert sp.snapshot("zotero")["zotero"]["status"] == "paused"


def test_request_cancel_fails_when_idle():
    assert not sp.request_cancel("firefox")


def test_set_total_sets_phase():
    sp.begin("calibre")
    sp.set_total("calibre", 12, phase="metadata")
    snap = sp.snapshot("calibre")["calibre"]
    assert snap["phase"] == "metadata"
    assert snap["total"] == 12


def test_finish_with_error():
    sp.begin("zotero")
    sp.set_phase("zotero", "embedding", 5)
    sp.finish("zotero", error="boom")
    snap = sp.snapshot("zotero")["zotero"]
    assert snap["status"] == "error"
    assert snap["error"] == "boom"


def test_plan_pipeline_creates_new_state():
    sp.plan_pipeline("image", [("ingesting", 7)])
    snap = sp.snapshot("image")["image"]
    assert snap["phases"] == list(sp.STANDARD_PHASES)
    assert snap["overall_total"] == 21
    assert all(p["total"] == 7 for p in snap["phase_details"])
    assert snap["phase_details"][2]["name"] == "embedding"


def test_shared_total_and_processed_order():
    sp.hydrate("firefox", {"metadata": 100, "fetching": 100, "embedding": 100},
               {"metadata": 100, "fetching": 60, "embedding": 40})
    snap = sp.snapshot("firefox")["firefox"]
    totals = [p["total"] for p in snap["phase_details"]]
    processed = [p["processed"] for p in snap["phase_details"]]
    assert totals == [100, 100, 100]
    assert processed[0] >= processed[1] >= processed[2]


def test_fetching_total_matches_metadata_after_set_phase():
    sp.begin("firefox")
    sp.set_phase("firefox", "metadata", 500)
    sp.set_phase("firefox", "fetching", 600)
    snap = sp.snapshot("firefox")["firefox"]
    assert snap["phase_details"][0]["total"] == snap["phase_details"][1]["total"] == 600


def test_sync_helpers_should_stop():
    from pka.ingestion.sync_helpers import should_stop

    assert should_stop(None) is None
    sp.begin("firefox")
    sp.set_phase("firefox", "fetching", 3)
    assert should_stop("firefox") is None
    sp.request_pause("firefox")
    assert should_stop("firefox") == "pause"


def test_set_phase_never_regresses_processed():
    sp.hydrate("zotero", {"metadata": 10, "fetching": 0, "embedding": 10},
               {"metadata": 10, "fetching": 0, "embedding": 7})
    sp.begin_job("zotero", "ingest")
    sp.set_phase("zotero", "embedding", 10)
    snap = sp.snapshot("zotero")["zotero"]
    assert snap["phase_details"][0]["processed"] == 10
    assert snap["phase_details"][2]["processed"] == 7


def test_plan_pipeline_never_shrinks_totals():
    sp.hydrate("firefox", {"metadata": 100, "fetching": 100, "embedding": 100},
               {"metadata": 100, "fetching": 60, "embedding": 40})
    sp.begin_job("firefox", "ingest")
    sp.plan_pipeline("firefox", [("metadata", 100), ("fetching", 5), ("embedding", 5)])
    sp.set_phase("firefox", "fetching", 5)
    snap = sp.snapshot("firefox")["firefox"]
    assert snap["phase_details"][0]["processed"] == 100
    assert snap["phase_details"][1]["processed"] == 60
    assert snap["phase_details"][1]["total"] == 100


def test_embedding_overflow_does_not_inflate_metadata():
    sp.hydrate("zotero", {"metadata": 10, "fetching": 10, "embedding": 10},
               {"metadata": 10, "fetching": 10, "embedding": 10})
    sp.begin_job("zotero", "ingest")
    sp.set_phase("zotero", "embedding", 10)
    for _ in range(3):
        sp.advance("zotero")
    snap = sp.snapshot("zotero")["zotero"]
    assert snap["phase_details"][0]["processed"] == 10
    assert snap["phase_details"][1]["processed"] == 10
    assert snap["phase_details"][2]["processed"] == 10


def test_fetch_breakdown_from_hydrate():
    sp.hydrate(
        "firefox",
        {"metadata": 100, "fetching": 100, "embedding": 100},
        {"metadata": 100, "fetching": 70, "embedding": 40},
        {"success": 60, "failure": 10},
    )
    fetch = sp.snapshot("firefox")["firefox"]["phase_details"][1]
    assert fetch["name"] == "fetching"
    assert fetch["breakdown"] == {"success": 60, "failure": 10, "pending": 30}


def test_fetch_breakdown_resets_stale_success_on_hydrate():
    sp.begin("firefox")
    sp.set_phase("firefox", "fetching", 100)
    for _ in range(80):
        sp.advance("firefox")
    sp.finish("firefox")
    assert sp.snapshot("firefox")["firefox"]["phase_details"][1]["breakdown"]["success"] == 80

    sp.hydrate(
        "firefox",
        {"metadata": 100, "fetching": 100, "embedding": 100},
        {"metadata": 100, "fetching": 0, "embedding": 0},
        {"success": 0, "failure": 0},
    )
    fetch = sp.snapshot("firefox")["firefox"]["phase_details"][1]
    assert fetch["breakdown"] == {"success": 0, "failure": 0, "pending": 100}


def test_fetch_breakdown_updates_on_advance():
    sp.begin("firefox")
    sp.set_phase("firefox", "fetching", 10)
    sp.advance("firefox")
    sp.advance("firefox", failed=True)
    sp.advance("firefox")
    fetch = sp.snapshot("firefox")["firefox"]["phase_details"][1]
    assert fetch["breakdown"] == {"success": 2, "failure": 1, "pending": 7}
    assert fetch["processed"] == 3


def test_metadata_job_progress_from_archive_db():
    from pka.db.queries import init_db, insert_document_if_new
    from pka.ingestion.pending_metadata import metadata_job_progress

    init_db()
    insert_document_if_new("firefox", "bm0", "T", "http://z", None)
    sp.begin_metadata_sync("firefox", pending=3, baseline=1)
    insert_document_if_new("firefox", "bm1", "T", "http://a", None)
    insert_document_if_new("firefox", "bm2", "T", "http://b", None)
    snap = sp.snapshot("firefox")["firefox"]
    meta = snap["phase_details"][0]
    assert meta["processed"] == 3
    assert meta["total"] == 4
    assert metadata_job_progress("firefox", 1, 3) == (3, 4)


def test_hydrate_shrinks_totals_after_documents_removed():
    sp.hydrate(
        "firefox",
        {"metadata": 10000, "fetching": 10000, "embedding": 10000},
        {"metadata": 10000, "fetching": 8000, "embedding": 5000},
        {"success": 7000, "failure": 1000},
    )
    snap = sp.snapshot("firefox")["firefox"]
    assert snap["phase_details"][0]["total"] == 10000

    sp.hydrate(
        "firefox",
        {"metadata": 0, "fetching": 0, "embedding": 0},
        {"metadata": 0, "fetching": 0, "embedding": 0},
        {"success": 0, "failure": 0},
    )
    snap = sp.snapshot("firefox")["firefox"]
    assert all(p["total"] == 0 for p in snap["phase_details"])
    assert all(p["processed"] == 0 for p in snap["phase_details"])
    assert "breakdown" not in snap["phase_details"][1]


def test_hydrate_before_job_uses_db_not_stale_memory():
    sp.begin("firefox")
    sp.set_phase("firefox", "metadata", 10000)
    for _ in range(10000):
        sp.advance("firefox")
    sp.finish("firefox")

    sp.hydrate(
        "firefox",
        {"metadata": 500, "fetching": 500, "embedding": 500},
        {"metadata": 500, "fetching": 0, "embedding": 0},
        {"success": 0, "failure": 0},
    )
    snap = sp.snapshot("firefox")["firefox"]
    assert snap["phase_details"][0]["total"] == 500
    assert snap["phase_details"][0]["processed"] == 500
