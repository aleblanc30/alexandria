"""``/ingestion`` endpoint tests.

TestClient (a synchronous httpx wrapper), so no running server is needed. All
storage and embedding calls are mocked; the DB is real SQLite under tmp_path.
The ``client`` fixture comes from ``conftest.py``; row builders come from
``tests.api_seed``.
"""

import time

from tests.api_seed import seed_docs
from tests.conftest import make_document

# ── Ingestion ─────────────────────────────────────────────────────────────────


def _join_worker(ing_module, src: str, timeout: float = 5.0) -> None:
    """Wait for the background sync thread the router started for ``src``."""
    worker = ing_module._workers.get(src)
    if worker is not None:
        worker.join(timeout=timeout)


class TestIngestion:
    def setup_method(self):
        from pka.constants import ALL_SOURCES
        from pka.ingestion import progress as sp

        for src in ALL_SOURCES:
            sp.reset(src)

    def test_status_returns_totals(self, client):
        seed_docs(3)
        r = client.get("/ingestion/status")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert data["total"] >= 3

    def test_status_reports_unavailable_sources(self, client):
        r = client.get("/ingestion/status")
        assert r.status_code == 200
        data = r.json()
        unavailable = data.get("source_unavailable", {})
        assert "calibre" in unavailable
        assert "image" in unavailable
        assert unavailable["calibre"] is not None
        assert unavailable["image"] is not None
        assert "metadata.db" in unavailable["calibre"]
        assert "Image folder not found" in unavailable["image"]

    def test_unfetchable_returns_list(self, client):
        r = client.get("/ingestion/unfetchable")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_domains_returns_both_lists(self, client):
        from pka.constants import FetchStatus, Source

        make_document(
            Source.FIREFOX, "f1", "A", "https://a.com/1", 1, fetch_status=FetchStatus.FETCHED
        )
        make_document(
            Source.FIREFOX, "f2", "B", "https://b.com/1", 1, fetch_status=FetchStatus.UNFETCHABLE
        )

        r = client.get("/ingestion/domains")
        assert r.status_code == 200
        data = r.json()
        assert {"top_domains", "top_unfetchable"} == set(data.keys())
        assert any(row["domain"] == "b.com" for row in data["top_unfetchable"])

    def test_domains_rejects_bad_limit(self, client):
        assert client.get("/ingestion/domains?limit=0").status_code == 400
        assert client.get("/ingestion/domains?limit=101").status_code == 400

    def test_domains_rejects_unknown_source(self, client):
        r = client.get("/ingestion/domains?source=nope")
        assert r.status_code == 400

    def test_sync_valid_source_queued(self, client, monkeypatch):
        from pka.ingestion import progress as sp

        def fake_sync(src: str, backfill: bool = False) -> None:
            sp.finish(src)

        sp.reset("zotero")
        monkeypatch.setattr("pka.api.routers.ingestion._sync", fake_sync, raising=False)
        r = client.post("/ingestion/sync/zotero")
        assert r.status_code == 202

    def test_sync_invalid_source_400(self, client):
        r = client.post("/ingestion/sync/nonexistent")
        assert r.status_code == 400

    def test_pause_sync_requires_active_job(self, client):
        from pka.ingestion import progress as sp

        sp.reset("zotero")
        r = client.post("/ingestion/sync/zotero/pause")
        assert r.status_code == 409

    def test_cancel_sync_requires_active_job(self, client):
        from pka.ingestion import progress as sp

        sp.reset("zotero")
        r = client.post("/ingestion/sync/zotero/cancel")
        assert r.status_code == 409

    def test_pause_sync_when_running(self, client):
        from pka.ingestion import progress as sp

        sp.reset("firefox")
        sp.begin("firefox", phase="fetching")
        sp.set_phase("firefox", "fetching", 10)
        r = client.post("/ingestion/sync/firefox/pause")
        assert r.status_code == 202
        assert r.json()["status"] == "pause_requested"
        assert sp.check_stop("firefox") == "pause"

    def test_cancel_sync_when_running(self, client):
        from pka.ingestion import progress as sp

        sp.reset("zotero")
        sp.begin("zotero", phase="embedding")
        sp.set_phase("zotero", "embedding", 5)
        r = client.post("/ingestion/sync/zotero/cancel")
        assert r.status_code == 202
        assert r.json()["status"] == "cancel_requested"
        assert sp.check_stop("zotero") == "cancel"

    def test_sync_progress_snapshot(self, client):
        from pka.ingestion import progress as sp

        sp.reset("firefox")
        sp.begin("firefox")
        sp.plan_pipeline("firefox", [("metadata", 2), ("fetching", 2)])
        sp.set_phase("firefox", "metadata", 2)
        sp.advance("firefox")
        r = client.get("/ingestion/sync/progress?source=firefox")
        assert r.status_code == 200
        data = r.json()["firefox"]
        assert data["overall_processed"] == 1
        assert data["overall_total"] == 4  # metadata + fetching (Firefox skips embed phase)
        assert len(data["phase_details"]) == 3
        assert data["phase_details"][0]["total"] == 2
        assert data["phase_details"][1]["total"] == 2
        assert data["phase_details"][2]["total"] == 0
        assert data["phase_details"][0]["processed"] == 1
        assert data["phase_details"][2]["processed"] == 0
        sp.reset("firefox")

    def test_sync_events_streams_a_frame_for_an_idle_source(self, client, monkeypatch):
        import json

        from pka.api.routers import ingestion as router

        # No grace window: an idle source yields its snapshot and the stream ends.
        monkeypatch.setattr(router, "_START_GRACE_SECONDS", 0.0)
        with client.stream("GET", "/ingestion/sync/events?source=zotero") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            frames = [
                json.loads(line[len("data: ") :])
                for line in r.iter_lines()
                if line.startswith("data: ")
            ]
        assert len(frames) == 1
        assert frames[0]["progress"]["source"] == "zotero"
        assert set(frames[0]["counts"]) == {"pending_metadata", "fetch", "unavailable"}

    def test_sync_events_ends_when_the_job_does(self, client, monkeypatch):
        import json

        from pka.api.routers import ingestion as router

        snapshots = [
            {"source": "zotero", "status": "running", "processed": 1},
            {"source": "zotero", "status": "running", "processed": 2},
            {"source": "zotero", "status": "done", "processed": 2},
        ]
        monkeypatch.setattr(router, "_EVENT_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(router, "_START_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(router, "display_snapshot", lambda _e, _s: snapshots.pop(0))
        monkeypatch.setattr(router, "source_counts", lambda _e, _s: {})
        with client.stream("GET", "/ingestion/sync/events?source=zotero") as r:
            frames = [
                json.loads(line[len("data: ") :])
                for line in r.iter_lines()
                if line.startswith("data: ")
            ]
        # Three snapshots, three distinct frames, and the stream closes itself.
        assert [f["progress"]["status"] for f in frames] == ["running", "running", "done"]
        assert snapshots == []

    def test_sync_events_unknown_source_400(self, client):
        r = client.get("/ingestion/sync/events?source=nope")
        assert r.status_code == 400

    def test_sync_metadata_queued(self, client, monkeypatch):
        from pka.ingestion import progress as sp

        def fake_meta(src: str, backfill: bool = False) -> None:
            sp.finish(src)

        sp.reset("zotero")
        monkeypatch.setattr("pka.api.routers.ingestion._sync_metadata", fake_meta, raising=False)
        r = client.post("/ingestion/sync/zotero/metadata")
        assert r.status_code == 202
        assert r.json()["job"] == "metadata"

    def test_sync_ingest_queued(self, client, monkeypatch):
        from pka.ingestion import progress as sp

        def fake_ingest(src: str, backfill: bool = False) -> None:
            sp.finish(src)

        sp.reset("firefox")
        monkeypatch.setattr("pka.api.routers.ingestion._sync_ingest", fake_ingest, raising=False)
        r = client.post("/ingestion/sync/firefox/ingest")
        assert r.status_code == 202
        assert r.json()["job"] == "ingest"

    def test_backfill_flag_reaches_the_reddit_handler(self, client, monkeypatch):
        """Reddit's sync is incremental, so a full re-walk must be asked for."""
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        seen: dict = {}

        class _Handlers:
            @staticmethod
            def sync_metadata(progress_key=None, **kwargs):
                seen.update(kwargs)
                return {"metadata": {}}

        sp.reset("reddit")
        monkeypatch.setattr(ing, "require_handlers", lambda src: _Handlers)
        r = client.post("/ingestion/sync/reddit/metadata?backfill=1")
        _join_worker(ing, "reddit")

        assert r.status_code == 202
        assert r.json()["backfill"] is True
        assert seen == {"backfill": True}

    def test_metadata_sync_without_backfill_passes_nothing(self, client, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        seen: dict = {"called": False}

        class _Handlers:
            @staticmethod
            def sync_metadata(progress_key=None, **kwargs):
                seen["called"] = True
                seen["kwargs"] = kwargs
                return {"metadata": {}}

        sp.reset("reddit")
        monkeypatch.setattr(ing, "require_handlers", lambda src: _Handlers)
        client.post("/ingestion/sync/reddit/metadata")
        _join_worker(ing, "reddit")

        assert seen["called"] is True
        assert seen["kwargs"] == {}

    def test_backfill_rejected_for_sources_without_one(self, client):
        """Every other connector reads its whole source each run; 400, not a no-op."""
        r = client.post("/ingestion/sync/zotero/metadata?backfill=1")
        assert r.status_code == 400
        assert "backfill" in r.json()["detail"]

    def test_sync_metadata_routes_zotero(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        called = []
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.sync_zotero_metadata",
            lambda progress_key=None: called.append(progress_key) or {"metadata": {}},
        )
        sp.reset("zotero")
        ing._sync_metadata("zotero")
        assert called == ["zotero"]
        assert sp.snapshot("zotero")["zotero"]["status"] == "done"
        assert sp.snapshot("zotero")["zotero"]["active_job"] is None

    def test_sync_ingest_routes_firefox(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        called = []
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.sync_firefox_ingest",
            lambda progress_key=None, **kw: called.append(progress_key) or {"embed": {}},
        )
        sp.reset("firefox")
        ing._sync_ingest("firefox")
        assert called == ["firefox"]
        snap = sp.snapshot("firefox")["firefox"]
        assert snap["status"] == "done"
        assert snap["active_job"] is None

    def _stub_ingest(self, monkeypatch, result: dict):
        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.sync_firefox_ingest",
            lambda progress_key=None, **kw: result,
        )

    def _spy_assignment(self, monkeypatch, *, active_run: int | None = 1, boom: bool = False):
        """Record assign_new_docs calls; returns the (mutable) call list."""
        calls: list[int] = []

        def fake_assign(run_id=None):
            calls.append(run_id)
            if boom:
                raise RuntimeError("chroma exploded")
            return {"assigned": 3}

        monkeypatch.setattr("pka.clustering.lifecycle.assign_new_docs", fake_assign)
        monkeypatch.setattr(
            "pka.clustering.lifecycle.get_active_run_id",
            lambda: active_run,
        )
        return calls

    def test_ingest_assigns_new_docs_to_active_run(self, monkeypatch):
        """New documents are filed into the accepted run as soon as they land."""
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        self._stub_ingest(monkeypatch, {"embed": {}})
        calls = self._spy_assignment(monkeypatch)

        sp.reset("firefox")
        ing._sync_ingest("firefox")

        assert len(calls) == 1
        assert sp.snapshot("firefox")["firefox"]["status"] == "done"

    def test_metadata_sync_does_not_assign(self, monkeypatch):
        """Metadata writes no chunks, so there is nothing to assign."""
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.sync_firefox_metadata",
            lambda progress_key=None, **kw: {},
        )
        calls = self._spy_assignment(monkeypatch)

        sp.reset("firefox")
        ing._sync_metadata("firefox")

        assert calls == []

    def test_cancelled_ingest_does_not_assign(self, monkeypatch):
        """A stopped job leaves the archive mid-update; assignment waits."""
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        self._stub_ingest(monkeypatch, {"embed": {"stopped": "cancelled"}})
        calls = self._spy_assignment(monkeypatch)

        sp.reset("firefox")
        ing._sync_ingest("firefox")

        assert calls == []

    def test_no_active_run_skips_assignment(self, monkeypatch):
        """Nothing to assign into, and an ingest must never start a full run."""
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        self._stub_ingest(monkeypatch, {"embed": {}})
        calls = self._spy_assignment(monkeypatch, active_run=None)

        sp.reset("firefox")
        ing._sync_ingest("firefox")

        assert calls == []

    def test_assignment_failure_leaves_sync_successful(self, monkeypatch):
        """Clustering is a view over the archive: its failure is not the sync's."""
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        self._stub_ingest(monkeypatch, {"embed": {}})
        self._spy_assignment(monkeypatch, boom=True)

        sp.reset("firefox")
        ing._sync_ingest("firefox")

        snap = sp.snapshot("firefox")["firefox"]
        assert snap["status"] == "done"
        assert snap.get("error") is None

    def test_sync_progress_unknown_source_400(self, client):
        r = client.get("/ingestion/sync/progress?source=invalid")
        assert r.status_code == 400

    def test_force_cancels_running_job_before_restart(self, client, monkeypatch):
        """force=true must stop the running worker, not run two jobs concurrently."""
        import threading as th

        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        started: list[th.Event] = []

        def fake_job(src: str, backfill: bool = False) -> None:
            ev = th.Event()
            started.append(ev)
            sp.begin_job(src, "metadata")
            ev.set()
            while not sp.check_stop(src):
                time.sleep(0.005)
            sp.finish(src, stopped="cancel")

        monkeypatch.setitem(ing._JOB_TARGETS, "metadata", fake_job)
        sp.reset("zotero")

        r1 = client.post("/ingestion/sync/zotero/metadata")
        assert r1.status_code == 202
        assert started[0].wait(timeout=2.0)

        # Same job again without force → conflict, still exactly one worker.
        assert client.post("/ingestion/sync/zotero/metadata").status_code == 409
        assert len(started) == 1

        # force=true cancels the first worker and only then starts a second.
        r2 = client.post("/ingestion/sync/zotero/metadata?force=true")
        assert r2.status_code == 202
        deadline = time.time() + 2.0
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.005)
        assert len(started) == 2
        assert started[1].wait(timeout=2.0)

        # Clean up: stop the second worker too.
        sp.request_cancel("zotero")
        deadline = time.time() + 2.0
        while sp.is_running("zotero") and time.time() < deadline:
            time.sleep(0.005)
        assert not sp.is_running("zotero")

    def test_rebuild_vectors_queued(self, client, monkeypatch):
        from pka.api.routers import ingestion as ing

        ing._rebuild_running = False
        monkeypatch.setattr(
            "pka.storage.vector_store.rebuild_from_chunks",
            lambda **kw: {"chunks": 0, "processed": 0},
        )
        r = client.post("/ingestion/rebuild-vectors")
        assert r.status_code == 202
        assert r.json()["status"] == "queued"
        ing._rebuild_running = False

    def test_rebuild_vectors_409_when_busy(self, client):
        from pka.api.routers import ingestion as ing

        ing._rebuild_running = True
        try:
            r = client.post("/ingestion/rebuild-vectors")
            assert r.status_code == 409
        finally:
            ing._rebuild_running = False

    def test_sync_routes_zotero_via_sync_fn(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        called = []
        monkeypatch.setattr(
            "pka.ingestion.zotero_sync.sync_zotero",
            lambda progress_key=None: called.append(progress_key) or {"processed": 1},
        )
        sp.reset("zotero")
        ing._sync("zotero")
        assert called == ["zotero"]
        assert sp.snapshot("zotero")["zotero"]["status"] == "done"

    def test_sync_records_error_on_failure(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        def boom(**kw):
            raise RuntimeError("sync blew up")

        monkeypatch.setattr("pka.ingestion.zotero_sync.sync_zotero", boom)
        sp.reset("zotero")
        ing._sync("zotero")
        snap = sp.snapshot("zotero")["zotero"]
        assert snap["status"] == "error"
        assert "sync blew up" in snap["error"]

    def test_sync_firefox_source(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        monkeypatch.setattr(
            "pka.ingestion.firefox_sync.sync_firefox",
            lambda progress_key=None, **kw: {"metadata": {}, "stopped": "pause"},
        )
        sp.reset("firefox")
        ing._sync("firefox")
        assert sp.snapshot("firefox")["firefox"]["status"] == "paused"

    def test_sync_calibre_source(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        monkeypatch.setattr(
            "pka.ingestion.calibre_sync.sync_calibre",
            lambda progress_key=None, **kw: {"metadata": {}, "fulltext": {}},
        )
        sp.reset("calibre")
        ing._sync("calibre")
        assert sp.snapshot("calibre")["calibre"]["status"] == "done"

    def test_sync_image_source(self, monkeypatch):
        from pka.api.routers import ingestion as ing
        from pka.ingestion import progress as sp

        monkeypatch.setattr(
            "pka.ingestion.image_sync.sync_images",
            lambda progress_key=None, **kw: {"processed": 2, "stopped": "cancel"},
        )
        sp.reset("image")
        ing._sync("image")
        assert sp.snapshot("image")["image"]["status"] == "cancelled"
