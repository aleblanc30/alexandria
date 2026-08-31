"""HTTP domain extraction and frequency reporting for ingested documents."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import sqlalchemy as sa

from pka.constants import FetchStatus
from pka.db.queries import get_engine
from pka.db.schema import documents


def extract_domain(url_or_path: str | None) -> str | None:
    """Return normalized hostname for http(s) URLs, or None."""
    if not url_or_path:
        return None
    raw = url_or_path.strip()
    if not raw.lower().startswith(("http://", "https://")):
        return None
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def domain_has_fetch_handler(domain: str) -> bool:
    """True when Firefox fetch has a domain-specific handler for this host.

    Mirrors the dispatch chain in ``pka.ingestion.fetcher._fetch_one_impl`` at
    the domain level — a new handler there needs a matching predicate here or
    this report silently under-counts.
    """
    from pka.ingestion.amazon import is_amazon_host
    from pka.ingestion.arxiv import is_arxiv_url
    from pka.ingestion.biorxiv import is_biorxiv_url
    from pka.ingestion.pubmed import is_pubmed_url
    from pka.ingestion.reddit_bookmark import is_reddit_host
    from pka.ingestion.wikipedia import is_wikipedia_url
    from pka.ingestion.youtube_bookmark import is_youtube_url

    probe = f"https://{domain}/"
    return (
        is_wikipedia_url(probe)
        or is_amazon_host(probe)
        or is_arxiv_url(probe)
        or is_biorxiv_url(probe)
        or is_pubmed_url(probe)
        or is_youtube_url(probe)
        or is_reddit_host(probe)
    )


def build_domain_frequency_report(
    *,
    source: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Count documents per domain, sorted by frequency descending."""
    q = sa.select(documents.c.url_or_path, documents.c.fetch_status)
    if source is not None:
        q = q.where(documents.c.source == source)

    counts: dict[str, int] = defaultdict(int)
    status_by_domain: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    with get_engine().connect() as con:
        rows = con.execute(q).fetchall()

    for url_or_path, fetch_status in rows:
        domain = extract_domain(url_or_path)
        if not domain:
            continue
        counts[domain] += 1
        status = fetch_status or "pending"
        status_by_domain[domain][status] += 1

    report = [
        {
            "domain": domain,
            "count": count,
            "has_handler": domain_has_fetch_handler(domain),
            "by_fetch_status": dict(status_by_domain[domain]),
            "unfetchable": status_by_domain[domain].get(str(FetchStatus.UNFETCHABLE), 0),
        }
        for domain, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    if limit is not None:
        report = report[:limit]
    return report


def build_domain_top_lists(
    *,
    source: str | None = None,
    limit: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """Top domains by document count and by unfetchable count, from one scan.

    ``skipped`` documents are excluded from the rejected ranking — that status
    marks a deliberate policy outcome (non-HTML extension, Wikipedia special
    page), not a fetch failure, so it must not inflate a domain's apparent
    need for a handler.
    """
    rows = build_domain_frequency_report(source=source)
    rejected = sorted(
        (r for r in rows if r["unfetchable"] > 0),
        key=lambda r: (-r["unfetchable"], r["domain"]),
    )
    return {"top_domains": rows[:limit], "top_unfetchable": rejected[:limit]}
