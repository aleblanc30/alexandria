"""Tests for sync progress tracking.

State isolation: the autouse ``isolated_settings`` fixture in conftest resets
``progress`` state around every test.
"""
from pka.ingestion import progress as sp


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
    assert snap["overall_total"] == 20  # metadata + fetching only (no embed phase)
    assert snap["percent"] == 25


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
    sp.plan_pipeline("image", [("embedding", 7)])
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
    assert totals == [100, 100, 0]
    assert processed == [100, 60, 0]


def test_fetching_total_matches_metadata_after_set_phase():
    sp.begin("firefox")
    sp.set_phase("firefox", "metadata", 500)
    sp.set_phase("firefox", "fetching", 600)
    snap = sp.snapshot("firefox")["firefox"]
    assert snap["phase_details"][0]["total"] == snap["phase_details"][1]["total"] == 600


def test_should_stop_tracks_pause_request():
    from pka.ingestion.progress import should_stop

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


def test_untracked_embed_phase_stays_empty_as_fetching_advances():
    """Firefox embeds inside its fetch pass, so its embedding bar has no work."""
    sp.begin("firefox")
    sp.set_phase("firefox", "fetching", 10)
    sp.advance("firefox", phase="fetching")
    fetch = sp.snapshot("firefox")["firefox"]["phase_details"][1]
    embed = sp.snapshot("firefox")["firefox"]["phase_details"][2]
    assert fetch["processed"] == 1
    assert embed["total"] == 0
    assert embed["processed"] == 0


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


def test_metadata_job_advances_from_ticks():
    """The importing worker owns metadata progress — no DB read on the poll."""
    sp.begin_metadata_sync("firefox", pending=3, baseline=1)
    sp.advance("firefox")
    sp.advance("firefox")
    meta = sp.snapshot("firefox")["firefox"]["phase_details"][0]
    assert meta["processed"] == 3
    assert meta["total"] == 4


def test_metadata_job_total_grows_past_an_underestimated_probe():
    sp.begin_metadata_sync("zotero", pending=2, baseline=0)
    for _ in range(5):
        sp.advance("zotero")
    meta = sp.snapshot("zotero")["zotero"]["phase_details"][0]
    assert (meta["processed"], meta["total"]) == (5, 5)


def test_advance_does_not_inflate_phase_total():
    sp.begin_job("zotero", "ingest")
    sp.set_corpus_total("zotero", 100)
    sp.set_phase("zotero", "embedding", 100)
    for _ in range(150):
        sp.advance("zotero")
    embed = sp.snapshot("zotero")["zotero"]["phase_details"][2]
    assert embed["total"] == 100
    assert embed["processed"] == 100


def test_begin_ingest_pins_corpus_before_handler():
    sp.begin_job("zotero", "ingest")
    sp.begin_ingest("zotero", 500)
    snap = sp.snapshot("zotero")["zotero"]
    assert all(p["total"] == 500 for p in snap["phase_details"])
    assert snap["phase_details"][1]["processed"] == 500


def test_hydrate_is_ignored_while_a_job_runs():
    """DB counts seed the idle display; a running job owns its own numbers."""
    sp.hydrate(
        "zotero",
        {"metadata": 500, "fetching": 500, "embedding": 500},
        {"metadata": 500, "fetching": 500, "embedding": 200},
    )
    sp.begin_job("zotero", "ingest")
    sp.set_corpus_total("zotero", 500)
    sp.skip_phase("zotero", "fetching")
    sp.set_phase("zotero", "embedding", 500)
    sp.advance("zotero")
    sp.hydrate(
        "zotero",
        {"metadata": 1000, "fetching": 1000, "embedding": 1000},
        {"metadata": 500, "fetching": 500, "embedding": 250},
    )
    snap = sp.snapshot("zotero")["zotero"]
    assert all(p["total"] == 500 for p in snap["phase_details"])
    # 200 already embedded when the job started, plus the one item it ticked —
    # not the 250 the late hydrate tried to impose.
    assert snap["phase_details"][2]["processed"] == 201


