"""Reddit saved-posts connector — the private Atom feed and its failure modes."""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from pka.config import settings as cfg
from pka.connectors.reddit import RedditConnectorError, load_saved

# ── Private feed loader ──────────────────────────────────────────────────────
#
# Only the Atom (.rss) form of /prefs/feeds/ is fetched; shapes are defined
# alongside the tests that use them, below.

FEED_URL = "https://www.reddit.com/saved.rss?feed=abc123token&user=someone"

_EMPTY_ATOM = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom"/>'
)


class _Feed(tuple):
    """(calls, pages) for existing unpacking, with ``.sleeps`` alongside."""

    def __new__(cls, calls, pages, sleeps):
        self = super().__new__(cls, (calls, pages))
        self.calls, self.pages, self.sleeps = calls, pages, sleeps
        return self


class _FakeResponse:
    def __init__(self, text_body="", status_code=200):
        self.text = text_body
        self.status_code = status_code


@pytest.fixture()
def feed_http(monkeypatch):
    """Capture httpx.get calls and serve queued pages in order.

    ``time.sleep`` is recorded rather than performed: the loader throttles
    between pages, and a suite that honoured that would take minutes.
    """
    import httpx

    calls: list[dict] = []
    pages: list[_FakeResponse] = []
    sleeps: list[float] = []

    monkeypatch.setattr("pka.connectors.reddit.time.sleep", sleeps.append)

    def _get(url, params=None, headers=None, timeout=None, follow_redirects=False):
        calls.append({
            "url": url,
            "params": dict(params or {}),
            "headers": headers,
            # httpx logs the full URL at INFO and the URL carries the feed token,
            # so the loader is expected to have quietened this logger by now.
            "httpx_log_level": logging.getLogger("httpx").level,
        })
        return pages.pop(0) if pages else _FakeResponse(_EMPTY_ATOM)

    monkeypatch.setattr(httpx, "get", _get)
    return _Feed(calls, pages, sleeps)


class TestLoaderSelection:
    def test_configured_feed_url_is_what_gets_fetched(self, feed_http, monkeypatch):
        """No argument needed: the loader reads the URL out of settings."""
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_SELF))
        monkeypatch.setattr(cfg, "reddit_feed_url", FEED_URL)

        items = load_saved()

        assert [i.source_id for i in items] == ["t3_aaa"]
        assert calls[0]["url"] == "https://www.reddit.com/saved.rss"

    def test_no_feed_url_names_the_setting(self, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_feed_url", "")

        with pytest.raises(RedditConnectorError, match="SECRET_ALEXANDRIA_REDDIT_FEED_URL"):
            load_saved()

class TestFeedBlockMitigations:
    """old.reddit.com plus an unrecognised UA earn "403 Blocked" from Reddit."""

    def test_old_reddit_host_is_rewritten_to_www(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_SELF))

        load_saved(
            url="https://old.reddit.com/saved.json?feed=tok&user=someone", limit=None,
        )

        # Both the host and the .json form the user pasted are normalised away.
        assert calls[0]["url"] == "https://www.reddit.com/saved.rss"
        assert calls[0]["params"]["feed"] == "tok"

    def test_user_agent_carries_reddit_attribution(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_SELF))

        load_saved(url=FEED_URL, limit=None)

        assert calls[0]["headers"]["User-Agent"].endswith("(by /u/someone)")

    def test_custom_user_agent_with_attribution_is_left_alone(self, feed_http, monkeypatch):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_SELF))
        monkeypatch.setattr(cfg, "reddit_user_agent", "python:mine:1.0 (by /u/other)")

        load_saved(url=FEED_URL, limit=None)

        assert calls[0]["headers"]["User-Agent"] == "python:mine:1.0 (by /u/other)"

    def test_403_quotes_the_body_that_distinguishes_the_causes(self, feed_http):
        """Bot protection and a rejected token are both 403; only the body differs."""
        calls, pages = feed_http
        pages.append(_FakeResponse("Blocked", status_code=403))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved(url=FEED_URL, limit=None)

        message = str(excinfo.value)
        assert "Server said: Blocked" in message   # the actual evidence
        assert "bot protection" in message         # and how to read it
        assert "abc123token" not in message        # still redacted

    def test_403_with_json_body_points_at_the_token(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(
            '{"message": "Forbidden", "error": 403}', status_code=403,
        ))

        message = str(pytest.raises(
            RedditConnectorError, load_saved, url=FEED_URL, limit=None,
        ).value)
        assert '"error": 403' in message

    def test_long_block_page_is_truncated(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse("x" * 5000, status_code=403))

        message = str(pytest.raises(
            RedditConnectorError, load_saved, url=FEED_URL, limit=None,
        ).value)
        # The excerpt is cut short; the whole body goes to the saved file instead.
        assert "…" in message
        assert "x" * 500 not in message
        assert "Full response saved to" in message

    def test_httpx_url_logging_is_suppressed_during_the_request(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_SELF))
        httpx_log = logging.getLogger("httpx")
        httpx_log.setLevel(logging.INFO)

        load_saved(url=FEED_URL, limit=None)

        assert calls[0]["httpx_log_level"] == logging.WARNING  # quiet in flight
        assert httpx_log.level == logging.INFO                 # restored after

