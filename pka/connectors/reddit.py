"""
Reddit saved-posts connector — the private Atom feed, and nothing else.

Unlike the Firefox/Zotero/Calibre connectors, Reddit saved items live behind an
authenticated web endpoint rather than a local file. The credential is the
token-bearing feed URL Reddit issues from https://www.reddit.com/prefs/feeds/
(``reddit_feed_url``): no OAuth app, no client id, no approval step, one URL
fetched over plain HTTP. It belongs in ``.secrets`` and is never committed.

Whichever form of that URL is pasted, the loader asks for ``saved.rss``. The
``.json`` sibling was tried first for a while and never once succeeded — Reddit
answers automated clients with the web app's HTML — so every run paid a blocked
request, which is exactly the signal an account does not want to keep sending.

The OAuth API (``praw``) was the original route and has been removed: creating
the "script" app it needs now requires a separate API-access clearance a
personal account does not get, so the code could not be exercised at all. The
feed is the route that works. ``git log`` has the PRAW client if Reddit ever
reopens that door.

Entries become :class:`RedditSaved` via :func:`_atom_entry_to_saved`. Both saved
submissions (``t3_``) and saved comments (``t1_``) are returned.

Content handling:
  - self-posts / comments carry their body inline (``body``); the runner embeds
    it in phase 1 (no HTTP fetch owed).
  - link posts expose ``external_url``; the runner queues those for the phase-2
    fetcher (reusing the Firefox fetch machinery).
"""

from __future__ import annotations

import logging
import random
import time
import webbrowser
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pka.config import settings as cfg
from pka.connectors import reddit_archive

log = logging.getLogger(__name__)

_DELETED_BODIES = {"[deleted]", "[removed]"}


class RedditConnectorError(RuntimeError):
    """Raised when the Reddit connector cannot load saved posts."""


@dataclass
class RedditSaved:
    source_id: str  # reddit fullname, e.g. "t3_abc123" / "t1_def456"
    kind: str  # "post" | "comment"
    title: str
    permalink: str  # canonical reddit thread URL (always present)
    external_url: str | None  # off-reddit target for link posts, else None
    subreddit: str  # display name, e.g. "MachineLearning"
    body: str | None  # selftext / comment body (may be None)
    date_added: int | None  # unix seconds (item creation; Reddit exposes no "saved at")
    tags: list[str] = field(default_factory=list)  # checklist-required; unused today

    @property
    def url_or_path(self) -> str:
        """Document URL: the external target for link posts, else the permalink."""
        return self.external_url or self.permalink

    @property
    def collection(self) -> str:
        """Subreddit rendered as a folder-like collection path."""
        return f"r/{self.subreddit}" if self.subreddit else "r/"


# ── Item mapping ─────────────────────────────────────────────────────────────


