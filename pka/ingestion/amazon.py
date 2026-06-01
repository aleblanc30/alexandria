"""Amazon book product page extraction for Firefox bookmark URLs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

_AMAZON_HOST = re.compile(r"^([a-z0-9-]+\.)*amazon\.[a-z.]+$", re.IGNORECASE)
_ASIN_PATH = re.compile(
    r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)",
    re.IGNORECASE,
)
_TITLE_IDS = ("productTitle",)
_SUMMARY_IDS = ("bookDescription_feature_div", "productDescription")
_META_DESC = re.compile(
    r'<meta\s+[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)
_META_DESC_ALT = re.compile(
    r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']description["\']',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class AmazonBook:
    title: str
    summary: str


def is_amazon_host(url: str) -> bool:
    """True when the URL host is an Amazon domain (any TLD)."""
    host = (urlparse(url).hostname or "").lower()
    return bool(_AMAZON_HOST.match(host))


def is_amazon_book_url(url: str) -> bool:
    """True for Amazon product pages with a 10-character ASIN in the path."""
    if not is_amazon_host(url):
        return False
    path = urlparse(url).path
    return _ASIN_PATH.search(path) is not None


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split()).strip()


class _ElementTextExtractor(HTMLParser):
    """Collect direct and nested text for the first element matching ``target_id``."""

    def __init__(self, target_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self._target_id = target_id
        self._capture_depth = 0
        self._parts: list[str] = []
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        if self._capture_depth == 0 and attr_map.get("id") == self._target_id:
            self._capture_depth = 1
            self.found = True
            return
        if self._capture_depth > 0:
            self._capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._capture_depth > 0:
            self._capture_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_depth > 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return _collapse_whitespace(" ".join(self._parts))


def _text_by_id(html: str, element_id: str) -> str | None:
    parser = _ElementTextExtractor(element_id)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    if not parser.found:
        return None
    text = parser.text()
    return text or None


def _meta_description(html: str) -> str | None:
    for pattern in (_META_DESC, _META_DESC_ALT):
        match = pattern.search(html)
        if match:
            text = _collapse_whitespace(unescape(match.group(1)))
            if text:
                return text
    return None


def _first_summary(html: str) -> str | None:
    for element_id in _SUMMARY_IDS:
        text = _text_by_id(html, element_id)
        if text:
            return text
    return _meta_description(html)


def extract_amazon_book(html: str) -> AmazonBook | None:
    """Return book title and editorial summary from Amazon product HTML, or ``None``."""
    title = None
    for element_id in _TITLE_IDS:
        title = _text_by_id(html, element_id)
        if title:
            break

    summary = _first_summary(html)
    if not title or not summary:
        return None

    return AmazonBook(title=title, summary=summary)