# ── Atom (.rss) feed loader ──────────────────────────────────────────────────
#
# Shapes mirror Reddit's Atom output: <id> carries the t3_/t1_ fullname,
# <category term> the subreddit, and <content type="html"> the body plus a
# footer of links whose "[link]" anchor is the off-reddit target.

ATOM_SELF = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>saved</title>
  <entry>
    <id>t3_aaa</id>
    <title>Anyone got any good leftist book suggestions</title>
    <category term="socialism" label="r/socialism"/>
    <updated>2026-08-16T09:11:48+00:00</updated>
    <link href="https://www.reddit.com/r/socialism/comments/aaa/anyone_got/"/>
    <content type="html">&lt;div class="md"&gt;&lt;p&gt;I have read the manifesto.&lt;/p&gt;&lt;/div&gt;
      &amp;lt;span&amp;gt;submitted by &lt;a href="https://www.reddit.com/user/someone"&gt;/u/someone&lt;/a&gt;
      to &lt;a href="https://www.reddit.com/r/socialism/"&gt;r/socialism&lt;/a&gt;&amp;lt;/span&amp;gt;</content>
  </entry>
</feed>
"""

ATOM_LINK_POST = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_bbb</id>
    <title>Paxos made simple</title>
    <category term="compsci" label="r/compsci"/>
    <updated>2023-11-14T22:15:00+00:00</updated>
    <link href="https://www.reddit.com/r/compsci/comments/bbb/paxos/"/>
    <content type="html">&lt;a href="https://example.com/paxos.pdf"&gt;[link]&lt;/a&gt;
      &lt;a href="https://www.reddit.com/r/compsci/comments/bbb/paxos/"&gt;[comments]&lt;/a&gt;</content>
  </entry>
</feed>
"""

ATOM_COMMENT = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t1_ccc</id>
    <title>What kind of problematics can fit in a Solarpunk story ?</title>
    <category term="solarpunk" label="r/solarpunk"/>
    <updated>2023-11-14T22:20:00+00:00</updated>
    <link href="https://www.reddit.com/r/solarpunk/comments/xyz/what_kind/ccc/"/>
    <content type="html">&lt;div class="md"&gt;&lt;p&gt;That is one way of handling it.&lt;/p&gt;&lt;/div&gt;</content>
  </entry>
