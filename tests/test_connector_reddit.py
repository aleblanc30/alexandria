"""Reddit saved-posts connector — mapping, client construction, auth errors."""
from __future__ import annotations

import logging
import sys
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from pka.config import settings as cfg
from pka.connectors.reddit import RedditConnectorError, _build_client, load_saved


@pytest.fixture()
def stub_praw(monkeypatch):
    """Install a fake ``praw`` module and return the recorded Reddit() calls.

    ``_build_client`` is otherwise unreachable in a praw-free suite, which is
    exactly how a read-only-mode regression went unnoticed: every other test
    injects a ready-made client through ``load_saved(client=...)``.
    """
    calls: list[dict] = []

    class _Reddit:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.kwargs = kwargs
            self.read_only = False
            self.user = MagicMock()

    module = types.ModuleType("praw")
    module.Reddit = _Reddit
    monkeypatch.setitem(sys.modules, "praw", module)
    monkeypatch.setattr(cfg, "reddit_client_id", "cid")
    monkeypatch.setattr(cfg, "reddit_client_secret", "csecret")
    monkeypatch.setattr(cfg, "reddit_username", "")
    monkeypatch.setattr(cfg, "reddit_password", "")
    monkeypatch.setattr(cfg, "reddit_refresh_token", "")
    return calls


def test_load_saved_maps_posts_and_comments(fake_reddit_client):
    items = load_saved(client=fake_reddit_client)

    assert [i.source_id for i in items] == ["t3_selfpost", "t3_linkpost", "t1_comment1"]

    self_post, link_post, comment = items

    # Self-post: content inline, no external target.
    assert self_post.kind == "post"
    assert self_post.external_url is None
    assert self_post.body and "Raft" in self_post.body
    assert self_post.url_or_path == self_post.permalink
    assert self_post.permalink.startswith("https://www.reddit.com/")
    assert self_post.collection == "r/compsci"

    # Link post: external URL present, body empty.
    assert link_post.kind == "post"
    assert link_post.external_url == "https://example.com/paxos.pdf"
    assert link_post.body is None
    assert link_post.url_or_path == "https://example.com/paxos.pdf"

    # Comment: titled by its thread, body carried inline.
    assert comment.kind == "comment"
    assert comment.title == 'Comment on "Understanding Raft"'
    assert comment.external_url is None
    assert comment.body and "leader election" in comment.body


def test_deleted_body_becomes_none(fake_reddit_client):
    me = fake_reddit_client.user.me.return_value
    from types import SimpleNamespace

    deleted = SimpleNamespace(
        name="t1_deleted",
        body="[deleted]",
        link_title="Gone",
        permalink="/r/x/comments/gone/c/",
        subreddit="x",
        created_utc=1700000300,
    )
    me.saved.side_effect = lambda *a, **k: iter([deleted])

    (item,) = load_saved(client=fake_reddit_client)
    assert item.body is None


def test_dedupes_by_fullname(fake_reddit_client):
    me = fake_reddit_client.user.me.return_value
    from types import SimpleNamespace

    dupe = SimpleNamespace(
        name="t3_dupe", title="Once", selftext="body", is_self=True,
        url="https://reddit.com/x", permalink="/r/x/comments/dupe/",
        subreddit="x", created_utc=1,
    )
    me.saved.side_effect = lambda *a, **k: iter([dupe, dupe])

    items = load_saved(client=fake_reddit_client)
    assert len(items) == 1


