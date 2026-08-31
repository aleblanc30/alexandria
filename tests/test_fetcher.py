import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pka.constants import FetchStatus, PdfTextLayer
from pka.db.queries import DocumentWrite, init_db, upsert_document
from pka.ingestion.book_extractor import BookExtraction
from pka.ingestion.fetcher import (
    FetchResult,
    _fetch_one,
    _persist_fetch_result,
    bookmark_url_unfetchable_reason,
    fetch_and_embed_pending,
    fetch_pending,
    reset_unfetchable_for_fetch,
)
from tests.conftest import make_document


def _pdf_extraction(text: str) -> BookExtraction:
    """One page-group of extracted text, as the real extractor would return it."""
    return BookExtraction(
        [{"title": "Pages 1–1", "text": text, "index": 0, "page_start": 1, "page_end": 1}],
        PdfTextLayer.TEXT,
        page_count=1,
        text_pages=1,
    )


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


class TestExtractPdfFromBytes:
    def test_extractor_reopens_temp_file_by_path(self, monkeypatch):
        """The temp PDF must be closed before the extractor reopens it by name
        (Windows locks files held open by NamedTemporaryFile)."""
        from pka.ingestion import fetch_base

        seen: dict = {}

        def fake_extract(path, max_pages=None):
            seen["path"] = Path(path)
            assert Path(path).read_bytes().startswith(b"%PDF")
            return BookExtraction(
                [{"title": "", "text": "extracted text"}],
                PdfTextLayer.TEXT,
            )

        monkeypatch.setattr(fetch_base, "extract_pdf_report", fake_extract)
        report = fetch_base._extract_pdf_from_bytes(b"%PDF-1.4 fake body")
        assert fetch_base._sections_text(report.sections) == "extracted text"
        assert not seen["path"].exists()

    def test_temp_file_removed_when_extractor_raises(self, monkeypatch):
        from pka.ingestion import fetch_base

        seen: dict = {}

        def fake_extract(path, max_pages=None):
            seen["path"] = Path(path)
            raise RuntimeError("boom")

        monkeypatch.setattr(fetch_base, "extract_pdf_report", fake_extract)
        with pytest.raises(RuntimeError):
            fetch_base._extract_pdf_from_bytes(b"%PDF-1.4 fake body")
        assert not seen["path"].exists()


