"""Shared DOI → metadata spine for the publisher fetch handlers.

Five of the seven handlers in ``planning/archive/PUBLISHER_FETCH_HANDLERS.md`` collapse
onto the same idea: a scholarly URL carries a resolvable identifier, and that
identifier has free structured metadata behind it, so the publisher's HTML —
paywall, cookie wall, or ``403`` — is never needed. This module is that spine;
``doi_org.py``, ``nature.py``, ``aps.py``, ``springer.py`` and
``sciencedirect.py`` are its consumers and carry only their own URL parsing.

Two endpoints, one ladder:

``GET https://doi.org/{doi}`` with ``Accept: application/vnd.citationstyles.csl+json``
    Content negotiation. Registration-agency agnostic, so a DataCite DOI
    (Zenodo dataset, figshare item) answers where a Crossref-only client 404s.
    Used only by ``doi_org.py``, where the bookmarked host *is* ``doi.org``.

``GET https://api.crossref.org/works/{doi}``
    Used by the publisher handlers, which need query support content
    negotiation does not offer — ``sciencedirect.py`` depends on it outright.

**The second rung is not optional.** Crossref abstract coverage is per-deposit,
not per-publisher: the same publishing group deposits abstracts under
``10.1038`` and none under ``10.1007``, and Elsevier deposits none at all. So
when the primary record carries no abstract, the Semantic Scholar Graph API is
asked for one — never speculatively, so the common case stays at one request.

A record with no abstract is a real, acceptable outcome, not a failure: a
metadata-only card is what ``pubmed.py`` already ships one shape of, and
treating it as unfetchable would mark half of Elsevier dead. What is *not*
acceptable is falling through to the generic GET on a miss — that reinstates
exactly the paywall scrape these handlers exist to remove.

Outbound policy (DESIGN.md §1.1): ``api.crossref.org`` and Semantic Scholar are
third parties the user did not bookmark, disclosing one derived identifier, so
they sit behind ``doi_metadata_lookup``. Content negotiation against ``doi.org``
does not — it is a request to the bookmarked host — but the Semantic Scholar
rung is gated even there.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from html import unescape
from urllib.parse import quote, unquote

import httpx

from pka.card_summary import preprint_card_summary
from pka.config import settings as cfg
from pka.ingestion.fetch_base import FetchResult, _http_timeout, _limiter
from pka.ingestion.identifiers import normalize_doi
from pka.ingestion.preprint_text import build_preprint_text

log = logging.getLogger(__name__)

_CROSSREF_WORKS = "https://api.crossref.org/works"
_DOI_NEGOTIATE = "https://doi.org"
_S2_PAPER = "https://api.semanticscholar.org/graph/v1/paper"
_S2_FIELDS = "title,abstract,year,authors,externalIds"
_CSL_ACCEPT = "application/vnd.citationstyles.csl+json"

# A DOI prefix is exactly `10.` plus 4–9 digits; the suffix is everything after
# the next slash and may itself contain slashes.
_DOI_PREFIX_SEGMENT = re.compile(r"^10\.\d{4,9}$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
# A tag must open with a letter, so a literal comparison in a title
# ("Superconductivity at T < 100 K") is not mistaken for markup and eaten.
_TAG_RE = re.compile(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?>")
_JATS_ABSTRACT_TITLE = re.compile(r"<jats:title>\s*abstract\s*</jats:title>", re.IGNORECASE)
_LEADING_ABSTRACT = re.compile(r"^abstract[\s:.–—-]*", re.IGNORECASE)


@dataclass(frozen=True)
class DoiMetadata:
    doi: str  # normalized via identifiers.normalize_doi
    title: str
    authors: list[str]  # "Given Family"
    abstract: str | None
    year: int | None
    container: str | None  # journal or book series
    type: str | None  # journal-article | book-chapter | posted-content | dataset
    arxiv_id: str | None = None  # externalIds.ArXiv, only from the S2 rung


@dataclass(frozen=True)
class DoiLookup:
    """One trip up the ladder: what came back, and which rung produced it."""

    meta: DoiMetadata | None
    http_status: int | None
    error: str | None
    primary: str | None = None  # "crossref" | "doi.org"
    abstract_from_s2: bool = False


# ── URL → DOI ────────────────────────────────────────────────────────────────


def doi_from_path(path: str, *, prefix: str | None = None) -> str | None:
    """Percent-decode, find the first ``10.dddd`` segment, take it and the rest.

    Positional rather than enumerated on purpose: it handles every publisher URL
    shape in the plan (``/article/``, ``/chapter/``, ``/content/pdf/``,
    ``/prl/abstract/``, ``/doi/``) plus shapes not yet invented, and needs no
    edit when a publisher adds a new content type.

    ``unquote`` runs **before** segmenting because
    ``/article/10.1007%2Fs11263-015-0816-y`` is a real browser-produced shape —
    without the early decode the scan sees one segment and finds no DOI at all.
    """
    decoded = unquote(path or "")
    segments = [s for s in decoded.split("/") if s]
    for index, segment in enumerate(segments):
        if not _DOI_PREFIX_SEGMENT.match(segment):
            continue
        doi = "/".join([segment, *segments[index + 1 :]]).rstrip("/")
        if doi.lower().endswith(".pdf"):
            doi = doi[:-4].rstrip("/")
        if not _DOI_RE.match(doi):
            return None  # a prefix with no suffix is not a DOI
        if prefix and not doi.lower().startswith(f"{prefix.lower()}/"):
            return None
        return doi
    return None


# ── Record parsing ───────────────────────────────────────────────────────────


def clean_text(raw: str | None) -> str | None:
    """Plain text from a Crossref string field — entities and markup removed.

    Crossref carries markup in *every* text field, not only abstracts: titles
    arrive with escaped HTML (``An Overview of &lt;i&gt;C. elegans&lt;/i&gt;
    Biology``), with MathML (``<mml:math ...>`` in APS titles), and with the
    newlines and runs of spaces of the publisher's own XML. Unescaping first is
    what turns the escaped form into markup this can then strip; a title that
    reaches ``documents.title`` unfiltered is what a reader sees on the card.
    """
    if not raw:
        return None
    text = _TAG_RE.sub(" ", unescape(raw))
    return " ".join(text.split()).strip() or None


def strip_jats(raw: str | None) -> str | None:
    """Crossref abstracts are JATS XML inside a JSON string — return plain text.

    ``<jats:p>Text…</jats:p>``, sometimes wrapped in a
    ``<jats:title>Abstract</jats:title>`` heading. Never embed the raw tags: the
    markup would be chunked and indexed alongside the prose.
    """
    if not raw:
        return None
    text = clean_text(_JATS_ABSTRACT_TITLE.sub(" ", raw)) or ""
    return _LEADING_ABSTRACT.sub("", text).strip() or None


def _first(value: object) -> str | None:
    """Crossref returns ``title`` and ``container-title`` as arrays; take ``[0]``."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _authors(raw: object) -> list[str]:
    """``[{given, family}]`` → ``["Given Family"]``, tolerating either half."""
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or " ".join(
            part for part in (entry.get("given"), entry.get("family")) if part
        )
        name = " ".join(str(name).split()).strip()
        if name:
            names.append(name)
    return names


