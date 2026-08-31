import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pka.ingestion.fetcher import _fetch_one
from pka.ingestion.wayback import fetch_via_wayback, lookup_snapshot_url


def _html_response(
    status: int = 200,
    body: str = "<html><body><p>Hello world, this is archived content.</p></body></html>",
    content_type: str = "text/html",
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = body
    resp.content = body.encode("utf-8")
    resp.headers = {"content-type": content_type}
    return resp


def _availability_response(
    snapshot_url: str,
    timestamp: str = "20190603190145",
) -> MagicMock:
    payload = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": snapshot_url,
                "timestamp": timestamp,
                "status": "200",
            }
        }
    }
    body = json.dumps(payload)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.text = body
    resp.content = body.encode("utf-8")
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload
    return resp


def _empty_availability_response() -> MagicMock:
    payload = {"archived_snapshots": {}}
    body = json.dumps(payload)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.text = body
    resp.content = body.encode("utf-8")
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload
    return resp


class TestLookupSnapshotUrl:
    @pytest.mark.asyncio
    async def test_returns_snapshot_when_available(self):
        original = "https://example.com/gone"
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/gone"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _availability_response(snapshot)

        result = await lookup_snapshot_url(mock_client, original)

        assert result == (snapshot, "20190603190145")
        assert "archive.org/wayback/available" in mock_client.get.call_args.args[0]

    @pytest.mark.asyncio
    async def test_returns_none_when_unavailable(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _empty_availability_response()

        result = await lookup_snapshot_url(mock_client, "https://example.com/gone")

        assert result is None


class TestFetchViaWayback:
    @pytest.mark.asyncio
    async def test_fetches_html_snapshot(self):
        original = "https://example.com/gone"
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/gone"

        async def route_get(url, **kwargs):
            if "archive.org/wayback/available" in url:
                return _availability_response(snapshot)
            if url == snapshot:
                return _html_response(200)
            raise AssertionError(f"unexpected URL: {url}")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = route_get

        result = await fetch_via_wayback(mock_client, doc_id=1, url=original)

        assert result is not None
        assert result.status == "fetched"
        assert result.text is not None
        assert result.archive_url == snapshot
        assert "wayback snapshot 20190603190145" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_returns_none_when_no_snapshot(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _empty_availability_response()

        result = await fetch_via_wayback(mock_client, doc_id=1, url="https://example.com/gone")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_snapshot_fetch_fails(self):
        original = "https://example.com/gone"
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/gone"

        async def route_get(url, **kwargs):
            if "archive.org/wayback/available" in url:
                return _availability_response(snapshot)
            if url == snapshot:
                return _html_response(404)
            raise AssertionError(f"unexpected URL: {url}")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = route_get

        result = await fetch_via_wayback(mock_client, doc_id=1, url=original)

        assert result is None


class TestFetchOneWaybackFallback:
    @pytest.mark.asyncio
    async def test_fetched_via_wayback_on_404(self):
        original = "https://example.com/gone"
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/gone"

        async def route_get(url, **kwargs):
            if url == original:
                return _html_response(404)
            if "archive.org/wayback/available" in url:
                return _availability_response(snapshot)
            if url == snapshot:
                return _html_response(200)
            raise AssertionError(f"unexpected URL: {url}")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = route_get

        result = await _fetch_one(mock_client, doc_id=1, url=original)

        assert result.status == "fetched"
        assert result.http_status == 200
        assert "wayback snapshot 20190603190145" in (result.error_msg or "")
        assert result.archive_url == snapshot

    @pytest.mark.asyncio
    async def test_unfetchable_on_404_without_snapshot(self):
        original = "https://example.com/gone"

        async def route_get(url, **kwargs):
            if url == original:
                return _html_response(404)
            if "archive.org/wayback/available" in url:
                return _empty_availability_response()
            raise AssertionError(f"unexpected URL: {url}")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = route_get

        result = await _fetch_one(mock_client, doc_id=1, url=original)

        assert result.status == "unfetchable"
        assert result.http_status == 404

    @pytest.mark.asyncio
    async def test_403_does_not_call_wayback(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(403)

        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/auth")

        assert result.status == "unfetchable"
        assert mock_client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_404_skips_wayback_when_disabled(self, monkeypatch):
        monkeypatch.setattr("pka.config.settings.fetch_wayback_fallback", False)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(404)

        result = await _fetch_one(mock_client, doc_id=1, url="https://example.com/gone")

        assert result.status == "unfetchable"
        assert result.http_status == 404
        assert mock_client.get.call_count == 1


class TestArchiveUrlPersistence:
    def test_persist_fetch_result_stores_archive_url(self):
        import sqlalchemy as sa

        from pka.db.queries import get_engine, init_db, upsert_document
        from pka.db.schema import documents as docs_table
        from pka.ingestion.fetcher import FetchResult, _persist_fetch_result

        init_db()
        original = "https://example.com/gone"
        snapshot = "https://web.archive.org/web/20190603190145/https://example.com/gone"
        doc_id = upsert_document("firefox", "F-WB", "Wayback doc", original, None)

        _persist_fetch_result(
            FetchResult(
                doc_id,
                original,
                "fetched",
                "Archived page text with enough content.",
                200,
                "fetched via wayback snapshot 20190603190145 (original HTTP 404)",
                archive_url=snapshot,
            )
        )

        with get_engine().connect() as con:
            row = con.execute(
                sa.select(
                    docs_table.c.fetch_status,
                    docs_table.c.archive_url,
                    docs_table.c.url_or_path,
                ).where(docs_table.c.id == doc_id)
            ).fetchone()
        assert row[0] == "fetched"
        assert row[1] == snapshot
        assert row[2] == original