def test_missing_praw_raises_helpful_error(monkeypatch):
    """When no client is injected and praw is absent, error names the fix."""
    import builtins

    real_import = builtins.__import__

    def _no_praw(name, *args, **kwargs):
        if name == "praw":
            raise ImportError("No module named 'praw'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_praw)

    with pytest.raises(RedditConnectorError, match="pip install"):
        load_saved()


class TestBuildClient:
    def test_password_auth_stays_user_authorized(self, stub_praw, monkeypatch):
        """The regression: read_only=True swaps in the app-only authorizer.

        PRAW's ``read_only`` setter replaces the script/refresh core with the
        ReadOnlyAuthorizer (client-credentials grant), after which ``user.me()``
        raises ``ReadOnlyException`` — saved posts are user-scoped and become
        unreachable no matter how valid the credentials are.
        """
        monkeypatch.setattr(cfg, "reddit_username", "alice")
        monkeypatch.setattr(cfg, "reddit_password", "hunter2")

        client = _build_client()

        assert client.read_only is False
        assert stub_praw == [{
            "client_id": "cid",
            "client_secret": "csecret",
            "user_agent": cfg.reddit_user_agent,
            "username": "alice",
            "password": "hunter2",
        }]

    def test_refresh_token_preferred_over_password(self, stub_praw, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_refresh_token", "rtoken")
        monkeypatch.setattr(cfg, "reddit_username", "alice")
        monkeypatch.setattr(cfg, "reddit_password", "hunter2")

        client = _build_client()

        assert client.read_only is False
        assert stub_praw[0]["refresh_token"] == "rtoken"
        assert "password" not in stub_praw[0]

    def test_missing_app_credentials(self, stub_praw, monkeypatch):
        monkeypatch.setattr(cfg, "reddit_client_id", "")
        with pytest.raises(RedditConnectorError, match="CLIENT_ID"):
            _build_client()

    def test_missing_user_auth(self, stub_praw):
        with pytest.raises(RedditConnectorError, match="REFRESH_TOKEN"):
            _build_client()


class TestAuthErrorTranslation:
    def test_me_failure_becomes_connector_error(self):
        client = MagicMock()
        client.user.me.side_effect = RuntimeError("401 invalid_grant")

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved(client=client)

        message = str(excinfo.value)
        assert "type 'script'" in message
        assert "RuntimeError: 401 invalid_grant" in message
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_me_returning_none_becomes_connector_error(self):
        client = MagicMock()
        client.user.me.return_value = None

        with pytest.raises(RedditConnectorError, match="user-scoped"):
            load_saved(client=client)

    def test_saved_listing_failure_is_reported(self):
        client = MagicMock()
        client.user.me.return_value.saved.side_effect = RuntimeError("403 Forbidden")

        with pytest.raises(RedditConnectorError, match="listing saved items failed"):
            load_saved(client=client)

    def test_mapping_errors_are_not_masked_as_auth_failures(self, fake_reddit_client,
                                                            monkeypatch):
        """A bug in item mapping must not be reported as an auth problem."""
        monkeypatch.setattr(
            "pka.connectors.reddit._to_saved",
            MagicMock(side_effect=ValueError("mapping bug")),
        )
        with pytest.raises(ValueError, match="mapping bug"):
            load_saved(client=fake_reddit_client)

# ── Private feed loader ──────────────────────────────────────────────────────
#
# Payload shapes below mirror a real /prefs/feeds/ response: a "Listing" whose
# children are {"kind": "t3"|"t1", "data": {...}}, with subreddit as a plain
# string, selftext/body inline, and an "after" cursor for pagination.

FEED_URL = "https://www.reddit.com/saved.rss?feed=abc123token&user=someone"


def _t3_self(fullname="t3_aaa"):
    return {"kind": "t3", "data": {
        "name": fullname, "id": fullname.split("_")[1],
        "title": "Anyone got any good leftist book suggestions",
        "selftext": "I've already read the manifesto.",
        "subreddit": "socialism", "is_self": True,
        "permalink": "/r/socialism/comments/aaa/anyone_got/",
        "url": "https://old.reddit.com/r/socialism/comments/aaa/anyone_got/",
        "created_utc": 1786989108.0,
    }}


def _t3_link(fullname="t3_bbb"):
    return {"kind": "t3", "data": {
        "name": fullname, "id": fullname.split("_")[1],
        "title": "Paxos made simple", "selftext": "",
        "subreddit": "compsci", "is_self": False,
        "permalink": "/r/compsci/comments/bbb/paxos/",
        "url": "https://example.com/paxos.pdf",
        "created_utc": 1700000100.0,
    }}


def _t1_comment(fullname="t1_ccc"):
    return {"kind": "t1", "data": {
        "name": fullname, "id": fullname.split("_")[1],
        "link_title": "What kind of problematics can fit in a Solarpunk story ?",
        "body": "Oh, that's one way of handling that.",
        "subreddit": "solarpunk",
        "permalink": "/r/solarpunk/comments/xyz/what_kind/ccc/",
        "created_utc": 1700000300.0,
    }}


def _listing(children, after=None):
    return {"kind": "Listing", "data": {"after": after, "children": children}}


class _Feed(tuple):
    """(calls, pages) for existing unpacking, with ``.sleeps`` alongside."""

    def __new__(cls, calls, pages, sleeps):
        self = super().__new__(cls, (calls, pages))
        self.calls, self.pages, self.sleeps = calls, pages, sleeps
        return self


class _FakeResponse:
    def __init__(self, payload, status_code=200, text_body=None):
        self._payload = payload
        self.status_code = status_code
        self._text_body = text_body
        self.text = text_body if text_body is not None else ""

    def json(self):
        if self._text_body is not None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


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
        return pages.pop(0) if pages else _FakeResponse(_listing([]))

    monkeypatch.setattr(httpx, "get", _get)
    return _Feed(calls, pages, sleeps)


class TestFeedLoader:
    def test_maps_posts_and_comments(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self(), _t3_link(), _t1_comment()])))

        items = load_saved_from_feed(url=FEED_URL, limit=None)

        assert [i.source_id for i in items] == ["t3_aaa", "t3_bbb", "t1_ccc"]
        self_post, link_post, comment = items
        # Feed items carry their body inline, so no fetch is owed for them.
        assert self_post.body == "I've already read the manifesto."
        assert self_post.external_url is None
        assert self_post.collection == "r/socialism"
        assert self_post.permalink.startswith("https://www.reddit.com/r/socialism/")
        assert link_post.external_url == "https://example.com/paxos.pdf"
        assert comment.kind == "comment"
        assert comment.title == 'Comment on "What kind of problematics can fit in a Solarpunk story ?"'

    def test_rss_url_is_normalised_to_json_keeping_token(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self()])))

        load_saved_from_feed(url=FEED_URL, limit=None)

        assert calls[0]["url"] == "https://www.reddit.com/saved.json"
        # The token lives in the query string; losing it in the merge would
        # silently downgrade the request to an unauthenticated one.
        assert calls[0]["params"]["feed"] == "abc123token"
        assert calls[0]["params"]["user"] == "someone"

    def test_json_url_accepted_unchanged(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self()])))

        load_saved_from_feed(url="https://www.reddit.com/saved.json?feed=tok", limit=None)

        assert calls[0]["url"] == "https://www.reddit.com/saved.json"

    def test_pages_through_after_cursor(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self("t3_p1")], after="t3_p1")))
        pages.append(_FakeResponse(_listing([_t3_self("t3_p2")], after=None)))

        items = load_saved_from_feed(url=FEED_URL, limit=None)

        assert [i.source_id for i in items] == ["t3_p1", "t3_p2"]
        assert "after" not in calls[0]["params"]
        assert calls[1]["params"]["after"] == "t3_p1"
        assert calls[1]["params"]["feed"] == "abc123token"  # token survives paging

    def test_limit_caps_results_and_page_size(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(
            _listing([_t3_self("t3_p1"), _t3_link("t3_p2")], after="t3_p2")
        ))

        items = load_saved_from_feed(url=FEED_URL, limit=2)

        assert len(items) == 2
        assert calls[0]["params"]["limit"] == "2"
        assert len(calls) == 1  # stopped without following the cursor

    def test_dedupes_across_pages(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self("t3_dupe")], after="t3_dupe")))
        pages.append(_FakeResponse(_listing([_t3_self("t3_dupe")], after=None)))

        items = load_saved_from_feed(url=FEED_URL, limit=None)

        assert [i.source_id for i in items] == ["t3_dupe"]

    def test_http_error_redacts_token(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(None, status_code=403))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved_from_feed(url=FEED_URL, limit=None)

        assert "abc123token" not in str(excinfo.value)
        assert "403" in str(excinfo.value)

    def test_non_json_body_reports_actionably(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body="<rss/>"))

        with pytest.raises(RedditConnectorError, match="did not return JSON"):
            load_saved_from_feed(url=FEED_URL, limit=None)

    def test_missing_listing_reports_rotated_token(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse({"error": 403}))

        with pytest.raises(RedditConnectorError, match="rotated"):
            load_saved_from_feed(url=FEED_URL, limit=None)

    def test_empty_url_names_the_setting(self):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_feed

        with pytest.raises(RedditConnectorError, match="SECRET_ALEXANDRIA_REDDIT_FEED_URL"):
            load_saved_from_feed(url="", limit=None)