def _html_response(
    status: int = 200,
    body: str = "<html><body><p>Hello world, this is content.</p></body></html>",
    content_type: str = "text/html",
) -> MagicMock:
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
        mock_client.get.return_value = _html_response(
            200, "<html><body><p>" + "word " * 50 + "</p></body></html>"
        )
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
            "pka.ingestion.fetch_base._extract_pdf_from_bytes",
            lambda data, **kw: _pdf_extraction(
                "Extracted PDF text with enough content to embed.",
            ),
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
            "pka.ingestion.fetch_base._extract_pdf_from_bytes",
            lambda data, **kw: BookExtraction([], PdfTextLayer.UNREADABLE),
        )
        result = await _fetch_one(mock_client, doc_id=1, url="https://arxiv.org/paper.pdf")
        assert result.status == "unfetchable"
        assert "pdf extraction" in (result.error_msg or "").lower()

    @pytest.mark.asyncio
    async def test_scanned_pdf_gets_its_own_status(self, monkeypatch):
        """A scan is not a broken URL: re-fetching it can never produce text."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _pdf_response()
        monkeypatch.setattr(
            "pka.ingestion.fetch_base._extract_pdf_from_bytes",
            lambda data, **kw: BookExtraction(
                [],
                PdfTextLayer.NONE,
                page_count=12,
            ),
        )
        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/scan.pdf")
        assert result.status == FetchStatus.NO_TEXT_LAYER
        assert "no text layer" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_fetched_for_pdf_content_type_without_extension(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _pdf_response()
        monkeypatch.setattr(
            "pka.ingestion.fetch_base._extract_pdf_from_bytes",
            lambda data, **kw: _pdf_extraction("PDF body from content-type route."),
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
        make_document("firefox", "F5", "T5", "https://ok.com/x", None)

        async def fake_fetch(client, doc_id, url):
            return FetchResult(doc_id, url, "skipped", None, None, "non-html extension")

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)
        first = asyncio.run(fetch_pending())
        second = asyncio.run(fetch_pending())
        assert first["skipped"] == 1
        assert second["fetched"] == 0

    @pytest.mark.asyncio
    async def test_pending_docs_are_fetched(self, monkeypatch):
        make_document("firefox", "F1", "T1", "https://ok.com/page", None)
        make_document("firefox", "F2", "T2", "https://ok.com/page2", None)

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

        make_document("firefox", "F4", "T4", "https://fail.com", None)

        async def fake_fetch(client, doc_id, url):
            return FetchResult(doc_id, url, "unfetchable", None, 404, "not found")

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)
        await fetch_pending()

        with get_engine().connect() as con:
            row = con.execute(
                sa.select(docs_table.c.fetch_status).where(docs_table.c.source_id == "F4")
            ).fetchone()
        assert row[0] == "unfetchable"

    @pytest.mark.asyncio
    async def test_cancelled_fetch_still_persists_completed_urls(self, monkeypatch):
        import sqlalchemy as sa

        from pka.db.queries import get_engine
        from pka.db.schema import documents as docs_table
        from pka.ingestion import progress as sp

        d1 = make_document("firefox", "F20", "T20", "https://a.com", None)
        make_document("firefox", "F21", "T21", "https://b.com", None)

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
        from pka.ingestion import progress as sp

        make_document("firefox", "F10", "T10", "https://a.com", None)
        make_document("firefox", "F11", "T11", "https://b.com", None)
        make_document("firefox", "F12", "T12", "https://c.com", None)

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


class TestFetchAndEmbedPending:
    @pytest.mark.asyncio
    async def test_calls_embed_fn_after_each_successful_fetch(self, monkeypatch):
        d1 = make_document("firefox", "F30", "T", "https://embed.example", None)
        embed_calls: list[tuple[int, str]] = []

        def embed_fn(doc_id: int, text: str, card_summary=None) -> dict:
            embed_calls.append((doc_id, text))
            return {"processed": True, "chunks": 2, "skipped": False, "failed": False}

        async def fake_fetch(client, doc_id, url):
            return FetchResult(
                doc_id,
                url,
                "fetched",
                "Enough extracted text to form a valid chunk for embedding.",
                200,
                None,
            )

        monkeypatch.setattr(
            "pka.db.queries.firefox_ingest_queue", lambda limit: [(d1, "https://embed.example")]
        )
        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)

        stats = await fetch_and_embed_pending(limit=None, embed_fn=embed_fn)

        assert embed_calls == [(d1, "Enough extracted text to form a valid chunk for embedding.")]
        assert stats["fetched"] == 1
        assert stats["embed"]["processed"] == 1
        assert stats["embed"]["chunks"] == 2
        assert "texts" not in stats

    @pytest.mark.asyncio
    async def test_includes_orphans_in_work_queue(self, monkeypatch):
        orphan = make_document(
            "firefox",
            "F31",
            "T",
            "https://orphan.example",
            None,
            fetch_status=FetchStatus.FETCHED,
        )
        embed_calls: list[int] = []

        def embed_fn(doc_id: int, text: str, card_summary=None) -> dict:
            embed_calls.append(doc_id)
            return {"processed": True, "chunks": 1, "skipped": False, "failed": False}

        async def fake_fetch(client, doc_id, url):
            return FetchResult(
                doc_id, url, "fetched", "Recovered orphan page text content.", 200, None
            )

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_one", fake_fetch)

        stats = await fetch_and_embed_pending(limit=None, embed_fn=embed_fn)

        assert embed_calls == [orphan]
        assert stats["embed"]["processed"] == 1


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
        wiki_id = make_document(
            "firefox",
            "F-WIKI",
            "Wiki",
            "https://en.wikipedia.org/wiki/Python",
            None,
        )
        other_id = make_document(
            "firefox",
            "F-403",
            "Blocked",
            "https://example.com/blocked",
            None,
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
        wiki_id = make_document(
            "firefox",
            "F-WIKI2",
            "Wiki",
            "https://en.wikipedia.org/wiki/Go",
            None,
        )
        other_id = make_document(
            "firefox",
            "F-403B",
            "Blocked",
            "https://example.com/gone",
            None,
        )
        local_id = make_document(
            "firefox",
            "F-LOCAL",
            "Local",
            "file:///C:/Users/foo.pdf",
            None,
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


_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>Attention Is All You Need Again</title>
    <summary>We revisit transformer architectures for language modeling.</summary>
    <author><name>Alice Smith</name></author>
  </entry>
</feed>
"""

