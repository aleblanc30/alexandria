"""Search-engine URL → card, with no HTTP request at all.

A bookmarked search-results page (``google.com/search?q=…``,
``youtube.com/results?search_query=…``) is not a document — the interesting
part is already in the URL's query string. This module recognizes that shape
and builds a ``FetchResult`` directly from the decoded query, so the fetch
pool never issues a request for it (no rate-limit slot, no scrape of a
JS-rendered SERP, no false ``unfetchable`` from a bot check).

See ``planning/SEARCH_URL_CARDS.md`` for the full design and rollout order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from pka.card_summary import SUMMARY_MAX_LEN, truncate_summary
from pka.config import settings as cfg
from pka.ingestion.fetch_base import FetchResult

# Room left for the " — <Engine> search" suffix (longest engine name plus
# the fixed wrapper) so the composed title still fits SUMMARY_MAX_LEN.
_QUERY_MAX_LEN = SUMMARY_MAX_LEN - 30


@dataclass(frozen=True)
class SearchEngine:
    name: str  # display name for the card, e.g. "Google"
    host: re.Pattern[str]
    paths: tuple[str, ...] | None  # exact paths; None = any path on this host
    path_re: re.Pattern[str] | None  # alternative to `paths` for non-exact shapes
    params: tuple[str, ...]  # query params to try, in order


def _engine(
    name: str,
    host: str,
    params: tuple[str, ...],
    *,
    paths: tuple[str, ...] | None = None,
    path_re: str | None = None,
) -> SearchEngine:
    return SearchEngine(
        name=name,
        host=re.compile(host, re.IGNORECASE),
        paths=paths,
        path_re=re.compile(path_re, re.IGNORECASE) if path_re else None,
        params=params,
    )


# Tier 1 — general web search engines.
_TIER_1: tuple[SearchEngine, ...] = (
    _engine("Google", r"^(www\.)?google\.[a-z.]+$", ("q",), paths=("/search",)),
    _engine("Google Scholar", r"^scholar\.google\.[a-z.]+$", ("q",), paths=("/scholar",)),
    _engine("Bing", r"^(www\.)?bing\.com$", ("q",), paths=("/search", "/images/search")),
    _engine(
        "DuckDuckGo",
        r"^(html\.|lite\.)?duckduckgo\.com$",
        ("q",),
        paths=("/", "/html", "/lite"),
    ),
    _engine("Brave", r"^search\.brave\.com$", ("q",), paths=("/search", "/images")),
    _engine("Ecosia", r"^(www\.)?ecosia\.org$", ("q",), paths=("/search", "/images")),
    _engine(
        "Startpage",
        r"^(www\.)?startpage\.com$",
        ("query", "q"),
        paths=("/sp/search", "/do/search"),
    ),
    _engine("Qwant", r"^(www\.)?qwant\.com$", ("q",), paths=("/",)),
    _engine("Yandex", r"^(www\.)?yandex\.(com|ru)$", ("text",), paths=("/search/",)),
    _engine("Baidu", r"^(www\.)?baidu\.com$", ("wd", "word"), paths=("/s",)),
)

# Tier 2 — site-scoped searches. Host patterns mirror the sibling handlers'
# predicates so a change there does not silently desync this table.
_TIER_2: tuple[SearchEngine, ...] = (
    _engine(
        "YouTube",
        r"^(?:www\.|m\.)?youtube\.com$",
        ("search_query",),
        paths=("/results",),
    ),
    _engine(
        "Reddit",
        r"^(?:www\.|old\.|np\.)?reddit\.com$",
        ("q",),
        paths=("/search", "/search/"),
        path_re=r"^/r/[^/]+/search/?$",
    ),
    _engine("Amazon", r"^([a-z0-9-]+\.)*amazon\.[a-z.]+$", ("k",), paths=("/s",)),
    _engine("GitHub", r"^(www\.)?github\.com$", ("q",), paths=("/search",)),
    _engine(
        "Stack Overflow",
        r"^(www\.)?stackoverflow\.com$",
        ("q",),
        paths=("/search",),
    ),
    _engine("PubMed", r"^pubmed\.ncbi\.nlm\.nih\.gov$", ("term",), paths=("/",)),
    _engine(
        "Wikipedia",
        r"^([a-z][\w-]*)\.(?:m\.)?wikipedia\.org$",
        ("search",),
        paths=("/wiki/Special:Search", "/w/index.php"),
    ),
)

_ENGINES: tuple[SearchEngine, ...] = _TIER_1 + _TIER_2


@dataclass(frozen=True)
class SearchQuery:
    engine: str
    query: str


def is_search_engine_host(url: str) -> bool:
    """True when the URL's host belongs to a known search engine (any path)."""
    host = (urlparse(url).hostname or "").lower()
    return any(engine.host.match(host) for engine in _ENGINES)


def _path_matches(engine: SearchEngine, path: str) -> bool:
    if engine.path_re and engine.path_re.match(path):
        return True
    if engine.paths is None:
        return True
    return path in engine.paths


def parse_search_url(url: str) -> SearchQuery | None:
    """Decode a search engine + query from ``url``, or ``None`` when it isn't one."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"

    for engine in _ENGINES:
        if not engine.host.match(host):
            continue
        if not _path_matches(engine, path):
            continue
        qs = parse_qs(parsed.query)
        for param in engine.params:
            values = qs.get(param)
            if not values:
                continue
            query = " ".join(values[0].split()).strip()
            if query:
                return SearchQuery(engine=engine.name, query=query)
        return None

    return None


def search_url_result(doc_id: int, url: str) -> FetchResult | None:
    """Build a card straight from a search URL's query — no HTTP request.

    Returns ``None`` when ``url`` is not a recognized search-results URL
    (dispatch in ``pka/ingestion/fetcher.py`` falls through to the next
    handler).
    """
    if not cfg.search_url_cards:
        return None
    parsed = parse_search_url(url)
    if parsed is None:
        return None

    query = truncate_summary(parsed.query, _QUERY_MAX_LEN)
    title = f"{query} — {parsed.engine} search"
    card_summary = f'Saved {parsed.engine} search for "{query}".'
    text = f"{query}\n\n{parsed.engine} search"

    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        None,
        "search url; card built from query, no fetch",
        title=title,
        card_summary=card_summary,
    )