class TestLoaderSelection:
    def test_feed_url_preferred_over_praw(self, feed_http, monkeypatch):
        """With a feed URL set, no OAuth app is needed and praw is never imported."""
        from pka.connectors.reddit import load_saved
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self()])))
        monkeypatch.setattr(cfg, "reddit_feed_url", FEED_URL)
        monkeypatch.setattr(cfg, "reddit_client_id", "")

        items = load_saved()

        assert [i.source_id for i in items] == ["t3_aaa"]
        assert calls[0]["url"] == "https://www.reddit.com/saved.json"

    def test_injected_client_wins_over_feed(self, fake_reddit_client, monkeypatch):
        from pka.connectors.reddit import load_saved
        monkeypatch.setattr(cfg, "reddit_feed_url", FEED_URL)

        items = load_saved(client=fake_reddit_client)

        assert [i.source_id for i in items] == ["t3_selfpost", "t3_linkpost", "t1_comment1"]

class TestFeedBlockMitigations:
    """old.reddit.com plus an unrecognised UA earn "403 Blocked" from Reddit."""

    def test_old_reddit_host_is_rewritten_to_www(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self()])))

        load_saved_from_feed(
            url="https://old.reddit.com/saved.json?feed=tok&user=someone", limit=None,
        )

        assert calls[0]["url"] == "https://www.reddit.com/saved.json"
        assert calls[0]["params"]["feed"] == "tok"

    def test_user_agent_carries_reddit_attribution(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self()])))

        load_saved_from_feed(url=FEED_URL, limit=None)

        assert calls[0]["headers"]["User-Agent"].endswith("(by /u/someone)")

    def test_custom_user_agent_with_attribution_is_left_alone(self, feed_http, monkeypatch):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self()])))
        monkeypatch.setattr(cfg, "reddit_user_agent", "python:mine:1.0 (by /u/other)")

        load_saved_from_feed(url=FEED_URL, limit=None)

        assert calls[0]["headers"]["User-Agent"] == "python:mine:1.0 (by /u/other)"

    def test_403_quotes_the_body_that_distinguishes_the_causes(self, feed_http):
        """Bot protection and a rejected token are both 403; only the body differs."""
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(None, status_code=403, text_body="Blocked"))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved_from_feed(url=FEED_URL, limit=None)

        message = str(excinfo.value)
        assert "Server said: Blocked" in message   # the actual evidence
        assert "bot protection" in message         # and how to read it
        assert "abc123token" not in message        # still redacted

    def test_403_with_json_body_points_at_the_token(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(
            None, status_code=403, text_body='{"message": "Forbidden", "error": 403}',
        ))

        message = str(pytest.raises(
            RedditConnectorError, load_saved_from_feed, url=FEED_URL, limit=None,
        ).value)
        assert '"error": 403' in message

    def test_long_block_page_is_truncated(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(None, status_code=403, text_body="x" * 5000))

        message = str(pytest.raises(
            RedditConnectorError, load_saved_from_feed, url=FEED_URL, limit=None,
        ).value)
        # The excerpt is cut short; the whole body goes to the saved file instead.
        assert "…" in message
        assert "x" * 500 not in message
        assert "Full response saved to" in message

    def test_httpx_url_logging_is_suppressed_during_the_request(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        calls, pages = feed_http
        pages.append(_FakeResponse(_listing([_t3_self()])))
        httpx_log = logging.getLogger("httpx")
        httpx_log.setLevel(logging.INFO)

        load_saved_from_feed(url=FEED_URL, limit=None)

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
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body=ATOM_SELF))

        (item,) = load_saved_from_rss(url=FEED_URL, limit=None)

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
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body=ATOM_LINK_POST))

        (item,) = load_saved_from_rss(url=FEED_URL, limit=None)

        assert item.external_url == "https://example.com/paxos.pdf"
        assert item.url_or_path == "https://example.com/paxos.pdf"
        # The [comments] anchor points back at reddit and must not win.
        assert "reddit.com" not in item.external_url

    def test_comment_titled_by_its_thread(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body=ATOM_COMMENT))

        (item,) = load_saved_from_rss(url=FEED_URL, limit=None)

        assert item.kind == "comment"
        assert item.title.startswith('Comment on "What kind')
        assert item.external_url is None

    def test_rss_url_input_still_hits_rss(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body=ATOM_SELF))

        load_saved_from_rss(url="https://www.reddit.com/saved.rss?feed=tok", limit=None)

        assert calls[0]["url"] == "https://www.reddit.com/saved.rss"

    def test_unparseable_xml_reports_actionably(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body="<html>nope"))

        with pytest.raises(RedditConnectorError, match="parseable Atom"):
            load_saved_from_rss(url=FEED_URL, limit=None)