_BIORXIV_JSON = {
    "collection": [
        {
            "doi": "10.1101/2024.01.16.575895",
            "title": "A neural circuit for reward",
            "authors": "Smith, A.",
            "abstract": "We map dopamine neurons in the ventral tegmental area.",
            "version": "1",
        }
    ],
}


class TestPreprintFetchIntegration:
    @pytest.mark.asyncio
    async def test_fetch_one_uses_arxiv_handler(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        api_resp = httpx.Response(200, text=_ARXIV_ATOM, request=httpx.Request("GET", "http://x"))
        mock_client.get.return_value = api_resp

        async def fake_pdf(client, arxiv_id):
            return "Full paper body with enough extracted text for embedding.", 200, None

        monkeypatch.setattr("pka.ingestion.arxiv._fetch_arxiv_pdf_text", fake_pdf)

        result = await _fetch_one(
            mock_client,
            doc_id=1,
            url="https://arxiv.org/abs/2301.00001",
        )

        assert result.status == "fetched"
        assert result.title == "Attention Is All You Need Again"
        assert result.card_summary == "We revisit transformer architectures for language modeling."
        assert result.error_msg == "fetched via arxiv api"

    @pytest.mark.asyncio
    async def test_persist_updates_title_and_card_summary(self):
        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.queries import get_engine
        from pka.db.schema import documents

        doc_id = make_document(
            source=Source.FIREFOX,
            source_id="F-ARX",
            title="Old bookmark title",
            url_or_path="https://arxiv.org/abs/2301.00001",
            date_added=None,
            fetch_status=FetchStatus.PENDING,
        )
        _persist_fetch_result(
            FetchResult(
                doc_id,
                "https://arxiv.org/abs/2301.00001",
                "fetched",
                "Paper text for embedding.",
                200,
                "fetched via arxiv api",
                title="Attention Is All You Need Again",
                card_summary="We revisit transformer architectures for language modeling.",
            )
        )
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.title, documents.c.card_summary).where(
                    documents.c.id == doc_id
                )
            ).fetchone()
        assert row[0] == "Attention Is All You Need Again"
        assert row[1] == "We revisit transformer architectures for language modeling."

    @pytest.mark.asyncio
    async def test_persist_writes_structured_metadata_when_present(self):
        import json

        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.queries import get_engine
        from pka.db.schema import documents

        doc_id = make_document(
            source=Source.FIREFOX,
            source_id="F-ARX-META",
            title="Old bookmark title",
            url_or_path="https://arxiv.org/abs/2301.00001",
            date_added=None,
            fetch_status=FetchStatus.PENDING,
        )
        _persist_fetch_result(
            FetchResult(
                doc_id,
                "https://arxiv.org/abs/2301.00001",
                "fetched",
                "Paper text for embedding.",
                200,
                "fetched via arxiv api",
                title="Attention Is All You Need Again",
                card_summary="We revisit transformer architectures for language modeling.",
                doi="10.48550/arxiv.2301.00001",
                arxiv_id="2301.00001",
                authors_json=json.dumps(["Alice Smith", "Bob Jones"]),
            )
        )
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(
                    documents.c.doi,
                    documents.c.arxiv_id,
                    documents.c.authors_json,
                ).where(documents.c.id == doc_id)
            ).fetchone()
        assert row[0] == "10.48550/arxiv.2301.00001"
        assert row[1] == "2301.00001"
        assert json.loads(row[2]) == ["Alice Smith", "Bob Jones"]

    @pytest.mark.asyncio
    async def test_persist_leaves_metadata_untouched_when_absent(self):
        """A refetch with no structured metadata must not blank a stored value."""
        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.queries import get_engine
        from pka.db.schema import documents

        doc_id = upsert_document(
            DocumentWrite(
                source=Source.FIREFOX,
                source_id="F-ARX-KEEP",
                title="Title",
                url_or_path="https://arxiv.org/abs/2301.00001",
                date_added=None,
                fetch_status=FetchStatus.PENDING,
                doi="10.48550/arxiv.2301.00001",
            )
        )
        _persist_fetch_result(
            FetchResult(
                doc_id,
                "https://arxiv.org/abs/2301.00001",
                "fetched",
                "Paper text.",
                200,
                "fetched via arxiv api",
            )
        )
        with get_engine().connect() as con:
            doi = con.execute(sa.select(documents.c.doi).where(documents.c.id == doc_id)).scalar()
        assert doi == "10.48550/arxiv.2301.00001"

    @pytest.mark.asyncio
    async def test_embed_preserves_api_card_summary(self, mock_chroma):
        import sqlalchemy as sa

        from pka.constants import Source
        from pka.db.queries import get_engine
        from pka.db.schema import documents
        from pka.ingestion.runners.firefox import embed_fetched_text

        doc_id = make_document(
            source=Source.FIREFOX,
            source_id="F-ARX2",
            title="Paper title",
            url_or_path="https://arxiv.org/abs/2301.00001",
            date_added=None,
            fetch_status=FetchStatus.FETCHED,
        )
        abstract = "We revisit transformer architectures for language modeling."
        body = "PDF opening line that should not replace the abstract on the card.\n" * 5
        _persist_fetch_result(
            FetchResult(
                doc_id,
                "https://arxiv.org/abs/2301.00001",
                "fetched",
                body,
                200,
                "fetched via arxiv api",
                title="Attention Is All You Need Again",
                card_summary=abstract,
            )
        )
        embed_fetched_text(doc_id, body, card_summary=abstract, skip_existing=False)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.card_summary).where(documents.c.id == doc_id)
            ).fetchone()
        assert row[0] == abstract


