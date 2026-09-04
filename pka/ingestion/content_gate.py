"""Reject a fetch whose "content" is an interstitial rather than the page.

A consent wall, a "JavaScript is disabled" notice or a bare stylesheet is what a
scrape returns for webmail, app shells, search-results pages and any site behind
a bot check. It is real text, extracts cleanly, and so was stored as the
document: chunked, embedded into Chroma, and shown as the card.

Catching it at display time was the wrong place twice over — the meaningless
text is already in the vector store by then, and hiding the card removes the
only signal that the URL needs a handler. So the check runs at fetch time and
the page is recorded ``unfetchable`` with a reason naming the wall, which puts
it in ``fetch_log`` and in the top-unfetchable-domains panel where it can be
acted on.

The rule throughout: judge the **opening** of the extracted text. A page *about*
cookie consent discusses it in prose further down; an interstitial leads with
it. A false negative stores junk (the status quo this replaces); a false
positive costs one page that can be re-fetched, and shows up as an unfetchable
row rather than disappearing.
"""

from __future__ import annotations

import re

# How much of the extracted text is examined. A wall is short and front-loaded;
# real content that merely mentions cookies has prose before it gets there.
_HEAD_CHARS = 200

_CONSENT_OR_SCRIPT_WALL = re.compile(
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
    | enable\ cookies\ to\ continue
    | checking\ your\ browser\ before
    | client\ challenge                   # cloudflare / akamai bot check
    | (verify|confirm)\ (you\ are|that\ you\ are)\ (a\ )?human
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Extractor residue: a stylesheet or an inline script that trafilatura returned
# as if it were prose. This is what a bookmarked Google search-results page
# stored — ``a, a:link, a:visited, a:active, a:hover { color: #1a73e8 …``.
#
# The text must *open* with a selector chain running straight into its brace.
# Allowing arbitrary characters before the brace instead would swallow any
# sentence that happens to quote a rule ("…sets every state at once: a:link,
# a:visited { color: #1a73e8; }"), which is an article about CSS, not CSS.
#
# Selectors may be chained only by a combinator (``,`` ``>`` ``+`` ``~``), never
# by a bare space: measured against the real archive, allowing space-separated
# tokens rejected pages that open with a short title and only then run into a
# stylesheet ("Deep Learning\n\nDeep Learning html{ background-image…"). Those
# have a real title, and a gate that drops a document has to be the precise
# kind of wrong.
_SELECTOR = r"[.#]?[A-Za-z][\w-]*(?:[.#:][\w-]+)*(?:\[[^\]\n]{0,40}\])?"
_STYLESHEET_HEAD = re.compile(
    rf"""
    ^\s*
    (?:
        @(?:font-face|media|import|charset)\b       # an at-rule, or
      | {_SELECTOR}                                 # a selector chain,
        (?:\s*[,>+~]\s*{_SELECTOR}){{0,5}}          # combinators only,
        \s*\{{[^{{}}]*:                             # then a brace with a
    )                                               # declaration inside it
    """,
    re.IGNORECASE | re.VERBOSE,
)

# JSON-LD or a config dump extracted as prose — ``{"@context":"https://schema.org"…``,
# ``{"imports":{"jquery":…``. Structured data *about* the page, never the page.
# A few leading tokens are tolerated because the extractor usually emits the
# page title first ("NeuroML\n\n{ "@context": …"); ``{ "`` is a strong enough
# signal to carry that, in a way a bare brace would not be.
#
# ``\S+\s+`` and not ``\S+[ \t]*\n*\s*``: overlapping optional groups nested in
# a repetition backtrack exponentially, and this runs over every fetched page.
# The first version of this line hung on the real archive.
_JSON_BLOB_HEAD = re.compile(r'^\s*(?:\S+\s+){0,4}\{\s*"')
_SCRIPT_HEAD = re.compile(
    r"""
    ^\s*
    (?:
        \(?\s*function\s*\(
      | var\s+\w+\s*=\s*(?:function|\(|\{)
      | window\.\w+\s*=
      | \(adsbygoogle
      | !function\s*\(
      | \w+\s*=\s*\w+\s*\|\|\s*\+?new\s+Date
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def interstitial_reason(text: str | None) -> str | None:
    """Why ``text`` is not page content, or ``None`` when it looks like content.

    The string is written into ``fetch_log.error_msg`` and shown in the
    unfetchable lists, so it says which wall was hit — "consent or script wall"
    reads differently from "stylesheet, not content" when deciding whether the
    domain needs its own handler.
    """
    if not text or not text.strip():
        return None
    head = text[:_HEAD_CHARS]
    if _CONSENT_OR_SCRIPT_WALL.search(head):
        return "interstitial: consent or script wall"
    # Data before stylesheet: JSON-LD often follows a page title, and the
    # selector rule would otherwise claim "NeuroML\n\n{ "@context"…" as CSS and
    # write the wrong reason into fetch_log.
    if _JSON_BLOB_HEAD.match(head):
        return "interstitial: embedded data, not content"
    if _STYLESHEET_HEAD.match(head):
        return "interstitial: stylesheet, not content"
    if _SCRIPT_HEAD.match(head):
        return "interstitial: inline script, not content"
    return None
