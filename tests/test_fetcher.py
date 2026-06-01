import asyncio
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from pka.db.queries import init_db, upsert_document
from pka.constants import FetchStatus
from pka.ingestion.fetcher import (
    _fetch_one,
    bookmark_url_unfetchable_reason,
    fetch_pending,
    FetchResult,
    reset_unfetchable_for_fetch,
)


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


def _html_response(status: int = 200, body: str = "<html><body><p>Hello world, this is content.</p></body></html>", content_type: str = "text/html") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = body
    resp.content = body.encode("utf-8")
    resp.headers = {"content-type": content_type}
    return resp


def _pdf_response(
    status: int = 200,
    body: bytes | None = None,
    content_type: str = "application/pdf",
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if body is None:
        body = b"%PDF-1.4"
    resp.content = body
    resp.text = body.decode("latin-1", errors="replace")
    resp.headers = {"content-type": content_type}
    return resp


class TestBookmarkUrlUnfetchableReason:
    @pytest.mark.parametrize(
        "url,reason",
        [
            ("file:///C:/Users/foo.pdf", "local file url"),
            ("file:///home/user/doc.html", "local file url"),
            ("C:", "local file path"),
            ("C\\:", "local file path"),
            ("C:/Users/foo.pdf", "local file path"),
            ("\\\\server\\share\\doc.pdf", "local file path"),
            ("/home/user/doc.html", "local file path"),
            ("https://example.com/page", None),
            ("http://example.com/page", None),
        ],
    )
    def test_detects_non_http_urls(self, url, reason):
        assert bookmark_url_unfetchable_reason(url) == reason


class TestFetchOne:
    @pytest.mark.asyncio
    async def test_unfetchable_local_file_url_without_http(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        result = await _fetch_one(mock_client, doc_id=1, url="file:///C:/Users/foo.pdf")
        assert result.status == "unfetchable"
        assert result.error_msg == "local file url"
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_unfetchable_windows_drive_path(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        result = await _fetch_one(mock_client, doc_id=1, url="C\\:")
        assert result.status == "unfetchable"
        assert "local file path" in (result.error_msg or "")
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetched_status_on_200(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(200)
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/page")
        assert result.status == "fetched"
        assert result.http_status == 200

    @pytest.mark.asyncio
    async def test_text_extracted_on_success(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(200, "<html><body><p>" + "word " * 50 + "</p></body></html>")
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com")
        assert result.text is not None
        assert len(result.text) > 0

    @pytest.mark.asyncio
    async def test_unfetchable_on_404(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(404)
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/gone")
        assert result.status == "unfetchable"
        assert result.http_status == 404

    @pytest.mark.asyncio
    async def test_unfetchable_on_403(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(403)
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/auth")
        assert result.status == "unfetchable"

    @pytest.mark.asyncio
    async def test_unfetchable_on_timeout(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = httpx.TimeoutException("timed out")
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com")
        assert result.status == "unfetchable"
        assert result.http_status is None
        assert "timeout" in (result.error_msg or "").lower()

    @pytest.mark.asyncio
    async def test_unfetchable_when_overall_budget_exceeded(self, monkeypatch):
        monkeypatch.setattr("pka.ingestion.fetcher._fetch_budget_seconds", lambda **kw: 0.05)

        async def slow_fetch(client, doc_id, url):
            await asyncio.sleep(1)
            return FetchResult(doc_id, url, "fetched", "late", 200, None)

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one_impl", slow_fetch)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com")
        assert result.status == "unfetchable"
        assert result.error_msg == "timeout"

    @pytest.mark.asyncio
    async def test_fetched_for_pdf_url(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _pdf_response()
        monkeypatch.setattr(
            "pka.ingestion.fetcher._extract_text_from_pdf_bytes",
            lambda data, **kw: "Extracted PDF text with enough content to embed.",
        )
        result = await _fetch_one(mock_client, doc_id=1, url="https://arxiv.org/paper.pdf")
        assert result.status == "fetched"
        assert result.text is not None
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_unfetchable_when_pdf_extraction_empty(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _pdf_response()
        monkeypatch.setattr(
            "pka.ingestion.fetcher._extract_text_from_pdf_bytes",
            lambda data, **kw: None,
        )
        result = await _fetch_one(mock_client, doc_id=1, url="https://arxiv.org/paper.pdf")
        assert result.status == "unfetchable"
        assert "pdf extraction" in (result.error_msg or "").lower()

    @pytest.mark.asyncio
    async def test_fetched_for_pdf_content_type_without_extension(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _pdf_response()
        monkeypatch.setattr(
            "pka.ingestion.fetcher._extract_text_from_pdf_bytes",
            lambda data, **kw: "PDF body from content-type route.",
        )
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/download")
        assert result.status == "fetched"
        assert result.text is not None

    @pytest.mark.asyncio
    async def test_unfetchable_when_pdf_url_returns_html(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(200, "<html><body>Not a PDF</body></html>")
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/missing.pdf")
        assert result.status == "unfetchable"
        assert "not a pdf" in (result.error_msg or "").lower()

    @pytest.mark.asyncio
    async def test_skipped_for_non_html_content_type(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(200, content_type="application/json")
        result = await _fetch_one(mock_client, doc_id=1, url="https://api.example.com/data")
        assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_document_id_preserved_in_result(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(404)
        result = await _fetch_one(mock_client, doc_id=42, url="https://x.com")
        assert result.document_id == 42

    @pytest.mark.asyncio
    async def test_non_wikipedia_uses_direct_fetch(self):
        url = "https://example.com/page"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(200)

        result = await _fetch_one(mock_client, doc_id=1, url=url)

        assert result.status == "fetched"
        mock_client.get.assert_called_once_with(
            url,
            follow_redirects=True,
            timeout=mock_client.get.call_args.kwargs["timeout"],
        )
        assert "/w/api.php" not in mock_client.get.call_args.args[0]


class TestFetchPending:
    @pytest.mark.asyncio
    async def test_returns_stats_dict(self):
        stats = await fetch_pending(limit=0)
        assert "fetched" in stats
        assert "skipped" in stats
        assert "unfetchable" in stats

    @pytest.mark.asyncio
    async def test_no_pending_returns_zeros(self):
        stats = await fetch_pending()
        assert stats["fetched"] == 0

    def test_two_asyncio_run_calls_do_not_break_limiter(self, monkeypatch):
        upsert_document("firefox", "F5", "T5", "https://ok.com/x", None)

        async def fake_fetch(client, doc_id, url):
            return FetchResult(doc_id, url, "skipped", None, None, "non-html extension")

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)
        first = asyncio.run(fetch_pending())
        second = asyncio.run(fetch_pending())
        assert first["skipped"] == 1
        assert second["fetched"] == 0

    @pytest.mark.asyncio
    async def test_pending_docs_are_fetched(self, monkeypatch):
        upsert_document("firefox", "F1", "T1", "https://ok.com/page", None)
        upsert_document("firefox", "F2", "T2", "https://ok.com/page2", None)

        async def fake_fetch(client, doc_id, url):
            return FetchResult(doc_id, url, "fetched", "Some text content here", 200, None)

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)
        stats = await fetch_pending()
        assert stats["fetched"] == 2
        assert len(stats["texts"]) == 2

    @pytest.mark.asyncio
    async def test_persists_each_result_before_batch_end(self, monkeypatch):
        import sqlalchemy as sa
        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table

        upsert_document("firefox", "F4", "T4", "https://fail.com", None)

        async def fake_fetch(client, doc_id, url):
            return FetchResult(doc_id, url, "unfetchable", None, 404, "not found")

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)
        await fetch_pending()

        with get_engine().connect() as con:
            row = con.execute(
                sa.select(docs_table.c.fetch_status).where(
                    docs_table.c.source_id == "F4"
                )
            ).fetchone()
        assert row[0] == "unfetchable"

    @pytest.mark.asyncio
    async def test_cancelled_fetch_still_persists_completed_urls(self, monkeypatch):
        import sqlalchemy as sa
        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table
        from pka.ingestion import sync_progress as sp

        d1 = upsert_document("firefox", "F20", "T20", "https://a.com", None)
        upsert_document("firefox", "F21", "T21", "https://b.com", None)

        call = {"n": 0}

        async def fake_fetch(client, doc_id, url):
            call["n"] += 1
            if call["n"] == 1:
                sp.request_cancel("firefox-fetch")
            return FetchResult(doc_id, url, "unfetchable", None, 404, "gone")

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)
        sp.begin("firefox-fetch")
        sp.set_phase("firefox-fetch", "fetching", 2)
        await fetch_pending(progress_key="firefox-fetch")

        with get_engine().connect() as con:
            row = con.execute(
                sa.select(docs_table.c.fetch_status).where(docs_table.c.id == d1)
            ).fetchone()
        assert row[0] == "unfetchable"

    @pytest.mark.asyncio
    async def test_stops_when_cancel_requested(self, monkeypatch):
        from pka.ingestion import sync_progress as sp

        upsert_document("firefox", "F10", "T10", "https://a.com", None)
        upsert_document("firefox", "F11", "T11", "https://b.com", None)
        upsert_document("firefox", "F12", "T12", "https://c.com", None)

        async def fake_fetch(client, doc_id, url):
            if doc_id == 2:
                sp.request_cancel("firefox-fetch")
            return FetchResult(doc_id, url, "fetched", "Page text content here.", 200, None)

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)
        sp.begin("firefox-fetch")
        sp.set_phase("firefox-fetch", "fetching", 3)
        stats = await fetch_pending(progress_key="firefox-fetch")
        assert stats.get("stopped") == "cancel"
        assert stats["fetched"] <= 3


class TestExtractText:
    def test_trafilatura_primary(self, monkeypatch):
        import sys
        fake_traf = MagicMock()
        fake_traf.extract.return_value = "Extracted article body text."
        monkeypatch.setitem(sys.modules, "trafilatura", fake_traf)
        from pka.ingestion.fetcher import _extract_text
        text = _extract_text("<html><body>ignored</body></html>", "https://x.com")
        assert text == "Extracted article body text."

    def test_readability_fallback(self, monkeypatch):
        import sys
        fake_traf = MagicMock()
        fake_traf.extract.return_value = None
        monkeypatch.setitem(sys.modules, "trafilatura", fake_traf)

        class FakeDoc:
            def summary(self):
                return "<p>Readability extracted content here.</p>"

        fake_readability = MagicMock()
        fake_readability.Document = lambda html: FakeDoc()
        monkeypatch.setitem(sys.modules, "readability", fake_readability)

        from pka.ingestion.fetcher import _extract_text
        text = _extract_text("<html></html>", "https://x.com")
        assert "Readability" in text

    def test_html_strip_last_resort(self, monkeypatch):
        import sys
        fake_traf = MagicMock()
        fake_traf.extract.return_value = None
        monkeypatch.setitem(sys.modules, "trafilatura", fake_traf)

        fake_readability = MagicMock()
        fake_readability.Document = MagicMock(
            side_effect=RuntimeError("no readability"),
        )
        monkeypatch.setitem(sys.modules, "readability", fake_readability)

        from pka.ingestion.fetcher import _extract_text
        text = _extract_text("<p>Plain fallback text content.</p>", "https://x.com")
        assert "Plain fallback" in text

    @pytest.mark.asyncio
    async def test_request_error_unfetchable(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = httpx.RequestError("connection reset")
        result = await _fetch_one(mock_client, doc_id=5, url="https://example.com")
        assert result.status == "unfetchable"
        assert result.document_id == 5

    @pytest.mark.asyncio
    async def test_empty_extraction_unfetchable(self, monkeypatch):
        monkeypatch.setattr("pka.ingestion.fetcher._extract_text", lambda h, u: None)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(200)
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com")
        assert result.status == "unfetchable"
        assert "no text" in (result.error_msg or "").lower()


class TestResetUnfetchableForFetch:
    def test_does_not_reset_when_not_dev(self, monkeypatch):
        monkeypatch.setattr("pka.config.settings.dev", False)
        wiki_id = upsert_document(
            "firefox", "F-WIKI", "Wiki", "https://en.wikipedia.org/wiki/Python", None,
        )
        other_id = upsert_document(
            "firefox", "F-403", "Blocked", "https://example.com/blocked", None,
        )
        import sqlalchemy as sa
        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table

        with get_engine().begin() as con:
            con.execute(
                docs_table.update()
                .where(docs_table.c.id.in_([wiki_id, other_id]))
                .values(fetch_status=str(FetchStatus.UNFETCHABLE))
            )

        count = reset_unfetchable_for_fetch()

        assert count == 0
        with get_engine().connect() as con:
            rows = {
                r[0]: r[1]
                for r in con.execute(
                    sa.select(docs_table.c.id, docs_table.c.fetch_status).where(
                        docs_table.c.id.in_([wiki_id, other_id])
                    )
                ).fetchall()
            }
        assert rows[wiki_id] == str(FetchStatus.UNFETCHABLE)
        assert rows[other_id] == str(FetchStatus.UNFETCHABLE)

    def test_resets_all_unfetchable_in_dev(self, monkeypatch):
        monkeypatch.setattr("pka.config.settings.dev", True)
        wiki_id = upsert_document(
            "firefox", "F-WIKI2", "Wiki", "https://en.wikipedia.org/wiki/Go", None,
        )
        other_id = upsert_document(
            "firefox", "F-403B", "Blocked", "https://example.com/gone", None,
        )
        local_id = upsert_document(
            "firefox", "F-LOCAL", "Local", "file:///C:/Users/foo.pdf", None,
        )
        import sqlalchemy as sa
        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table

        with get_engine().begin() as con:
            con.execute(
                docs_table.update()
                .where(docs_table.c.id.in_([wiki_id, other_id, local_id]))
                .values(fetch_status=str(FetchStatus.UNFETCHABLE))
            )

        count = reset_unfetchable_for_fetch()

        assert count == 2
        with get_engine().connect() as con:
            rows = {
                r[0]: r[1]
                for r in con.execute(
                    sa.select(docs_table.c.id, docs_table.c.fetch_status).where(
                        docs_table.c.id.in_([wiki_id, other_id, local_id])
                    )
                ).fetchall()
            }
        assert rows[wiki_id] == str(FetchStatus.PENDING)
        assert rows[other_id] == str(FetchStatus.PENDING)
        assert rows[local_id] == str(FetchStatus.UNFETCHABLE)