def _year(record: dict) -> int | None:
    for key in ("issued", "published", "published-print", "published-online"):
        parts = (record.get(key) or {}).get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def parse_doi_record(record: object) -> DoiMetadata | None:
    """Parse a Crossref ``message`` or a negotiated CSL-JSON body.

    The two shapes differ only in whether ``title`` / ``container-title`` are
    arrays or bare strings, which ``_first`` absorbs.
    """
    if not isinstance(record, dict):
        return None
    title = clean_text(_first(record.get("title")))
    doi = normalize_doi(_first(record.get("DOI")) or _first(record.get("doi")))
    if not title or not doi:
        return None
    return DoiMetadata(
        doi=doi,
        title=title,
        authors=_authors(record.get("author")),
        abstract=strip_jats(
            record.get("abstract") if isinstance(record.get("abstract"), str) else None
        ),
        year=_year(record),
        container=clean_text(_first(record.get("container-title"))),
        type=_first(record.get("type")),
    )


# ── HTTP rungs ───────────────────────────────────────────────────────────────


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[object | None, int | None, str | None]:
    await _limiter.wait(url)
    try:
        resp = await client.get(
            url,
            follow_redirects=True,
            timeout=_http_timeout(),
            headers=headers,
        )
    except httpx.TimeoutException:
        return None, None, "timeout"
    except httpx.RequestError as exc:
        return None, None, str(exc)

    if resp.status_code >= 400:
        return None, resp.status_code, f"HTTP {resp.status_code}"
    try:
        return resp.json(), resp.status_code, None
    except ValueError:
        return None, resp.status_code, "invalid json response"


def _encode_doi(doi: str) -> str:
    """Percent-encode a DOI for a path segment, keeping the prefix/suffix slash.

    DOI suffixes may contain ``#``, ``?`` and spaces, which would otherwise be
    read as URL syntax rather than as part of the identifier.
    """
    return quote(doi, safe="/")