class TestPrivateFeedFallback:
    def test_json_block_falls_back_to_atom(self, feed_http, monkeypatch):
        """Reddit answers saved.json with an HTML page; the Atom feed still works."""
        from pka.connectors.reddit import load_saved_from_private_feed
        calls, pages = feed_http
        monkeypatch.setattr(cfg, "reddit_feed_url", FEED_URL)
        pages.append(_FakeResponse(None, status_code=403, text_body="<body>blocked</body>"))
        pages.append(_FakeResponse(None, text_body=ATOM_SELF))

        items = load_saved_from_private_feed(limit=None)

        assert [i.source_id for i in items] == ["t3_aaa"]
        assert [c["url"] for c in calls] == [
            "https://www.reddit.com/saved.json",
            "https://www.reddit.com/saved.rss",
        ]

    def test_both_failures_are_reported(self, feed_http, monkeypatch):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_private_feed
        calls, pages = feed_http
        monkeypatch.setattr(cfg, "reddit_feed_url", FEED_URL)
        pages.append(_FakeResponse(None, status_code=403, text_body="<body>x</body>"))
        pages.append(_FakeResponse(None, status_code=429, text_body="slow down"))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved_from_private_feed(limit=None)

        message = str(excinfo.value)
        assert "JSON:" in message and "Atom:" in message
        assert "429" in message
        assert "abc123token" not in message

    def test_json_success_skips_the_fallback(self, feed_http, monkeypatch):
        from pka.connectors.reddit import load_saved_from_private_feed
        calls, pages = feed_http
        monkeypatch.setattr(cfg, "reddit_feed_url", FEED_URL)
        pages.append(_FakeResponse(_listing([_t3_self()])))

        items = load_saved_from_private_feed(limit=None)

        assert [i.source_id for i in items] == ["t3_aaa"]
        assert len(calls) == 1

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
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body=self._atom(["t3_a"])))

        load_saved_from_rss(url=FEED_URL, limit=None)

        assert calls[0]["params"]["limit"] == "100"

    def test_walks_pages_using_the_last_entry_as_cursor(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        first = [f"t3_{i:03d}" for i in range(100)]
        pages.append(_FakeResponse(None, text_body=self._atom(first)))
        pages.append(_FakeResponse(None, text_body=self._atom(["t3_last"])))

        items = load_saved_from_rss(url=FEED_URL, limit=None)

        assert len(items) == 101
        assert "after" not in calls[0]["params"]
        assert calls[1]["params"]["after"] == "t3_099"   # last id of page 1
        assert calls[1]["params"]["feed"] == "abc123token"

    def test_short_page_ends_the_walk(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body=self._atom(["t3_a", "t3_b"])))

        items = load_saved_from_rss(url=FEED_URL, limit=None)

        assert len(items) == 2
        assert len(calls) == 1   # fewer entries than asked for → no more pages

    def test_ignored_cursor_does_not_loop_forever(self, feed_http):
        """A server replaying the same page must not spin the budget."""
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        same = self._atom([f"t3_{i:03d}" for i in range(100)])
        for _ in range(5):
            pages.append(_FakeResponse(None, text_body=same))

        items = load_saved_from_rss(url=FEED_URL, limit=None)

        assert len(items) == 100     # deduped
        assert len(calls) == 2       # second page added nothing new → stop

    def test_limit_caps_page_size_and_results(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        calls, pages = feed_http
        pages.append(_FakeResponse(None, text_body=self._atom(["t3_a", "t3_b", "t3_c"])))

        items = load_saved_from_rss(url=FEED_URL, limit=2)

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
        from pka.connectors.reddit import load_saved_from_rss
        feed_http.pages.append(_FakeResponse(None, text_body=self._atom(["t3_a"])))

        load_saved_from_rss(url=FEED_URL, limit=None)

        assert feed_http.sleeps == []   # single page pays nothing

    def test_sleeps_between_pages_within_configured_bounds(self, feed_http, monkeypatch):
        from pka.connectors.reddit import load_saved_from_rss
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 1.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 0.5)
        feed_http.pages.append(_FakeResponse(
            None, text_body=self._atom([f"t3_{i:03d}" for i in range(100)]),
        ))
        feed_http.pages.append(_FakeResponse(None, text_body=self._atom(["t3_last"])))

        load_saved_from_rss(url=FEED_URL, limit=None)

        assert len(feed_http.sleeps) == 1              # one gap between two pages
        assert 1.0 <= feed_http.sleeps[0] <= 1.5       # base + jitter

    def test_jitter_varies_the_delay(self, feed_http, monkeypatch):
        """A fixed cadence is what rate limiters notice; the delay must move."""
        from pka.connectors.reddit import load_saved_from_rss
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 0.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 1.0)
        full = self._atom([f"t3_{i:03d}" for i in range(100)])
        for _ in range(4):
            feed_http.pages.append(_FakeResponse(None, text_body=full))
        # Distinct ids per page so the walk continues.
        feed_http.pages[1] = _FakeResponse(
            None, text_body=self._atom([f"t3_1{i:02d}" for i in range(100)]))
        feed_http.pages[2] = _FakeResponse(
            None, text_body=self._atom([f"t3_2{i:02d}" for i in range(100)]))
        feed_http.pages[3] = _FakeResponse(None, text_body=self._atom(["t3_end"]))

        load_saved_from_rss(url=FEED_URL, limit=None)

        assert len(set(feed_http.sleeps)) > 1

    def test_throttle_can_be_disabled(self, feed_http, monkeypatch):
        from pka.connectors.reddit import load_saved_from_rss
        monkeypatch.setattr(cfg, "reddit_feed_poll_interval_seconds", 0.0)
        monkeypatch.setattr(cfg, "reddit_feed_poll_jitter_seconds", 0.0)
        feed_http.pages.append(_FakeResponse(
            None, text_body=self._atom([f"t3_{i:03d}" for i in range(100)])))
        feed_http.pages.append(_FakeResponse(None, text_body=self._atom(["t3_last"])))

        load_saved_from_rss(url=FEED_URL, limit=None)

        assert feed_http.sleeps == []


