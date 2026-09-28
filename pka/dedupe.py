"""Find documents that are the same work saved twice (DESIGN.md §3.9).

Two passes, both local, with no network and no model call beyond the stored
document vectors:

- **Exact keys** link on sight: the same DOI (an arXiv id and its derived DOI
  count as one), the same arXiv id, the same ISBN once both are ISBN-13, or
  the same URL after :func:`canonical_url`. A group of three or more is linked
  to one canonical row, never as a chain.
- **Embeddings** only propose: two documents whose ``doc_embedding`` vectors
  reach ``dedupe_similarity`` become a candidate for review. A page saved
  under two unrelated URLs is found this way, and so, wrongly, are the papers
  of one series, which is why nothing here links without the user.

A pair already linked, proposed or rejected is left as it is.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import numpy as np
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.db import duplicates, engine
from pka.db.schema import chunks, documents
from pka.ingestion.identifiers import resolve_doi
from pka.ingestion.openlibrary import normalize_isbn

log = logging.getLogger(__name__)

# ── Keys ─────────────────────────────────────────────────────────────────────

# Query parameters that identify a campaign or a click, never the page.
_TRACKING = frozenset(
    {
        "fbclid",
        "gclid",
        "yclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "si",
        "ref",
        "ref_src",
        "_hsenc",
        "_hsmi",
    }
)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_AMAZON_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*amazon\.[a-z.]+$")
_AMAZON_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
_REDDIT_THREAD = re.compile(r"^/r/([^/]+)/comments/([a-z0-9]+)", re.IGNORECASE)


def _youtube(host: str, path: str, query: dict[str, str]) -> str | None:
    video = None
    if host == "youtu.be":
        video = path.strip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if path == "/watch":
            video = query.get("v")
        elif path.startswith(("/shorts/", "/embed/", "/live/")):
            video = path.split("/")[2] if path.count("/") >= 2 else None
    if video and _YOUTUBE_ID.match(video):
        return f"https://youtube.com/watch?v={video}"
    return None


def _reddit(host: str, path: str) -> str | None:
    if not (host == "reddit.com" or host.endswith(".reddit.com")):
        return None
    m = _REDDIT_THREAD.match(path)
    if m:
        return f"https://reddit.com/r/{m.group(1).lower()}/comments/{m.group(2).lower()}"
    return None


def _amazon(host: str, path: str) -> str | None:
    if not _AMAZON_HOST.match(host):
        return None
    m = _AMAZON_ASIN.search(path)
    # The TLD stays: amazon.fr and amazon.com are different listings.
    return f"https://{host}/dp/{m.group(1).upper()}" if m else None


def canonical_url(url: str | None) -> str | None:
    """A matching key for *url*: equal for two spellings of one page.

    For comparison only, never fetched: a canonical form can 404 where the
    original works. ``None`` for anything that is not http(s).
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in _DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    for prefix in ("www.", "m.", "old.", "new.", "np."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))

    special = _youtube(host, path, query) or _reddit(host, path) or _amazon(host, path)
    if special:
        return special

    if port is not None and port != _DEFAULT_PORTS[parts.scheme.lower()]:
        host = f"{host}:{port}"
    path = path.rstrip("/")
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    )
    return f"https://{host}{path}" + (f"?{urlencode(kept)}" if kept else "")


def isbn13(raw: object) -> str | None:
    """*raw* as an ISBN-13, converting an ISBN-10; for matching only."""
    value = normalize_isbn(raw)
    if value is None or len(value) == 13:
        return value
    core = "978" + value[:9]
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
    return core + str((10 - total % 10) % 10)


# ── Scan ─────────────────────────────────────────────────────────────────────

_STATUS_RANK = {
    "fetched": 0,
    "available": 0,
    "no_text_layer": 1,
    "pending": 2,
    "skipped": 2,
    "unfetchable": 3,
    "missing": 3,
}


def _rank(doc: sa.Row) -> tuple:
    """Order within a group: the row with the most content is canonical.

    Deterministic, so a re-scan never swaps a pair's canonical. Deliberately not
    a source preference: whichever row carries the text serves the read path.
    """
    return (
        _STATUS_RANK.get(doc.fetch_status or "", 2),
        -(doc.n_chunks or 0),
        doc.ingested_at if doc.ingested_at is not None else float("inf"),
        doc.id,
    )


def _load_documents(con: sa.Connection) -> list[sa.Row]:
    n_chunks = (
        sa.select(sa.func.count(chunks.c.id))
        .where(chunks.c.document_id == documents.c.id)
        .scalar_subquery()
    )
    return list(
        con.execute(
            sa.select(
                documents.c.id,
                documents.c.source,
                documents.c.doi,
                documents.c.arxiv_id,
                documents.c.isbn,
                documents.c.url_or_path,
                documents.c.fetch_status,
                documents.c.ingested_at,
                n_chunks.label("n_chunks"),
            )
        )
    )


