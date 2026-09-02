"""Reddit saved-posts connector — the private Atom feed and its failure modes."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import pytest

from pka.config import settings as cfg
from pka.connectors import reddit_archive
from pka.connectors.reddit import (
    RedditConnectorError,
    load_saved,
    load_saved_from_archive,
)

# ── Private feed loader ──────────────────────────────────────────────────────
#
# Only the Atom (.rss) form of /prefs/feeds/ is fetched; shapes are defined
# alongside the tests that use them, below.

FEED_URL = "https://www.reddit.com/saved.rss?feed=abc123token&user=someone"

_EMPTY_ATOM = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"/>'


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
        calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": headers,
                # httpx logs the full URL at INFO and the URL carries the feed token,
                # so the loader is expected to have quietened this logger by now.
                "httpx_log_level": logging.getLogger("httpx").level,
            }
        )
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
            url="https://old.reddit.com/saved.json?feed=tok&user=someone",
            limit=None,
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
        assert "Server said: Blocked" in message  # the actual evidence
        assert "bot protection" in message  # and how to read it
        assert "abc123token" not in message  # still redacted

    def test_403_with_json_body_points_at_the_token(self, feed_http):
        calls, pages = feed_http
        pages.append(
            _FakeResponse(
                '{"message": "Forbidden", "error": 403}',
                status_code=403,
            )
        )

        message = str(
            pytest.raises(
                RedditConnectorError,
                load_saved,
                url=FEED_URL,
                limit=None,
            ).value
        )
        assert '"error": 403' in message

    def test_403_with_html_body_quotes_visible_text_not_markup(self, feed_http):
        """The real message sits behind a wall of inlined CSS; quote the text, not the wall."""
        calls, pages = feed_http
        page = (
            "<html><head><style>"
            + ("--rem360:22.5rem;" * 200)
            + "</style></head><body>"
            "<div>You've been blocked by network security.</div>"
            "<div>File a ticket.</div>"
            "</body></html>"
        )
        pages.append(_FakeResponse(page, status_code=403))

        message = str(
            pytest.raises(
                RedditConnectorError,
                load_saved,
                url=FEED_URL,
                limit=None,
            ).value
        )
        assert "You've been blocked by network security. File a ticket." in message
        assert "--rem360" not in message  # the stylesheet, not the sentence

    def test_long_block_page_is_truncated(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse("x" * 5000, status_code=403))

        message = str(
            pytest.raises(
                RedditConnectorError,
                load_saved,
                url=FEED_URL,
                limit=None,
            ).value
        )
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
        assert httpx_log.level == logging.INFO  # restored after


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
        expected = int(datetime.fromisoformat("2026-08-16T09:11:48+00:00").timestamp())
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
        assert calls[1]["params"]["after"] == "t3_099"  # last id of page 1
        assert calls[1]["params"]["feed"] == "abc123token"

    def test_short_page_ends_the_walk(self, feed_http):
        calls, pages = feed_http
        pages.append(_FakeResponse(self._atom(["t3_a", "t3_b"])))

        items = load_saved(url=FEED_URL, limit=None)

        assert len(items) == 2
        assert len(calls) == 1  # fewer entries than asked for → no more pages

    def test_ignored_cursor_does_not_loop_forever(self, feed_http):
        """A server replaying the same page must not spin the budget."""
        calls, pages = feed_http
        same = self._atom([f"t3_{i:03d}" for i in range(100)])
        for _ in range(5):
            pages.append(_FakeResponse(same))

        items = load_saved(url=FEED_URL, limit=None)

        assert len(items) == 100  # deduped
        assert len(calls) == 2  # second page added nothing new → stop

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
            f'<entry><id>{f}</id><title>P</title><category term="x"/>'
            f"<updated>2026-01-01T00:00:00+00:00</updated>"
            f'<link href="https://www.reddit.com/r/x/comments/{f[3:]}/p/"/>'
            f'<content type="html">body</content></entry>'
            for f in ids
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">' + entries + "</feed>"
        )

    def test_no_sleep_before_the_first_request(self, feed_http, monkeypatch):
        feed_http.pages.append(_FakeResponse(self._atom(["t3_a"])))

        load_saved(url=FEED_URL, limit=None)

        assert feed_http.sleeps == []  # single page pays nothing

    def test_sleeps_between_pages_within_configured_bounds(self, feed_http, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 1.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 0.5)
        feed_http.pages.append(
            _FakeResponse(
                self._atom([f"t3_{i:03d}" for i in range(100)]),
            )
        )
        feed_http.pages.append(_FakeResponse(self._atom(["t3_last"])))

        load_saved(url=FEED_URL, limit=None)

        assert len(feed_http.sleeps) == 1  # one gap between two pages
        assert 1.0 <= feed_http.sleeps[0] <= 1.5  # base + jitter

    def test_jitter_varies_the_delay(self, feed_http, monkeypatch):
        """A fixed cadence is what rate limiters notice; the delay must move."""
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 0.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 1.0)
        full = self._atom([f"t3_{i:03d}" for i in range(100)])
        for _ in range(4):
            feed_http.pages.append(_FakeResponse(full))
        # Distinct ids per page so the walk continues.
        feed_http.pages[1] = _FakeResponse(self._atom([f"t3_1{i:02d}" for i in range(100)]))
        feed_http.pages[2] = _FakeResponse(self._atom([f"t3_2{i:02d}" for i in range(100)]))
        feed_http.pages[3] = _FakeResponse(self._atom(["t3_end"]))

        load_saved(url=FEED_URL, limit=None)

        assert len(set(feed_http.sleeps)) > 1

    def test_throttle_can_be_disabled(self, feed_http, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 0.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 0.0)
        feed_http.pages.append(_FakeResponse(self._atom([f"t3_{i:03d}" for i in range(100)])))
        feed_http.pages.append(_FakeResponse(self._atom(["t3_last"])))

        load_saved(url=FEED_URL, limit=None)

        assert feed_http.sleeps == []


class TestIncrementalVsBackfill:
    """Save-ordered listing: the first known id ends an incremental walk."""

    _atom = staticmethod(TestFeedThrottle._atom)

    def test_stops_at_first_known_id_mid_page(self, feed_http):
        feed_http.pages.append(
            _FakeResponse(
                self._atom(["t3_new1", "t3_new2", "t3_old", "t3_older"]),
            )
        )

        items = load_saved(
            url=FEED_URL,
            limit=None,
            known_ids={"t3_old", "t3_older"},
            stop_on_known=True,
        )

        assert [i.source_id for i in items] == ["t3_new1", "t3_new2"]
        assert len(feed_http.calls) == 1
        assert feed_http.sleeps == []  # nothing new means no second request

    def test_backfill_walks_past_known_items(self, feed_http):
        feed_http.pages.append(
            _FakeResponse(
                self._atom(["t3_new", "t3_old"]),
            )
        )

        items = load_saved(
            url=FEED_URL,
            limit=None,
            known_ids={"t3_old"},
            stop_on_known=False,
        )

        assert [i.source_id for i in items] == ["t3_new", "t3_old"]

    def test_nothing_new_returns_empty(self, feed_http):
        feed_http.pages.append(_FakeResponse(self._atom(["t3_old"])))

        items = load_saved(
            url=FEED_URL,
            limit=None,
            known_ids={"t3_old"},
            stop_on_known=True,
        )

        assert items == []


class TestFailedResponseDump:
    """A block page explains itself in HTML that will not fit in a log line."""

    def test_body_is_written_to_diagnostics_and_named_in_the_error(self, feed_http):
        feed_http.pages.append(
            _FakeResponse(
                "<body>Blocked, and here is why</body>",
                status_code=403,
            )
        )

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


# ── Poll archive ─────────────────────────────────────────────────────────────


def _poll_dirs():
    return sorted(p for p in (cfg.data_dir / "reddit").iterdir() if p.is_dir())


def _saved_log():
    path = cfg.data_dir / "reddit" / "saved.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestPollArchive:
    """Every poll is written to disk: Reddit serves no second copy of the past."""

    _atom = staticmethod(TestFeedThrottle._atom)

    def test_raw_pages_are_kept_verbatim_under_a_timestamped_directory(self, feed_http):
        full_page = self._atom([f"t3_{i:03d}" for i in range(100)])  # 100 → keep walking
        feed_http.pages.append(_FakeResponse(full_page))
        feed_http.pages.append(_FakeResponse(self._atom(["t3_last"])))

        load_saved(url=FEED_URL, limit=None)

        (poll,) = _poll_dirs()
        assert re.fullmatch(r"\d{8}T\d{6}Z", poll.name)
        pages = sorted(poll.glob("page-*.xml"))
        assert [p.name for p in pages] == ["page-01.xml", "page-02.xml"]
        assert pages[0].read_text(encoding="utf-8") == full_page

        manifest = json.loads((poll / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["pages"] == 2
        assert manifest["items"] == manifest["new"] == 101
        assert manifest["source_ids"][-1] == "t3_last"

    def test_items_land_in_the_cumulative_log(self, feed_http):
        feed_http.pages.append(_FakeResponse(ATOM_LINK_POST))

        load_saved(url=FEED_URL, limit=None)

        (record,) = _saved_log()
        item = record["item"]
        assert item["source_id"] == "t3_bbb"
        assert item["external_url"] == "https://example.com/paxos.pdf"
        assert item["permalink"].startswith("https://www.reddit.com/r/compsci/")

    def test_repolling_the_same_items_appends_nothing(self, feed_http):
        for _ in range(2):
            feed_http.pages.append(_FakeResponse(self._atom(["t3_a", "t3_b"])))
            load_saved(url=FEED_URL, limit=None)

        assert len(_saved_log()) == 2  # not four
        assert len(_poll_dirs()) == 2  # but both polls kept
        second = json.loads((_poll_dirs()[1] / "manifest.json").read_text(encoding="utf-8"))
        assert (second["new"], second["unchanged"]) == (0, 2)

    def test_an_edited_item_is_appended_and_wins_on_read(self, feed_http):
        original = self._atom(["t3_a"])
        feed_http.pages.append(_FakeResponse(original))
        load_saved(url=FEED_URL, limit=None)

        edited = original.replace(">body<", ">edited<")  # the entry was rewritten
        assert edited != original
        feed_http.pages.append(_FakeResponse(edited))
        load_saved(url=FEED_URL, limit=None)

        assert len(_saved_log()) == 2
        (item,) = reddit_archive.read_items()
        assert item["body"] == "edited"

    def test_a_walk_that_dies_keeps_the_pages_it_got(self, feed_http):
        """A partial walk is exactly when the bytes are worth having."""
        feed_http.pages.append(_FakeResponse(self._atom([f"t3_{i:03d}" for i in range(100)])))
        feed_http.pages.append(_FakeResponse("<body>Blocked</body>", status_code=403))

        with pytest.raises(RedditConnectorError):
            load_saved(url=FEED_URL, limit=None)

        (poll,) = _poll_dirs()
        assert [p.name for p in poll.glob("page-*.xml")] == ["page-01.xml"]
        manifest = json.loads((poll / "manifest.json").read_text(encoding="utf-8"))
        assert "403" in manifest["error"]
        assert len(_saved_log()) == 100

    def test_the_feed_token_never_reaches_the_archive(self, feed_http):
        feed_http.pages.append(_FakeResponse(self._atom(["t3_a"])))

        load_saved(url=FEED_URL, limit=None)

        for path in (cfg.data_dir / "reddit").rglob("*"):
            assert "abc123token" not in path.name
            if path.is_file():
                assert "abc123token" not in path.read_text(encoding="utf-8")

    def test_nothing_is_written_when_archiving_is_off(self, feed_http, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_archive_enabled", False)
        feed_http.pages.append(_FakeResponse(self._atom(["t3_a"])))

        assert load_saved(url=FEED_URL, limit=None)
        assert not (cfg.data_dir / "reddit").exists()

    def test_an_unwritable_archive_does_not_fail_the_poll(self, feed_http, monkeypatch):
        monkeypatch.setattr(
            "pathlib.Path.mkdir",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only")),
        )
        feed_http.pages.append(_FakeResponse(self._atom(["t3_a"])))

        items = load_saved(url=FEED_URL, limit=None)

        assert [i.source_id for i in items] == ["t3_a"]

    def test_a_truncated_final_line_does_not_lose_the_rest(self, feed_http):
        feed_http.pages.append(_FakeResponse(self._atom(["t3_a"])))
        load_saved(url=FEED_URL, limit=None)
        path = cfg.data_dir / "reddit" / "saved.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"item": {"source_i')  # killed mid-append

        assert [i["source_id"] for i in reddit_archive.read_items()] == ["t3_a"]


class TestLoadFromArchive:
    """The restore path: re-ingest from disk when the feed is unreachable."""

    _atom = staticmethod(TestFeedThrottle._atom)

    def test_items_come_back_without_touching_the_network(self, feed_http):
        for page in (ATOM_LINK_POST, ATOM_COMMENT):  # two polls, one item each
            feed_http.pages.append(_FakeResponse(page))
            load_saved(url=FEED_URL, limit=None)
        calls_after_poll = len(feed_http.calls)

        items = load_saved_from_archive(limit=None)

        assert len(feed_http.calls) == calls_after_poll
        assert [i.source_id for i in items] == ["t3_bbb", "t1_ccc"]
        link, comment = items
        assert link.external_url == "https://example.com/paxos.pdf"
        assert link.url_or_path == "https://example.com/paxos.pdf"
        assert comment.kind == "comment"
        assert comment.collection == "r/solarpunk"

    def test_an_empty_archive_is_not_an_error(self):
        assert load_saved_from_archive(limit=None) == []

    def test_a_record_from_an_older_field_set_is_skipped(self, feed_http):
        feed_http.pages.append(_FakeResponse(self._atom(["t3_a"])))
        load_saved(url=FEED_URL, limit=None)
        path = cfg.data_dir / "reddit" / "saved.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"item": {"source_id": "t3_z", "gone": "field"}}) + "\n")

        assert [i.source_id for i in load_saved_from_archive(limit=None)] == ["t3_a"]
