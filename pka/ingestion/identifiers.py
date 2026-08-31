"""Shared DOI helpers, reused by every source that can populate ``documents.doi``.

See ``planning/DOCUMENT_METADATA_PLAN.md``: a source-provided DOI always wins,
and an arXiv document with no source DOI derives one from its arXiv ID
(arXiv mints ``10.48550/arXiv.<id>`` for every submission). This derivation is
reached from more than one runner (Zotero, and the arXiv fetch path), so it
lives here rather than being written out per source.
"""

import re

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_doi(raw: str | None) -> str | None:
    """Strip a ``https://doi.org/`` or ``doi:`` prefix and lowercase. DOIs are
    case-insensitive and this column is a join key."""
    if not raw:
        return None
    value = _DOI_PREFIX_RE.sub("", raw.strip()).strip()
    return value.lower() or None


def derive_arxiv_doi(arxiv_id: str) -> str:
    """The DOI arXiv mints for every submission, from its normalized arXiv id."""
    return f"10.48550/arxiv.{arxiv_id.lower()}"


def resolve_doi(source_doi: str | None, arxiv_id: str | None) -> str | None:
    """A source-provided DOI always wins; otherwise derive one from ``arxiv_id``."""
    normalized = normalize_doi(source_doi)
    if normalized:
        return normalized
    if arxiv_id:
        return derive_arxiv_doi(arxiv_id)
    return None