def _keys(doc: sa.Row) -> list[tuple[str, str]]:
    out = []
    doi = resolve_doi(doc.doi, doc.arxiv_id)
    if doi:
        out.append(("doi", doi))
    if doc.arxiv_id:
        out.append(("arxiv_id", doc.arxiv_id.lower()))
    isbn = isbn13(doc.isbn)
    if isbn:
        out.append(("isbn", isbn))
    url = canonical_url(doc.url_or_path)
    if url:
        out.append(("url", url))
    return out


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def exact_links(con: sa.Connection) -> list[dict[str, Any]]:
    """The links the exact keys call for, one per non-canonical group member."""
    docs = {d.id: d for d in _load_documents(con)}
    merged = duplicates.canonical_map(con)
    rejected = {
        frozenset((r[0], r[1]))
        for r in con.execute(
            sa.select(duplicates.dd.c.canonical_id, duplicates.dd.c.duplicate_id).where(
                duplicates.dd.c.state == "rejected"
            )
        )
    }

    buckets: dict[tuple[str, str], set[int]] = {}
    for doc in docs.values():
        node = merged.get(doc.id, doc.id)
        for key in _keys(doc):
            buckets.setdefault(key, set()).add(node)

    uf = _UnionFind()
    edge_key: dict[int, tuple[str, str]] = {}
    for key, nodes in buckets.items():
        if len(nodes) < 2:
            continue
        first, *rest = sorted(nodes)
        for n in rest:
            uf.union(first, n)
            edge_key.setdefault(n, key)
            edge_key.setdefault(first, key)

    groups: dict[int, list[int]] = {}
    for node in uf.parent:
        groups.setdefault(uf.find(node), []).append(node)

    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        canonical = min(members, key=lambda i: _rank(docs[i]))
        for m in sorted(members):
            if m == canonical or frozenset((canonical, m)) in rejected:
                continue
            match_key, match_value = edge_key.get(m) or edge_key[canonical]
            out.append(
                {
                    "canonical_id": canonical,
                    "duplicate_id": m,
                    "match_key": match_key,
                    "match_value": match_value,
                }
            )
    return out


def embedding_candidates(
    con: sa.Connection, threshold: float, limit: int, *, block: int = 1024
) -> list[dict[str, Any]]:
    """Pairs of unlinked documents whose vectors reach *threshold*, best first."""
    from pka.clustering.doc_embeddings import blob_to_embedding

    merged = duplicates.merged_duplicate_ids(con)
    docs = {d.id: d for d in _load_documents(con)}
    rows = [
        (r[0], r[1])
        for r in con.execute(
            sa.select(documents.c.id, documents.c.doc_embedding).where(
                documents.c.doc_embedding.isnot(None)
            )
        )
        if r[0] not in merged
    ]
    if len(rows) < 2:
        return []
    ids = [r[0] for r in rows]
    matrix = np.stack([blob_to_embedding(r[1]) for r in rows]).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    unit = matrix / np.where(norms == 0, 1, norms)
    known = duplicates.known_pairs(con)

    found: list[tuple[float, int, int]] = []
    for start in range(0, len(ids), block):
        sims = unit[start : start + block] @ unit.T
        rs, cs = np.nonzero(sims >= threshold)
        for r, c in zip(rs.tolist(), cs.tolist(), strict=True):
            i = start + r
            if c <= i or frozenset((ids[i], ids[c])) in known:
                continue
            found.append((float(sims[r, c]), ids[i], ids[c]))
    found.sort(reverse=True)

    out = []
    for score, a, b in found[:limit]:
        canonical, dup = sorted((a, b), key=lambda i: _rank(docs[i]))
        out.append(
            {
                "canonical_id": canonical,
                "duplicate_id": dup,
                "match_key": "embedding",
                "match_value": None,
                "score": round(score, 4),
            }
        )
    return out


def scan(
    *,
    embeddings: bool = True,
    threshold: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Link exact duplicates and propose near ones; returns counts and the pairs.

    With ``dry_run`` nothing is written; the embedding pass then does not see
    the links the exact pass would have made.
    """
    eng = engine.get_engine()
    with eng.begin() as con:
        links = exact_links(con)
        if not dry_run:
            for row in links:
                duplicates.link(
                    row["canonical_id"],
                    row["duplicate_id"],
                    match_key=row["match_key"],
                    match_value=row["match_value"],
                    decided_by="scan",
                    con=con,
                )
    candidates: list[dict[str, Any]] = []
    if embeddings:
        with eng.connect() as con:
            candidates = embedding_candidates(
                con,
                cfg.dedupe_similarity if threshold is None else threshold,
                cfg.dedupe_max_candidates,
            )
        if not dry_run:
            duplicates.add_candidates(candidates)
    by_key = Counter(r["match_key"] for r in links)
    log.info("Duplicate scan: %d linked %s, %d proposed", len(links), dict(by_key), len(candidates))
    return {
        "linked": len(links),
        "by_key": dict(by_key),
        "proposed": len(candidates),
        "dry_run": dry_run,
        "links": links,
        "candidates": candidates,
    }


def describe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Link rows with both documents' title, source and URL, for review."""
    ids = {r[k] for r in rows for k in ("canonical_id", "duplicate_id")}
    if not ids:
        return []
    with engine.get_engine().connect() as con:
        docs = {
            d.id: {
                "id": d.id,
                "source": d.source,
                "title": d.title or "",
                "url_or_path": d.url_or_path,
            }
            for d in con.execute(
                sa.select(
                    documents.c.id, documents.c.source, documents.c.title, documents.c.url_or_path
                ).where(documents.c.id.in_(sorted(ids)))
            )
        }
    return [
        {**r, "canonical": docs.get(r["canonical_id"]), "duplicate": docs.get(r["duplicate_id"])}
        for r in rows
        if r["canonical_id"] in docs and r["duplicate_id"] in docs
    ]
