"""Card summary text for browse/search cards.

Two shapes of junk reach a card and neither is the writer's fault, so both are
cleaned here rather than at each of the dozen call sites:

- **Markup.** Abstracts arrive as JATS or HTML from Crossref, Zotero and
  Calibre — ``<h3>Abstract</h3> <p>We report…``. ``doi_meta.strip_jats`` cleans
  the Crossref rung, but a Zotero ``abstractNote`` is copied verbatim, so
  ``truncate_summary`` strips tags for everyone. Because every card path (write
  *and* read, via ``queries.resolve_description``) goes through it, this also
  fixes rows already stored, with no re-ingestion.
- **Interstitials.** A consent wall or a "JavaScript is disabled" page is what a
  scrape returns for YouTube channel pages, webmail and app shells. It is real
  page text, so nothing upstream rejects it; ``looks_like_boilerplate`` marks it
  so a card can show nothing rather than something wrong.
"""

from __future__ import annotations

import html
import re

SUMMARY_MAX_LEN = 280

# ``<p>``, ``</jats:title>``, ``<a href="…">`` — anything opening with a letter
# or a slash. A bare ``<`` in prose ("x < y", "<3") does not match, so
# comparisons and emoticons survive.
_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")

# A leading ``<h3>Abstract</h3>`` / ``<jats:title>Abstract</jats:title>`` heading:
# stripping the tags alone would leave the word "Abstract" glued to the first
# sentence. Matched as a whole element rather than as a leading word, so a
# summary that genuinely opens with "Abstract" keeps it.
_ABSTRACT_HEADING_RE = re.compile(
    r"^\s*<([a-zA-Z][\w:.-]*)[^>]*>\s*abstract\s*</\1>\s*",
    re.IGNORECASE,
)

# Checked against the opening of a summary only: a page *about* cookie consent
# says so in its prose, an interstitial leads with it.
_BOILERPLATE_HEAD_CHARS = 200
_BOILERPLATE_RE = re.compile(
    r"""
    javascript\ (is\ |must\ )
    | enable\ javascript
    | requires\ javascript
    | (does\ not|doesn't)\ support\ javascript
    | you\ need\ to\ enable\ javascript
    | before\ you\ continue
    | bevor\ sie\ zu                      # google/youtube consent wall, de
    | vor\ der\ weiterleitung
    | we\ use\ cookies\ and\ data
    | wir\ verwenden\ cookies             # …and its german twin
    | accept\ all\ cookies
    | checking\ your\ browser\ before
    | (verify|confirm)\ (you\ are|that\ you\ are)\ (a\ )?human
    | enable\ cookies\ to\ continue
    """,
    re.IGNORECASE | re.VERBOSE,
)


def clean_summary_text(text: str) -> str:
    """Strip markup and unescape entities, leaving readable prose.

    Tags go before entities are unescaped, so text an author escaped on purpose
    (``&lt;p&gt;`` written to *show* a tag) survives as visible characters
    instead of becoming a tag this then removes.
    """
    return html.unescape(_TAG_RE.sub(" ", _ABSTRACT_HEADING_RE.sub("", text)))


def looks_like_boilerplate(text: str | None) -> bool:
    """True when ``text`` is a consent wall or a "turn on JavaScript" notice.

    Only the opening of the text is examined — see ``_BOILERPLATE_HEAD_CHARS``.
    A false positive costs a card its summary; a false negative shows the wall,
    which is what this exists to stop.
    """
    if not text:
        return False
    return _BOILERPLATE_RE.search(text[:_BOILERPLATE_HEAD_CHARS]) is not None


def truncate_summary(text: str | None, max_len: int = SUMMARY_MAX_LEN) -> str:
    if not text:
        return ""
    collapsed = " ".join(clean_summary_text(text).split())
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
