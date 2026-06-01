"""Ingest-time document classification for general browse filters."""
from __future__ import annotations

import time
from urllib.parse import urlparse

import sqlalchemy as sa

from pka.constants import Source, TagOrigin
from pka.db.queries import get_engine
from pka.db.schema import overlay_tags

CLASSIFICATION_TAGS = frozenset({"academic", "paper", "preprint"})

ZOTERO_PAPER_TYPES = frozenset({"journalArticle", "conferencePaper", "thesis"})
ZOTERO_PREPRINT_TYPES = frozenset({"preprint"})

PREPRINT_HOSTS = frozenset({
    "arxiv.org",
    "www.arxiv.org",
    "biorxiv.org",
    "www.biorxiv.org",
    "medrxiv.org",
    "www.medrxiv.org",
    "ssrn.com",
    "www.ssrn.com",
    "researchsquare.com",
    "www.researchsquare.com",
})

PAPER_HOSTS = frozenset({
    "doi.org",
    "www.doi.org",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
})


def _hostname(url_or_path: str | None) -> str | None:
    if not url_or_path:
        return None
    raw = url_or_path.strip()
    if not raw.lower().startswith(("http://", "https://")):
        return None
    try:
        host = urlparse(raw).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _classify_zotero(item_type: str | None) -> list[str]:
    if not item_type:
        return []
    if item_type in ZOTERO_PREPRINT_TYPES:
        return ["academic", "preprint"]
    if item_type in ZOTERO_PAPER_TYPES:
        return ["academic", "paper"]
    return []


def _classify_firefox_url(url_or_path: str | None) -> list[str]:
    host = _hostname(url_or_path)
    if not host:
        return []
    if host in PREPRINT_HOSTS:
        return ["academic", "preprint"]
    if host in PAPER_HOSTS:
        return ["academic", "paper"]
    if host in {"ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
        if url_or_path and "/pmc/" in url_or_path.lower():
            return ["academic", "paper"]
    return []


def classify_document(
    source: Source | str,
    *,
    item_type: str | None = None,
    url_or_path: str | None = None,
) -> list[str]:
    """Return classification tags for a document, or [] if not academic."""
    src = str(source)
    if src == Source.ZOTERO:
        return _classify_zotero(item_type)
    if src == Source.FIREFOX:
        return _classify_firefox_url(url_or_path)
    return []


def resolve_general_tag_filter(
    academic: bool,
    kinds: list[str] | None,
) -> list[str] | None:
    """Map browse UI state to API ``general_tags`` values."""
    if not academic:
        return None
    if not kinds or set(kinds) >= {"paper", "preprint"}:
        return ["academic"]
    return list(kinds)


def sync_classification_tags(document_id: int, tags: list[str]) -> None:
    """Upsert inferred classification tags and remove stale ones."""
    desired = {t for t in tags if t in CLASSIFICATION_TAGS}
    eng = get_engine()
    now = int(time.time())
    origin = str(TagOrigin.INFERRED)
    with eng.begin() as con:
        existing = {
            row[0]
            for row in con.execute(
                sa.select(overlay_tags.c.tag).where(
                    (overlay_tags.c.document_id == document_id)
                    & (overlay_tags.c.origin == origin)
                    & overlay_tags.c.tag.in_(CLASSIFICATION_TAGS)
                )
            ).fetchall()
        }
        for tag in desired - existing:
            con.execute(
                sa.text("""
                    INSERT OR IGNORE INTO overlay_tags
                        (document_id, tag, origin, confidence, created_at)
                    VALUES (:did, :tag, :origin, 1.0, :now)
                """),
                {"did": document_id, "tag": tag, "origin": origin, "now": now},
            )
        stale = existing - desired
        if stale:
            con.execute(
                overlay_tags.delete().where(
                    (overlay_tags.c.document_id == document_id)
                    & (overlay_tags.c.origin == origin)
                    & overlay_tags.c.tag.in_(stale)
                )
            )