</feed>
"""


class TestAtomFeedLoader:
    def test_self_post_maps_with_inline_body(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_SELF))

        (item,) = load_saved(url=FEED_URL, limit=None)

        assert calls[0]["url"] == "https://www.reddit.com/saved.rss"
        assert calls[0]["params"]["feed"] == "abc123token"
        assert item.source_id == "t3_aaa"
        assert item.kind == "post"
        assert item.subreddit == "socialism"
        assert item.collection == "r/socialism"
        assert "manifesto" in item.body
        assert item.external_url is None
        expected = int(
            datetime.fromisoformat("2026-08-16T09:11:48+00:00").timestamp()
        )
        assert item.date_added == expected

    def test_link_post_takes_external_from_link_anchor(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_LINK_POST))

        (item,) = load_saved(url=FEED_URL, limit=None)

        assert item.external_url == "https://example.com/paxos.pdf"
        assert item.url_or_path == "https://example.com/paxos.pdf"
        # The [comments] anchor points back at reddit and must not win.
        assert "reddit.com" not in item.external_url

    def test_comment_titled_by_its_thread(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_COMMENT))

        (item,) = load_saved(url=FEED_URL, limit=None)

        assert item.kind == "comment"
        assert item.title.startswith('Comment on "What kind')
        assert item.external_url is None

    def test_rss_url_input_still_hits_rss(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(ATOM_SELF))

        load_saved(url="https://www.reddit.com/saved.rss?feed=tok", limit=None)

        assert calls[0]["url"] == "https://www.reddit.com/saved.rss"

    def test_unparseable_xml_reports_actionably(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse("<html>nope"))

        with pytest.raises(RedditConnectorError, match="parseable Atom"):
            load_saved(url=FEED_URL, limit=None)


class TestAtomPagination:
    """Atom carries no cursor field, so the last entry's <id> becomes `after`."""

    @staticmethod
    def _atom(ids):
        entries = "".join(
            f"""
  <entry>
    <id>{fullname}</id>
    <title>Post {fullname}</title>
    <category term="x" label="r/x"/>
    <updated>2026-01-01T00:00:00+00:00</updated>
    <link href="https://www.reddit.com/r/x/comments/{fullname[3:]}/p/"/>
    <content type="html">&lt;p&gt;body&lt;/p&gt;</content>
  </entry>"""
            for fullname in ids
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">' + entries + "</feed>"
        )

    def test_requests_full_pages_when_no_limit_is_set(self, feed_http):
        """Without an explicit limit Reddit serves 25; ask for the maximum."""
        calls, pages = feed_http
        pages.append(_FakeResponse(self._atom(["t3_a"])))

        load_saved(url=FEED_URL, limit=None)

        assert calls[0]["params"]["limit"] == "100"

    def test_walks_pages_using_the_last_entry_as_cursor(self, feed_http):
        calls, pages = feed_http
        first = [f"t3_{i:03d}" for i in range(100)]
        pages.append(_FakeResponse(self._atom(first)))
        pages.append(_FakeResponse(self._atom(["t3_last"])))

        items = load_saved(url=FEED_URL, limit=None)

        assert len(items) == 101
        assert "after" not in calls[0]["params"]
        assert calls[1]["params"]["after"] == "t3_099"   # last id of page 1
        assert calls[1]["params"]["feed"] == "abc123token"

    def test_short_page_ends_the_walk(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(self._atom(["t3_a", "t3_b"])))

        items = load_saved(url=FEED_URL, limit=None)

        assert len(items) == 2
        assert len(calls) == 1   # fewer entries than asked for → no more pages

    def test_ignored_cursor_does_not_loop_forever(self, feed_http):
        """A server replaying the same page must not spin the budget."""
        calls, pages = feed_http
        same = self._atom([f"t3_{i:03d}" for i in range(100)])
        for _ in range(5):
            pages.append(_FakeResponse(same))

        items = load_saved(url=FEED_URL, limit=None)

        assert len(items) == 100     # deduped
        assert len(calls) == 2       # second page added nothing new → stop

    def test_limit_caps_page_size_and_results(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(self._atom(["t3_a", "t3_b", "t3_c"])))

        items = load_saved(url=FEED_URL, limit=2)

        assert [i.source_id for i in items] == ["t3_a", "t3_b"]
        assert calls[0]["params"]["limit"] == "2"

class TestFeedThrottle:
    @staticmethod
    def _atom(ids):
        entries = "".join(
            f"<entry><id>{f}</id><title>P</title><category term=\"x\"/>"
            f"<updated>2026-01-01T00:00:00+00:00</updated>"
            f"<link href=\"https://www.reddit.com/r/x/comments/{f[3:]}/p/\"/>"
            f"<content type=\"html\">body</content></entry>"
            for f in ids
        )
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom">' + entries + "</feed>")

    def test_no_sleep_before_the_first_request(self, feed_http, monkeypatch):
        feed_http.pages.append(_FakeResponse(self._atom(["t3_a"])))

        load_saved(url=FEED_URL, limit=None)

        assert feed_http.sleeps == []   # single page pays nothing

    def test_sleeps_between_pages_within_configured_bounds(self, feed_http, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 1.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 0.5)
        feed_http.pages.append(_FakeResponse(
            self._atom([f"t3_{i:03d}" for i in range(100)]),
        ))
        feed_http.pages.append(_FakeResponse(self._atom(["t3_last"])))

        load_saved(url=FEED_URL, limit=None)

        assert len(feed_http.sleeps) == 1              # one gap between two pages
        assert 1.0 <= feed_http.sleeps[0] <= 1.5       # base + jitter

    def test_jitter_varies_the_delay(self, feed_http, monkeypatch):
        """A fixed cadence is what rate limiters notice; the delay must move."""
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 0.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 1.0)
        full = self._atom([f"t3_{i:03d}" for i in range(100)])
        for _ in range(4):
            feed_http.pages.append(_FakeResponse(full))
        # Distinct ids per page so the walk continues.
        feed_http.pages[1] = _FakeResponse(
            self._atom([f"t3_1{i:02d}" for i in range(100)]))
        feed_http.pages[2] = _FakeResponse(
            self._atom([f"t3_2{i:02d}" for i in range(100)]))
        feed_http.pages[3] = _FakeResponse(self._atom(["t3_end"]))

        load_saved(url=FEED_URL, limit=None)

        assert len(set(feed_http.sleeps)) > 1

    def test_throttle_can_be_disabled(self, feed_http, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 0.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 0.0)
        feed_http.pages.append(_FakeResponse(
            self._atom([f"t3_{i:03d}" for i in range(100)])))
        feed_http.pages.append(_FakeResponse(self._atom(["t3_last"])))

        load_saved(url=FEED_URL, limit=None)

        assert feed_http.sleeps == []