def _abs_permalink(permalink: object) -> str:
    p = str(permalink or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return "https://www.reddit.com" + p


def _clean_body(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text in _DELETED_BODIES:
        return None
    return text


# ── Error reporting ──────────────────────────────────────────────────────────
#
# A failed feed request says nothing useful in its exception type alone, so
# failures are caught broadly, named, and chained rather than propagated raw.


def _describe(exc: BaseException) -> str:
    return f"Underlying error: {type(exc).__name__}: {exc}"


# -- Private feed loader (no OAuth app) ---------------------------------------

_FEED_PAGE_MAX = 100  # Reddit caps a listing page at 100 items
_FEED_PAGE_BUDGET = 20  # stop walking ``after`` eventually when limit is None


def _feed_url(raw: str) -> tuple[str, dict[str, str]]:
    """Split a /prefs/feeds/ URL into the Atom endpoint and its query parameters.

    The preferences page offers each feed as RSS and as JSON and users paste
    whichever they clicked; only the extension differs and both carry the same
    token, so either input is normalised to ``.rss`` — the only form Reddit
    actually serves to a non-browser client. The query is returned separately
    because it carries that token, which must survive being merged with the
    pagination parameters rather than replaced.
    """
    url = (raw or "").strip()
    if not url:
        raise RedditConnectorError(
            "Reddit feed URL missing. Copy the saved-links feed from "
            "https://www.reddit.com/prefs/feeds/ into "
            "SECRET_ALEXANDRIA_REDDIT_FEED_URL."
        )
    split = urlsplit(url)
    path = split.path
    for known in (".rss", ".json"):
        if path.endswith(known):
            path = path[: -len(known)]
            break
    path = path.rstrip("/") + ".rss"
    # old.reddit.com answers non-browser clients with "403 Blocked" — its bot
    # protection, not an auth failure (a bad token returns a JSON error body).
    # The feed token is account-scoped, not host-scoped, so the same URL works
    # on www. Users copying from an old-reddit session get the old host.
    netloc = split.netloc
    if netloc.lower() in {"old.reddit.com", "np.reddit.com"}:
        netloc = "www.reddit.com"
    base = urlunsplit((split.scheme or "https", netloc, path, "", ""))
    return base, dict(parse_qsl(split.query))


def _redact(url: str) -> str:
    """Feed URLs carry a bearer-equivalent token; never log or raise one intact."""
    base, sep, _ = url.partition("?")
    return f"{base}?<redacted>" if sep else base


def _feed_user_agent(params: dict[str, str]) -> str:
    """Compose a User-Agent in the format Reddit's API rules ask for.

    Reddit wants ``<platform>:<app id>:<version> (by /u/<username>)`` and serves
    "403 Blocked" to clients it does not recognise. The feed URL already names
    the account, so the attribution costs no extra setting; a user_agent that
    already carries one is left alone.
    """
    agent = cfg.reddit_user_agent
    user = params.get("user", "").strip()
    if not user or "/u/" in agent:
        return agent
    return f"{agent} (by /u/{user})"


@contextmanager
def _quiet_http_logs():
    """Stop httpx from logging the request URL — it contains the feed token.

    httpx logs "HTTP Request: GET <full url>" at INFO, which puts a credential
    in the log file and on the console. Only this request is silenced; the
    fetcher's own httpx traffic keeps its logging.
    """
    httpx_log = logging.getLogger("httpx")
    previous = httpx_log.level
    httpx_log.setLevel(logging.WARNING)
    try:
        yield
    finally:
        httpx_log.setLevel(previous)


def _save_failed_body(response, base: str) -> Path | None:
    """Write a failed feed response to ``data_dir/diagnostics`` and return its path.

    A block page explains itself in the body, and the excerpt in the error
    message is truncated to keep logs readable — so the whole thing goes to a
    file instead. Named by status and timestamp rather than overwritten, because
    the interesting question is usually what changed between two attempts.

    The URL is deliberately *not* written into the file or its name: it carries
    the feed token. Never raises — a diagnostic that breaks the error path it
    exists to explain would be worse than no diagnostic.
    """
    try:
        body = response.text or ""
    except Exception:  # pragma: no cover - undecodable body
        return None
    if not body.strip():
        return None

    try:
        directory = cfg.data_dir / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        stripped = body.lstrip()
        suffix = ".html" if stripped[:1] == "<" else ".txt"
        path = directory / f"reddit-feed-{response.status_code}-{int(time.time())}{suffix}"
        path.write_text(body, encoding="utf-8")
    except Exception as exc:
        log.warning("Could not save the failed Reddit feed response: %s", exc)
        return None

    if cfg.reddit_feed_open_failed_page:
        try:
            webbrowser.open(path.as_uri())
        except Exception as exc:  # pragma: no cover - no browser available
            log.warning("Could not open %s: %s", path, exc)
    return path


class _VisibleText(HTMLParser):
    """Text a browser would render, dropping ``<script>``/``<style>``/``<head>``.

    A block page's actual explanation ("You've been blocked by network
    security.") sits behind a wall of inlined CSS custom properties that can
    run to hundreds of KB — the first 200 characters of the raw body are all
    stylesheet, never the sentence a person would read. Distinct from
    ``_HtmlText`` above, which parses Reddit's own atom content and never
    meets those tags.
    """

    _SKIP_TAGS = {"script", "style", "head"}
    _BLOCK_TAGS = {"div", "p", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS and not self._skip_depth:
            # Adjacent <div>s carry no whitespace between them in the markup;
            # without this, sentences from separate block elements fuse into one.
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _html_to_text(html: str) -> str:
    parser = _VisibleText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - malformed block-page markup
        return ""
    return parser.text


def _body_excerpt(response, limit: int = 200) -> str:
    """First bytes of a failed response — the only way to tell the cases apart.

    Reddit's bot protection answers with the bare word "Blocked" while a
    rejected token answers with a JSON error document, and the status code is
    403 either way. An HTML body is reduced to its visible text first, since
    the raw markup opens on styling, not the message. Truncated either way
    because an HTML block page's rendered text is still long and says nothing
    more after its first line.
    """
    try:
        body = (response.text or "").strip()
    except Exception:  # pragma: no cover - a body that will not decode
        return "<unreadable>"
    if not body:
        return "<empty body>"
    if body[:1] == "<" or "<html" in body[:200].lower():
        body = _html_to_text(body) or body
    body = " ".join(body.split())
    if not body:
        return "<empty body>"
    return f"{body[:limit]}…" if len(body) > limit else body


def _diagnose_status(response) -> str:
    """Name the likely cause from the status *and* the shape of the body.

    A 403 carries three different meanings here and the status alone cannot tell
    them apart — only the body can:

    * **HTML** — Reddit served its web application, so the request never reached
      the feed's token auth. Bot protection, or the endpoint no longer honours
      feed tokens. Rotating the token changes nothing.
    * **JSON error** — the token *was* evaluated and rejected: rotated on
      /prefs/feeds/, or belonging to another account.
    * **A bare word** (``Blocked``) — the old-reddit style refusal of
      non-browser clients.
    """
    code = response.status_code
    try:
        body = (response.text or "").lstrip()
    except Exception:  # pragma: no cover - a body that will not decode
        body = ""
    looks_html = body[:1] == "<" or "<html" in body[:200].lower()
    looks_json = body[:1] in "{["

    if code == 403 and looks_html:
        return (
            "The body is HTML, so Reddit served its web page instead of the feed "
            "and the token was never evaluated — rotating it will not help. "
            "Either bot protection intercepted the request or the feed endpoint "
            "no longer honours feed tokens; check the URL still resolves in a "
            "browser and re-copy it from /prefs/feeds/ if it does not."
        )
    if code == 403 and looks_json:
        return (
            "The body is a JSON error, so the token was evaluated and rejected: "
            "it was probably rotated on /prefs/feeds/, or the URL belongs to "
            "another account."
        )
    if code == 403:
        return (
            "A bare 'Blocked' body is bot protection refusing automated clients "
            "rather than a bad token."
        )
    if code == 404:
        return "404 usually means the token was rotated or the feed was disabled."
    if code == 429:
        return "429 is rate limiting; retry later."
    return ""


def _fetch_feed_text(base: str, params: dict[str, str]) -> str:
    """GET one feed page as text, translating every failure into a connector error."""
    return _fetch_feed_response(base, params).text


def _fetch_feed_response(base: str, params: dict[str, str]):
    """GET one feed URL, raising :class:`RedditConnectorError` on any failure."""
    import httpx

    try:
        with _quiet_http_logs():
            response = httpx.get(
                base,
                params=params,
                headers={
                    "User-Agent": _feed_user_agent(params),
                    "Accept": "application/atom+xml, text/xml;q=0.9, */*;q=0.8",
                },
                timeout=cfg.fetch_timeout_seconds,
                follow_redirects=True,
            )
    except Exception as exc:
        raise RedditConnectorError(
            f"Reddit feed request to {_redact(base)} failed. {_describe(exc)}"
        ) from exc

    if response.status_code != 200:
        saved = _save_failed_body(response, base)
        where = f" Full response saved to {saved}." if saved else ""
        raise RedditConnectorError(
            f"Reddit feed returned HTTP {response.status_code} for {_redact(base)}. "
            f"{_diagnose_status(response)} Server said: {_body_excerpt(response)}"
            f"{where}"
        )
    return response


_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class _HtmlText(HTMLParser):
    """Collect text and hrefs out of an Atom entry's HTML content.

    Reddit puts the selftext (or comment body) in ``<content type="html">`` and
    appends a footer of links: "submitted by /u/x to r/y" plus, for a link post,
    an anchor whose text is ``[link]`` pointing at the off-reddit target. Both
    the text and that anchor are wanted, so this collects them in one pass
    rather than running a regex over the markup twice.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, "".join(self._anchor).strip()))
            self._href, self._anchor = None, []

    def handle_data(self, data):
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _parse_atom_content(html: str) -> tuple[str, str | None]:
    """Return (body text, external target) for one entry's HTML content."""
    parser = _HtmlText()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as exc:  # pragma: no cover - malformed entry markup
        log.warning("Reddit feed entry content did not parse: %s", exc)
        return "", None

    external = None
    for href, label in parser.links:
        # Reddit labels the off-site target "[link]"; every other anchor in the
        # footer points back at reddit itself.
        if label == "[link]" and "reddit.com" not in urlsplit(href).netloc:
            external = href
            break
    return parser.text, external


def _atom_entry_to_saved(entry) -> RedditSaved | None:
    """Map one Atom entry to :class:`RedditSaved`.

    Atom carries less than the JSON listing: there is no ``is_self`` flag and no
    ``selftext``/``body`` split, so the kind comes from the ``t3_``/``t1_``
    prefix on ``<id>`` and the external target from the ``[link]`` anchor.
    """

    def _text(name: str) -> str:
        node = entry.find(f"atom:{name}", _ATOM_NS)
        return (node.text or "").strip() if node is not None and node.text else ""

    fullname = _text("id")
    if not fullname:
        return None
    kind = "comment" if fullname.startswith("t1_") else "post"

    link_node = entry.find("atom:link", _ATOM_NS)
    permalink = _abs_permalink(link_node.get("href") if link_node is not None else "")

    category = entry.find("atom:category", _ATOM_NS)
    subreddit = (category.get("term") if category is not None else "") or ""

    content_node = entry.find("atom:content", _ATOM_NS)
    body, external = _parse_atom_content(content_node.text if content_node is not None else "")

    updated = _text("updated") or _text("published")
    date_added = None
    if updated:
        try:
            date_added = int(datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp())
        except ValueError:
            log.debug("Unparseable Atom timestamp %r", updated)

    title = _text("title")
    if kind == "comment":
        title = (
            f'Comment on "{title}"'
            if title
            else (f"Comment in r/{subreddit}" if subreddit else "Reddit comment")
        )

    return RedditSaved(
        source_id=fullname,
        kind=kind,
        title=title or "Reddit post",
        permalink=permalink,
        external_url=external,
        subreddit=subreddit,
        body=_clean_body(body),
        date_added=date_added,
    )


def _throttle_poll() -> None:
    """Pause between feed pages, with jitter.

    A backfill is the one place this connector makes many requests in a row, and
    a fixed-rate burst is what bot protection is built to notice. The jitter is
    not obfuscation — it keeps a retrying loop from synchronising into a
    metronome. Applied between pages only, never before the first request, so a
    single-page incremental sync pays nothing.
    """
    base = max(0.0, cfg.reddit_feed_poll_interval_seconds)
    jitter = max(0.0, cfg.reddit_feed_poll_jitter_seconds)
    delay = base + (random.uniform(0.0, jitter) if jitter else 0.0)
    if delay > 0:
        time.sleep(delay)


def _parse_atom_entries(xml_text: str, base: str) -> list:
    """Parse an Atom document into its entry elements."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RedditConnectorError(
            f"Reddit feed at {_redact(base)} did not return parseable Atom. {_describe(exc)}"
        ) from exc
    return root.findall("atom:entry", _ATOM_NS)


def _last_entry_fullname(entries: list) -> str:
    """The last entry's ``<id>``, which is the fullname ``after`` expects."""
    if not entries:
        return ""
    node = entries[-1].find("atom:id", _ATOM_NS)
    return (node.text or "").strip() if node is not None and node.text else ""


# ── Main loader ──────────────────────────────────────────────────────────────


def load_saved(
    limit: int | None = -1,
    url: str | None = None,
    known_ids: set[str] | None = None,
    stop_on_known: bool = False,
) -> list[RedditSaved]:
    """Load the account's saved posts and comments from the private Atom feed.

    ``limit`` defaults to :data:`settings.reddit_saved_limit` (``None`` = as
    much as Reddit will serve); ``url`` defaults to ``reddit_feed_url`` and is
    passed explicitly only by tests.

    ``saved.rss`` is the only endpoint fetched. Reddit's web app answers
    ``saved.json`` with an HTML page for automated clients, which never reaches
    the feed's auth; Atom is what feed readers use and is served accordingly.
    Atom is thinner than the JSON listing would be — a self-post and a link post
    are told apart only by the ``[link]`` anchor.

    The cursor has to be derived rather than read: Atom carries no ``after``
    field, while each entry's ``<id>`` *is* the fullname ``after`` expects, so
    the last entry of a page becomes the cursor for the next. Without an
    explicit ``limit`` Reddit serves 25; ask for the per-page maximum instead,
    or a poll silently sees a quarter of what it could.

    Two walk modes:

    * ``stop_on_known=True`` (**incremental sync**) — stop at the first entry
      whose fullname is already in ``known_ids``. The listing is ordered by
      *save* time, newest first, so everything past that entry was saved earlier
      and is already in the archive. Usually one page, no sleep, no second
      request.
    * ``stop_on_known=False`` (**backfill**) — walk to the page budget or until
      Reddit stops paging, whichever comes first.

    Note the stop signal is fullnames rather than dates on purpose: Atom's
    ``<updated>`` is when an item was *created*, not when it was saved, so an
    old post saved today sorts first while carrying an old timestamp. A date
    cutoff would end the walk on exactly the item that is new.
    """
    if limit == -1:
        limit = cfg.reddit_saved_limit

    base, params = _feed_url(url if url is not None else cfg.reddit_feed_url)

    saved: list[RedditSaved] = []
    seen: set[str] = set()
    after: str | None = None

    # Every response is written to data/reddit/<timestamp>/ before it is parsed:
    # the feed serves only the newest slice of a list held on someone else's
    # server, so a poll is not repeatable. The ``finally`` covers a walk that
    # dies halfway — those pages are the ones worth keeping.
    archive = reddit_archive.PollArchive() if cfg.reddit_archive_enabled else None
    failure: str | None = None

    try:
        for page in range(_FEED_PAGE_BUDGET):
            if page:
                _throttle_poll()

            page_params = dict(params)
            want = _FEED_PAGE_MAX if limit is None else min(limit - len(saved), _FEED_PAGE_MAX)
            if want <= 0:
                break
            page_params["limit"] = str(want)
            if after:
                page_params["after"] = after

            xml_text = _fetch_feed_text(base, page_params)
            if archive is not None:
                archive.add_page(xml_text)
            entries = _parse_atom_entries(xml_text, base)
            if not entries:
                break

            fresh = 0
            reached_known = False
            for entry in entries:
                item = _atom_entry_to_saved(entry)
                if item is None or item.source_id in seen:
                    continue
                if stop_on_known and known_ids and item.source_id in known_ids:
                    # Save-ordered listing: this item and everything after it is
                    # already in the archive, so the walk is done mid-page.
                    reached_known = True
                    break
                seen.add(item.source_id)
                saved.append(item)
                fresh += 1

            if reached_known:
                log.info(
                    "Incremental sync: reached an already-saved item on page %d; "
                    "stopping after %d new (backfill=True walks the whole feed)",
                    page + 1,
                    len(saved),
                )
                break
            # A server that ignores ``after`` would replay the same page forever.
            if fresh == 0:
                break
            after = _last_entry_fullname(entries) or None
            if after is None or len(entries) < want:
                break
    except Exception as exc:
        # Named for the manifest, then re-raised untouched. Feed URLs are
        # redacted at the point the error is built, so this carries no token.
        failure = _describe(exc)
        raise
    finally:
        if archive is not None:
            archive.finish([asdict(s) for s in saved], error=failure)

    log.info("Loaded %d Reddit saved items from the Atom feed", len(saved))
    return saved[:limit] if limit is not None else saved


def load_saved_from_archive(limit: int | None = -1) -> list[RedditSaved]:
    """Rebuild the saved list from ``data/reddit/saved.jsonl`` — no network.

    The point of archiving every poll: when the token stops working, or bot
    protection starts refusing the request, the archive still holds everything
    previous polls saw and a re-ingest can run off it. Ordering is first-seen,
    which is the order the feed served them (newest saved first) within each
    poll.
    """
    if limit == -1:
        limit = cfg.reddit_saved_limit

    items: list[RedditSaved] = []
    fields = {f for f in RedditSaved.__dataclass_fields__}
    for payload in reddit_archive.read_items():
        try:
            items.append(RedditSaved(**{k: v for k, v in payload.items() if k in fields}))
        except TypeError as exc:  # a record written by an older field set
            log.warning("Skipping unusable archived Reddit item: %s", exc)

    log.info("Loaded %d Reddit saved items from the local archive", len(items))
    return items[:limit] if limit is not None else items