class TestEventLoopNotBlocked:
    """Content extraction and result persistence must run off the event loop.

    When they ran inline, one slow page (a big PDF, a heavy trafilatura parse)
    froze every other worker. Because ``_fetch_one`` guards each URL with an
    ``asyncio.wait_for`` deadline measured in wall-clock time, those frozen
    workers burned their budget while blocked and were recorded as
    ``unfetchable`` / "timeout" even though their server had answered fine.
    """

    @pytest.mark.asyncio
    async def test_slow_extraction_does_not_serialize_sibling_fetches(self, monkeypatch):
        import time

        blocking_seconds = 0.4
        parallel_urls = 4

        def blocking_extract(html, url):
            time.sleep(blocking_seconds)
            return "extracted text long enough to keep"

        monkeypatch.setattr("pka.ingestion.fetcher._extract_text", blocking_extract)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(200)

        started = time.monotonic()
        results = await asyncio.gather(
            *(
                _fetch_one(mock_client, doc_id=i, url=f"https://loopblock-{i}.example/{i}")
                for i in range(parallel_urls)
            )
        )
        elapsed = time.monotonic() - started

        assert all(r.status == "fetched" for r in results)
        # Serialized on the loop this would cost parallel_urls * blocking_seconds.
        assert elapsed < blocking_seconds * parallel_urls * 0.75, (
            f"extraction appears to be serialized on the event loop ({elapsed:.2f}s)"
        )

    @pytest.mark.asyncio
    async def test_one_heavy_pdf_does_not_time_out_healthy_siblings(self, monkeypatch):
        """A slow PDF extraction must not spend other URLs' timeout budgets.

        The three HTML pages here answer instantly. Serialized behind an on-loop
        PDF extraction they never get to run before their deadline and are
        recorded as "timeout" — a failure invented by the fetcher, not the site.
        """
        import time

        from pka.ingestion import fetch_base

        def slow_pdf_extract(data, **kwargs):
            time.sleep(2.0)
            return _pdf_extraction("PDF text long enough to keep.")

        monkeypatch.setattr(fetch_base, "_extract_pdf_from_bytes", slow_pdf_extract)

        pdf_url = "https://loopblock-pdf.example/book.pdf"

        async def respond(url, **kwargs):
            if url == pdf_url:
                return _pdf_response()
            # A real await, so the sibling is genuinely suspended on I/O with its
            # wait_for timer already running when the PDF extraction begins.
            await asyncio.sleep(0.3)
            return _html_response(200)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = respond

        # Warm the lazy per-site imports inside _fetch_one so their one-off cost
        # is not charged to the first fetch's budget below.
        await _fetch_one(mock_client, doc_id=99, url="https://loopblock-warm.example/")

        monkeypatch.setattr("pka.ingestion.fetcher._fetch_budget_seconds", lambda **kw: 1.2)

        html_urls = [f"https://loopblock-{i}.example/{i}" for i in range(3)]
        # PDF last: the siblings start, suspend on their 0.3s read, and only then
        # does the heavy extraction begin. That is the live worker-pool ordering.
        results = await asyncio.gather(
            *(_fetch_one(mock_client, doc_id=i + 1, url=u) for i, u in enumerate(html_urls)),
            _fetch_one(mock_client, doc_id=0, url=pdf_url),
        )

        siblings = results[:3]
        assert [r.error_msg for r in siblings] == [None] * 3
        assert all(r.status == "fetched" for r in siblings)