async def resolve_doi_negotiated(
    client: httpx.AsyncClient,
    doi: str,
) -> tuple[DoiMetadata | None, int | None, str | None]:
    """Ask ``doi.org`` itself for CSL-JSON — agency agnostic, one request."""
    url = f"{_DOI_NEGOTIATE}/{_encode_doi(doi)}"
    data, status, err = await _get_json(client, url, headers={"Accept": _CSL_ACCEPT})
    if data is None:
        return None, status, err
    meta = parse_doi_record(data)
    if meta is None:
        return None, status, "doi.org returned no usable record"
    return meta, status, None


async def fetch_crossref_work_item(
    client: httpx.AsyncClient,
    doi: str,
) -> tuple[dict | None, int | None, str | None]:
    """The single-work Crossref endpoint, returning the **raw** ``message``.

    Kept separate from :func:`fetch_crossref_work` because a caller that
    *derived* the DOI rather than reading it verbatim wants to check the record
    against something it already knew — and ``DoiMetadata`` deliberately drops
    the bibliographic coordinates (volume, issue, page) that make that possible.
    """
    url = f"{_CROSSREF_WORKS}/{_encode_doi(doi)}"
    data, status, err = await _get_json(client, url)
    if data is None:
        return None, status, err
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return None, status, "crossref returned no usable record"
    return message, status, None


async def fetch_crossref_work(
    client: httpx.AsyncClient,
    doi: str,
) -> tuple[DoiMetadata | None, int | None, str | None]:
    """The single-work Crossref endpoint."""
    message, status, err = await fetch_crossref_work_item(client, doi)
    if message is None:
        return None, status, err
    meta = parse_doi_record(message)
    if meta is None:
        return None, status, "crossref returned no usable record"
    return meta, status, None


async def fetch_crossref_alternative_id(
    client: httpx.AsyncClient,
    alternative_id: str,
) -> tuple[DoiMetadata | None, int | None, str | None]:
    """Resolve a publisher-local identifier (an Elsevier PII) to a full record.

    Selects the whole record rather than only the DOI, so the common
    ScienceDirect case is one request here instead of two (§8.3 of the plan).
    """
    url = (
        f"{_CROSSREF_WORKS}?filter=alternative-id:{quote(alternative_id, safe='')}"
        "&rows=1&select=DOI,title,author,issued,container-title,type,abstract"
    )
    data, status, err = await _get_json(client, url)
    if data is None:
        return None, status, err
    message = data.get("message") if isinstance(data, dict) else None
    items = message.get("items") if isinstance(message, dict) else None
    record = items[0] if isinstance(items, list) and items else None
    meta = parse_doi_record(record)
    if meta is None:
        return None, status, f"no crossref work for alternative-id {alternative_id}"
    return meta, status, None


async def fetch_semantic_scholar(
    client: httpx.AsyncClient,
    doi: str,
) -> tuple[str | None, str | None]:
    """Second rung: ``(abstract, arxiv_id)`` for a DOI, both possibly ``None``.

    Called only when the primary record has no abstract. A miss here is not an
    error — it leaves a metadata-only card, which is a valid outcome.
    """
    url = f"{_S2_PAPER}/DOI:{_encode_doi(doi)}?fields={_S2_FIELDS}"
    data, _status, err = await _get_json(client, url)
    if err:
        log.debug("Semantic Scholar lookup failed for %s: %s", doi, err)
    if not isinstance(data, dict):
        return None, None
    abstract = data.get("abstract")
    abstract = " ".join(abstract.split()).strip() if isinstance(abstract, str) else None
    external = data.get("externalIds")
    arxiv_id = None
    if isinstance(external, dict):
        raw = external.get("ArXiv")
        arxiv_id = str(raw).strip() or None if raw else None
    return abstract or None, arxiv_id


# ── The ladder ───────────────────────────────────────────────────────────────


async def fetch_doi_metadata(
    client: httpx.AsyncClient,
    doi: str,
    *,
    negotiated: bool = False,
) -> DoiLookup:
    """Primary record, then Semantic Scholar only if it carried no abstract.

    ``negotiated=True`` picks the ``doi.org`` content-negotiation rung, which is
    a request to the bookmarked host and therefore needs no flag; the Crossref
    primary is an enrichment lookup and does. The Semantic Scholar rung is gated
    either way.
    """
    if not negotiated and not cfg.doi_metadata_lookup:
        return DoiLookup(None, None, "doi metadata lookup disabled (doi_metadata_lookup)")

    primary = "doi.org" if negotiated else "crossref"
    if negotiated:
        meta, status, err = await resolve_doi_negotiated(client, doi)
    else:
        meta, status, err = await fetch_crossref_work(client, doi)
    if meta is None:
        return DoiLookup(None, status, err, primary=primary)

    if meta.abstract or not cfg.doi_metadata_lookup:
        return DoiLookup(meta, status, None, primary=primary)

    abstract, arxiv_id = await fetch_semantic_scholar(client, meta.doi)
    if not abstract and not arxiv_id:
        return DoiLookup(meta, status, None, primary=primary)
    enriched = DoiMetadata(
        doi=meta.doi,
        title=meta.title,
        authors=meta.authors,
        abstract=abstract or meta.abstract,
        year=meta.year,
        container=meta.container,
        type=meta.type,
        arxiv_id=arxiv_id,
    )
    return DoiLookup(enriched, status, None, primary=primary, abstract_from_s2=bool(abstract))


