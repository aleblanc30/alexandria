"""Card summary text for browse/search cards."""

from __future__ import annotations

SUMMARY_MAX_LEN = 280


def truncate_summary(text: str | None, max_len: int = SUMMARY_MAX_LEN) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[:max_len].rstrip() + "…"


def body_excerpt(text: str, max_lines: int = 3, max_len: int = SUMMARY_MAX_LEN) -> str:
    """First non-empty lines of fetched body text, collapsed for card display."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        excerpt = " ".join(lines[:max_lines])
    else:
        excerpt = text
    return truncate_summary(excerpt, max_len)


def preprint_card_summary(abstract: str | None) -> str | None:
    """Abstract text for browse cards on arXiv / bioRxiv papers."""
    raw = (abstract or "").strip()
    return raw or None