class TestIncrementalVsBackfill:
    """Save-ordered listing: the first known id ends an incremental walk."""

    _atom = staticmethod(TestFeedThrottle._atom)

    def test_stops_at_first_known_id_mid_page(self, feed_http):
        feed_http.pages.append(_FakeResponse(
            self._atom(["t3_new1", "t3_new2", "t3_old", "t3_older"]),
        ))

        items = load_saved(
            url=FEED_URL, limit=None,
            known_ids={"t3_old", "t3_older"}, stop_on_known=True,
        )

        assert [i.source_id for i in items] == ["t3_new1", "t3_new2"]
        assert len(feed_http.calls) == 1
        assert feed_http.sleeps == []   # nothing new means no second request

    def test_backfill_walks_past_known_items(self, feed_http):
        feed_http.pages.append(_FakeResponse(
            self._atom(["t3_new", "t3_old"]),
        ))

        items = load_saved(
            url=FEED_URL, limit=None, known_ids={"t3_old"}, stop_on_known=False,
        )

        assert [i.source_id for i in items] == ["t3_new", "t3_old"]

    def test_nothing_new_returns_empty(self, feed_http):
        feed_http.pages.append(_FakeResponse(self._atom(["t3_old"])))

        items = load_saved(
            url=FEED_URL, limit=None, known_ids={"t3_old"}, stop_on_known=True,
        )

        assert items == []

class TestFailedResponseDump:
    """A block page explains itself in HTML that will not fit in a log line."""

    def test_body_is_written_to_diagnostics_and_named_in_the_error(self, feed_http):
        feed_http.pages.append(_FakeResponse(
            "<body>Blocked, and here is why</body>", status_code=403,
        ))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved(url=FEED_URL, limit=None)

        saved = sorted((cfg.data_dir / "diagnostics").glob("reddit-feed-403-*.html"))
        assert len(saved) == 1
        assert saved[0].read_text(encoding="utf-8") == "<body>Blocked, and here is why</body>"
        assert str(saved[0]) in str(excinfo.value)

    def test_token_never_reaches_the_file_or_its_name(self, feed_http):
        feed_http.pages.append(_FakeResponse("<body>x</body>", status_code=403))

        with pytest.raises(RedditConnectorError):
            load_saved(url=FEED_URL, limit=None)

        for path in (cfg.data_dir / "diagnostics").iterdir():
            assert "abc123token" not in path.name
            assert "abc123token" not in path.read_text(encoding="utf-8")

    def test_plain_text_body_keeps_a_txt_suffix(self, feed_http):
        feed_http.pages.append(_FakeResponse("Blocked", status_code=403))

        with pytest.raises(RedditConnectorError):
            load_saved(url=FEED_URL, limit=None)

        assert list((cfg.data_dir / "diagnostics").glob("*.txt"))

    def test_empty_body_writes_nothing(self, feed_http):
        feed_http.pages.append(_FakeResponse("", status_code=429))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved(url=FEED_URL, limit=None)

        assert not (cfg.data_dir / "diagnostics").exists()
        assert "saved to" not in str(excinfo.value)

    def test_browser_opens_only_when_asked(self, feed_http, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", opened.append)
        feed_http.pages.append(_FakeResponse("<body>x</body>", status_code=403))

        with pytest.raises(RedditConnectorError):
            load_saved(url=FEED_URL, limit=None)
        assert opened == []

        monkeypatch.setattr(cfg, "reddit_feed_open_failed_page", True)
        feed_http.pages.append(_FakeResponse("<body>x</body>", status_code=403))
        with pytest.raises(RedditConnectorError):
            load_saved(url=FEED_URL, limit=None)

        assert len(opened) == 1
        assert opened[0].startswith("file:")

    def test_a_broken_dump_does_not_replace_the_real_error(self, feed_http, monkeypatch):
        """The diagnostic must never eclipse the failure it exists to explain."""
        monkeypatch.setattr(
            "pathlib.Path.write_text",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
        )
        feed_http.pages.append(_FakeResponse("<body>x</body>", status_code=403))

        with pytest.raises(RedditConnectorError, match="403"):
            load_saved(url=FEED_URL, limit=None)