class TestIncrementalVsBackfill:
    """Save-ordered listing: the first known id ends an incremental walk."""

    _atom = staticmethod(TestFeedThrottle._atom)

    def test_stops_at_first_known_id_mid_page(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        feed_http.pages.append(_FakeResponse(
            None, text_body=self._atom(["t3_new1", "t3_new2", "t3_old", "t3_older"]),
        ))

        items = load_saved_from_rss(
            url=FEED_URL, limit=None,
            known_ids={"t3_old", "t3_older"}, stop_on_known=True,
        )

        assert [i.source_id for i in items] == ["t3_new1", "t3_new2"]
        assert len(feed_http.calls) == 1
        assert feed_http.sleeps == []   # nothing new means no second request

    def test_backfill_walks_past_known_items(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        feed_http.pages.append(_FakeResponse(
            None, text_body=self._atom(["t3_new", "t3_old"]),
        ))

        items = load_saved_from_rss(
            url=FEED_URL, limit=None, known_ids={"t3_old"}, stop_on_known=False,
        )

        assert [i.source_id for i in items] == ["t3_new", "t3_old"]

    def test_nothing_new_returns_empty(self, feed_http):
        from pka.connectors.reddit import load_saved_from_rss
        feed_http.pages.append(_FakeResponse(None, text_body=self._atom(["t3_old"])))

        items = load_saved_from_rss(
            url=FEED_URL, limit=None, known_ids={"t3_old"}, stop_on_known=True,
        )

        assert items == []

    def test_json_loader_stops_at_first_known_id_too(self, feed_http):
        from pka.connectors.reddit import load_saved_from_feed
        feed_http.pages.append(_FakeResponse(
            _listing([_t3_self("t3_new"), _t3_link("t3_known")], after="t3_known"),
        ))

        items = load_saved_from_feed(
            url=FEED_URL, limit=None, known_ids={"t3_known"}, stop_on_known=True,
        )

        assert [i.source_id for i in items] == ["t3_new"]
        assert len(feed_http.calls) == 1

class TestFailedResponseDump:
    """A block page explains itself in HTML that will not fit in a log line."""

    def test_body_is_written_to_diagnostics_and_named_in_the_error(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_rss
        feed_http.pages.append(_FakeResponse(
            None, status_code=403, text_body="<body>Blocked, and here is why</body>",
        ))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved_from_rss(url=FEED_URL, limit=None)

        saved = sorted((cfg.data_dir / "diagnostics").glob("reddit-feed-403-*.html"))
        assert len(saved) == 1
        assert saved[0].read_text(encoding="utf-8") == "<body>Blocked, and here is why</body>"
        assert str(saved[0]) in str(excinfo.value)

    def test_token_never_reaches_the_file_or_its_name(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_rss
        feed_http.pages.append(_FakeResponse(None, status_code=403, text_body="<body>x</body>"))

        with pytest.raises(RedditConnectorError):
            load_saved_from_rss(url=FEED_URL, limit=None)

        for path in (cfg.data_dir / "diagnostics").iterdir():
            assert "abc123token" not in path.name
            assert "abc123token" not in path.read_text(encoding="utf-8")

    def test_plain_text_body_keeps_a_txt_suffix(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_rss
        feed_http.pages.append(_FakeResponse(None, status_code=403, text_body="Blocked"))

        with pytest.raises(RedditConnectorError):
            load_saved_from_rss(url=FEED_URL, limit=None)

        assert list((cfg.data_dir / "diagnostics").glob("*.txt"))

    def test_empty_body_writes_nothing(self, feed_http):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_rss
        feed_http.pages.append(_FakeResponse(None, status_code=429, text_body=""))

        with pytest.raises(RedditConnectorError) as excinfo:
            load_saved_from_rss(url=FEED_URL, limit=None)

        assert not (cfg.data_dir / "diagnostics").exists()
        assert "saved to" not in str(excinfo.value)

    def test_browser_opens_only_when_asked(self, feed_http, monkeypatch):
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_rss
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", opened.append)
        feed_http.pages.append(_FakeResponse(None, status_code=403, text_body="<body>x</body>"))

        with pytest.raises(RedditConnectorError):
            load_saved_from_rss(url=FEED_URL, limit=None)
        assert opened == []

        monkeypatch.setattr(cfg, "reddit_feed_open_failed_page", True)
        feed_http.pages.append(_FakeResponse(None, status_code=403, text_body="<body>x</body>"))
        with pytest.raises(RedditConnectorError):
            load_saved_from_rss(url=FEED_URL, limit=None)

        assert len(opened) == 1
        assert opened[0].startswith("file:")

    def test_a_broken_dump_does_not_replace_the_real_error(self, feed_http, monkeypatch):
        """The diagnostic must never eclipse the failure it exists to explain."""
        from pka.connectors.reddit import RedditConnectorError, load_saved_from_rss
        monkeypatch.setattr(
            "pathlib.Path.write_text",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
        )
        feed_http.pages.append(_FakeResponse(None, status_code=403, text_body="<body>x</body>"))

        with pytest.raises(RedditConnectorError, match="403"):
            load_saved_from_rss(url=FEED_URL, limit=None)