def test_embed_finish_preserves_job_corpus_over_doc_count(tmp_path, monkeypatch):
    from pka.db.queries import get_engine, init_db, insert_document_if_new
    from pka.ingestion.progress.baselines import seed_progress_from_db

    monkeypatch.setenv("ALEXANDRIA_DATA_DIR", str(tmp_path))
    init_db()
    for i in range(10):
        insert_document_if_new(
            "zotero", f"z{i}", f"Title {i}", f"http://{i}", None,
        )
    monkeypatch.setattr(
        "pka.ingestion.progress.baselines.source_corpus_size",
        lambda _src: 10,
    )
    sp.begin_job("zotero", "ingest")
    sp.begin_ingest("zotero", 5)
    sp.set_phase("zotero", "embedding", 5)
    for _ in range(5):
        sp.advance("zotero")
    sp.finish("zotero")
    seed_progress_from_db(get_engine(), "zotero")
    embed = sp.snapshot("zotero")["zotero"]["phase_details"][2]
    assert embed["total"] == 5


def test_metadata_finish_hydrate_does_not_double_totals():
    from pka.db.queries import get_engine, init_db, insert_document_if_new
    from pka.ingestion.progress.baselines import seed_progress_from_db

    init_db()
    for i in range(5):
        insert_document_if_new(
            "zotero", f"z{i}", f"Title {i}", f"http://{i}", None,
        )
    sp.begin_metadata_sync("zotero", pending=5, baseline=0)
    for _ in range(5):
        sp.advance("zotero")
    snap_running = sp.snapshot("zotero")["zotero"]
    assert snap_running["phase_details"][0]["total"] == 5
    assert snap_running["phase_details"][0]["processed"] == 5

    sp.finish("zotero")
    seed_progress_from_db(get_engine(), "zotero")
    meta = sp.snapshot("zotero")["zotero"]["phase_details"][0]
    assert meta["total"] == 5
    assert meta["processed"] == 5


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


def test_snapshot_concurrent_with_advance_is_safe():
    """snapshot() copies under the lock and serializes outside it — no races."""
    import threading

    sp.begin_job("zotero", "ingest")
    sp.set_corpus_total("zotero", 100_000)
    stop = threading.Event()
    errors: list[Exception] = []

    def hammer():
        try:
            while not stop.is_set():
                sp.advance("zotero", phase="fetching")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t = threading.Thread(target=hammer)
    t.start()
    try:
        for _ in range(300):
            snap = sp.snapshot("zotero")["zotero"]
            assert snap["source"] == "zotero"
            details = {d["name"]: d for d in snap["phase_details"]}
            assert details["fetching"]["processed"] <= details["fetching"]["total"]
    finally:
        stop.set()
        t.join(timeout=2.0)
    assert not errors
    sp.finish("zotero")


def test_snapshot_opens_no_db_connection(monkeypatch):
    """The serializer is pure: a poll must not queue behind the archive DB."""
    import pka.db.queries as queries

    sp.begin_metadata_sync("zotero", pending=4, baseline=1)
    sp.advance("zotero")

    def fail():  # pragma: no cover - only runs on regression
        raise AssertionError("snapshot() opened a database connection")

    monkeypatch.setattr(queries, "get_engine", fail)
    snap = sp.snapshot("zotero")["zotero"]
    assert snap["phase_details"][0]["processed"] == 2


def test_snapshot_does_not_mutate_shared_state():
    """Reads are reads: serializing must not normalize the live state."""
    sp.begin_job("zotero", "ingest")
    sp.set_corpus_total("zotero", 10)
    before = sp.snapshot("zotero")["zotero"]
    assert sp.snapshot("zotero")["zotero"] == before


def test_metadata_progress_advances_without_a_poll():
    """A closed browser tab must not stall a running metadata import."""
    from pka.ingestion.loops import run_metadata_loop

    sp.begin_metadata_sync("zotero", pending=3, baseline=0)
    run_metadata_loop(
        ["a", "b", "c"],
        known={},
        get_source_id=lambda item: item,
        persist=lambda item: "processed",
        progress_key="zotero",
    )
    meta = sp.snapshot("zotero")["zotero"]["phase_details"][0]
    assert (meta["processed"], meta["total"], meta["percent"]) == (3, 3, 100)


def test_metadata_loop_does_not_tick_for_items_already_stored():
    from pka.ingestion.loops import run_metadata_loop

    sp.begin_metadata_sync("zotero", pending=1, baseline=2)
    run_stats = run_metadata_loop(
        ["known1", "known2", "new"],
        known={"known1": 1, "known2": 2},
        get_source_id=lambda item: item,
        persist=lambda item: "processed",
        progress_key="zotero",
    )
    assert run_stats["skipped"] == 2
    meta = sp.snapshot("zotero")["zotero"]["phase_details"][0]
    assert (meta["processed"], meta["total"]) == (3, 3)