# ── FetchResult assembly ─────────────────────────────────────────────────────


def doi_result(doc_id: int, url: str, lookup: DoiLookup, *, via: str) -> FetchResult:
    """Turn a completed lookup into the handler's ``FetchResult``.

    ``item_type`` is deliberately not written from ``DoiMetadata.type``: that
    column has its own per-source vocabularies today (Zotero, Reddit) and adding
    a third writer is a change to the column's meaning, not a handler detail.
    """
    meta = lookup.meta
    if meta is None:
        return FetchResult(
            doc_id,
            url,
            "unfetchable",
            None,
            lookup.http_status,
            lookup.error or "doi lookup failed",
        )

    text = build_preprint_text(
        title=meta.title,
        authors=meta.authors,
        abstract=meta.abstract or "",
        pdf_text=None,
    )
    msg = f"fetched via {via} → {lookup.primary or 'crossref'}"
    if lookup.abstract_from_s2:
        msg += " (abstract from semantic scholar)"
    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        lookup.http_status,
        msg,
        title=meta.title,
        card_summary=preprint_card_summary(meta.abstract),
        doi=meta.doi,
        arxiv_id=meta.arxiv_id,
        year=meta.year,
        authors_json=json.dumps(meta.authors) if meta.authors else None,
    )


async def fetch_doi_card(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
    doi: str,
    *,
    via: str,
    negotiated: bool = False,
) -> FetchResult:
    """The whole publisher-handler body: look the DOI up, build the card."""
    lookup = await fetch_doi_metadata(client, doi, negotiated=negotiated)
    return doi_result(doc_id, url, lookup, via=via)


async def fetch_crossref_bibliographic(
    client: httpx.AsyncClient,
    query: str,
    *,
    prefix: str | None = None,
    rows: int = 5,
) -> tuple[list[dict], int | None, str | None]:
    """Search Crossref by citation text. Returns **raw, unverified** items.

    Deliberately returns candidates rather than a ``DoiMetadata``: a
    bibliographic query is a ranked guess, and accepting rank 1 unverified is
    how the wrong paper's abstract gets attached to a document — which shifts
    ``doc_embedding`` and makes it findable under the wrong queries
    (``openlibrary.py``'s argument, and why ``researchgate.py`` refuses this
    route). The caller must round-trip the hit against something the URL
    already told it. ``prefix`` scopes the search to one publisher, which is
    cheaper and more reliable than carrying a journal-to-ISSN table.
    """
    url = (
        f"{_CROSSREF_WORKS}?query.bibliographic={quote(query, safe='')}"
        f"&rows={rows}"
        "&select=DOI,title,author,issued,container-title,type,abstract,volume,issue,page"
    )
    if prefix:
        url += f"&filter=prefix:{quote(prefix, safe='')}"
    data, status, err = await _get_json(client, url)
    if data is None:
        return [], status, err
    message = data.get("message") if isinstance(data, dict) else None
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        return [], status, "crossref returned no items"
    return [i for i in items if isinstance(i, dict)], status, None


async def enrich_abstract(
    client: httpx.AsyncClient,
    meta: DoiMetadata,
    http_status: int | None,
    *,
    primary: str = "crossref",
) -> DoiLookup:
    """Climb to Semantic Scholar when ``meta`` carries no abstract.

    The tail of :func:`fetch_doi_metadata`, split out for the handlers that
    obtain their primary record some other way (a PII filter, a bibliographic
    query) but still need the same second rung.
    """
    if meta.abstract or not cfg.doi_metadata_lookup:
        return DoiLookup(meta, http_status, None, primary=primary)
    abstract, arxiv_id = await fetch_semantic_scholar(client, meta.doi)
    if not abstract and not arxiv_id:
        return DoiLookup(meta, http_status, None, primary=primary)
    return DoiLookup(
        replace(meta, abstract=abstract or meta.abstract, arxiv_id=arxiv_id),
        http_status,
        None,
        primary=primary,
        abstract_from_s2=bool(abstract),
    )
