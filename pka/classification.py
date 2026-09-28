"""Ingest-time document classification for general browse filters."""

from __future__ import annotations

from pka.constants import Source, TagOrigin
from pka.db.tags import sync_overlay_tags
from pka.domains import extract_domain

CLASSIFICATION_TAGS = frozenset({"academic", "paper", "preprint", "video"})

ZOTERO_PAPER_TYPES = frozenset({"journalArticle", "conferencePaper", "thesis"})
ZOTERO_PREPRINT_TYPES = frozenset({"preprint"})

PREPRINT_HOSTS = frozenset(
    {
        "arxiv.org",
        "biorxiv.org",
        "medrxiv.org",
        "ssrn.com",
        "researchsquare.com",
    }
)

PAPER_HOSTS = frozenset(
    {
        "doi.org",
        "pubmed.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
    }
)


def _classify_zotero(item_type: str | None) -> list[str]:
    if not item_type:
        return []
    if item_type in ZOTERO_PREPRINT_TYPES:
        return ["academic", "preprint"]
    if item_type in ZOTERO_PAPER_TYPES:
        return ["academic", "paper"]
    return []


def _classify_firefox_url(url_or_path: str | None) -> list[str]:
    host = extract_domain(url_or_path)
    if not host:
        return []
    if host in PREPRINT_HOSTS:
        return ["academic", "preprint"]
    if host in PAPER_HOSTS:
        return ["academic", "paper"]
    if host == "ncbi.nlm.nih.gov":
        if url_or_path and "/pmc/" in url_or_path.lower():
            return ["academic", "paper"]
    return []


def classify_document(
    source: Source | str,
    *,
    item_type: str | None = None,
    url_or_path: str | None = None,
) -> list[str]:
    """Return classification tags for a document, or [] if unclassified."""
    src = str(source)
    if src == Source.ZOTERO:
        return _classify_zotero(item_type)
    if src == Source.FIREFOX:
        return _classify_firefox_url(url_or_path)
    if src == Source.YOUTUBE:
        return ["video"]
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
    """Upsert inferred classification tags and remove stale ones.

    Scoped to :data:`CLASSIFICATION_TAGS`: other ``inferred`` tags (the image
    gate's) are not this function's to delete.
    """
    sync_overlay_tags(document_id, tags, TagOrigin.INFERRED, within=CLASSIFICATION_TAGS)
