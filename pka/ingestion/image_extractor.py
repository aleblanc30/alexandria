"""
Four extraction passes for each image:

  1. classify()     — vision LLM (llava/moondream/remote) → image_type label
  2. content pass   — vision LLM → per-type extraction (transcript / poster
                      summary / structured book fields) plus a prose description
                      of the artifact itself
  3. ocr()          — OCR provider (VLM/EasyOCR) → raw text
  4. clip_embed()   — image-embed provider (CLIP) → float vector

All passes are independent and can be skipped selectively. Every pass delegates
to the backend selected in ``pka/providers/`` (Ollama/OpenRouter/OVH/Scaleway for
vision, VLM or EasyOCR for OCR, CLIP for embeddings); this module owns only the
prompts, image encoding, and JSON-salvage logic.

Pass 2 is prompt-per-category (``DESIGN.md`` §3.2): a *generic* "describe what
you see" prompt encodes a whiteboard as "a whiteboard with diagrams", which has
near-zero retrieval value. :func:`extract_image_content` therefore picks the
prompt from the image's category — the label the admission gate already resolved,
so the better prompt costs no extra model call.
"""

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pka.json_utils import parse_llm_json as _parse_llm_json
from pka.providers import get_image_embedder, get_ocr_provider, get_vision_provider

if TYPE_CHECKING:
    from pka.providers.base import VisionProvider

log = logging.getLogger(__name__)

# Valid image type labels the LLM must choose from
_VALID_TYPES = {
    "book_cover",
    "multiple_book_covers",
    "bookshelf",
    "slide",
    "poster",
    "notes",
    "whiteboard",
    "unknown",
}

_CLASSIFY_PROMPT = """You are analysing an image from a personal research archive.

Return ONLY a JSON object with exactly these two keys:
{
  "image_type": "<one of: book_cover, multiple_book_covers, bookshelf, slide, \
poster, notes, whiteboard, unknown>",
  "description": "<2-4 sentence description of the image content>"
}

Rules:
- book_cover            : a photograph or scan of ONE book/report/thesis cover
- multiple_book_covers  : two or more distinct book covers facing the camera
                          (a stack, a table display, a grid of covers)
- bookshelf             : books shelved or stacked so mostly SPINES are visible —
                          a shelf, bookcase, or library section
- slide                 : a presentation slide (PowerPoint, Keynote, Beamer, etc.)
- poster                : an academic/conference poster or article figure
- notes                 : handwritten or typed notes, sticky notes, notebook pages
- whiteboard            : a whiteboard or blackboard with writing or diagrams
- unknown               : anything else

When several book rules could apply, pick by what is readable: spines → bookshelf,
two or more front covers → multiple_book_covers, a single front cover → book_cover.

No markdown, no explanation. Only the JSON object."""

# Plausible model spellings folded onto the enum. The multi-book labels are
# multi-word, so models paraphrase them far more than they do "slide" — and an
# unmatched label becomes "unknown", which the admission gate rejects and caches
# permanently. Aliases are checked after the space/hyphen fold in
# :func:`_normalize_type`.
_TYPE_ALIASES = {
    "book_covers": "multiple_book_covers",
    "multiple_books": "multiple_book_covers",
    "book_stack": "multiple_book_covers",
    "books": "multiple_book_covers",
    "bookshelves": "bookshelf",
    "book_shelf": "bookshelf",
    "book_shelves": "bookshelf",
    "bookcase": "bookshelf",
    "bookshelf_or_library": "bookshelf",
    "library_shelf": "bookshelf",
}


_IMAGE_TYPE_RE = re.compile(
    r'"?image_type"?\s*[:=]\s*"?([a-z]+(?:[ _-][a-z]+)*)', re.IGNORECASE
)
_DESCRIPTION_RE = re.compile(r'"?description"?\s*[:=]\s*"(.*)"\s*}?\s*$', re.IGNORECASE | re.DOTALL)


# ── Per-type content prompts (DESIGN.md §3.2) ────────────────────────────────
# The categories that get a transcript, a content summary, and structured book
# extraction. Every label in :data:`_VALID_TYPES` except "unknown" is covered.
_TRANSCRIPT_TYPES = {"slide", "notes", "whiteboard"}
_BOOK_TYPES = {"book_cover", "multiple_book_covers", "bookshelf"}

# Shared trailer: every content prompt also returns a short artifact-level
# description, because that is what the browse card shows (and what lands in
# ``images.description`` / ``documents.card_summary``). Asking for it in the same
# call is what keeps the per-type prompt free of an extra round trip.
_DESCRIPTION_NOTE = """\
"description" is about the artifact itself — the medium and layout, one or two
sentences — not about its subject matter. It is shown on a browse card next to
the thumbnail, so it must stay a truthful caption of the photo.

No markdown, no explanation. Only the JSON object."""

_TRANSCRIPT_PROMPT = f"""You are transcribing an image from a personal research \
archive: a presentation slide, a page of notes, or a whiteboard.

Return ONLY a JSON object with exactly these two keys:
{{
  "transcript": "<the text and diagram content, in reading order>",
  "description": "<1-2 sentences naming what the artifact is>"
}}

Rules for "transcript":
- Transcribe every legible word: title, headings, bullets, equations, axis
  labels, captions, marginalia. Keep the author's own wording and terminology.
- Read handwriting as carefully as you can; it is the point of this pass.
- Render diagrams as short semantic lines describing the relationships, e.g.
  "arrow: encoder -> latent -> decoder", not the shapes or colours.
- Separate lines and blocks with newlines, following the layout order.
- Do not summarise, explain, or add anything that is not on the image.
- If a word is genuinely unreadable write "[?]" rather than guessing at it.

{_DESCRIPTION_NOTE}"""

_POSTER_PROMPT = f"""You are summarising the content of a poster or figure from a \
personal research archive.

Return ONLY a JSON object with exactly these two keys:
{{
  "content": "<what the poster says, in 4-8 sentences>",
  "description": "<1-2 sentences naming what the artifact is>"
}}

Rules for "content":
- Report the substance: the title, the question asked, the method, the results
  and any numbers shown, the conclusion.
- Keep named entities verbatim — datasets, models, institutions, authors,
  metrics — and use the poster's own terminology.
- Say nothing the poster does not show. Omit rather than infer.
- Many posters reference no publication: do NOT try to identify a paper, invent
  a citation, or guess where the work was published.

{_DESCRIPTION_NOTE}"""

_BOOK_PROMPT = """You are cataloguing the books visible in a photograph from a \
personal research archive.

Return ONLY a JSON object with exactly these two keys:
{
  "books": [
    {"title": "<title as printed>", "authors": ["<author>"], "isbn": "<digits or null>"}
  ],
  "description": "<1-2 sentences naming what the artifact is>"
}

Rules for "books":
- One entry per book whose title you can actually read. Partial entries are
  expected and fine: an empty "authors" list and a null "isbn" are valid.
- NEVER invent, complete, or guess a title, an author, or an ISBN. These values
  are used to look the book up later, so a wrong one is far worse than a missing
  one. If you cannot read a title, leave that book out entirely.
- Copy each title exactly as printed, including a subtitle after a colon when it
  is legible.
- "isbn": only when the digits are physically legible on the image (usually a
  back cover or copyright page). Digits only, hyphens allowed. Otherwise null.
- Ignore publisher blurbs, prices, series slogans, and review quotes."""

# Label-specific hint appended to the one book prompt: what is actually readable
# differs sharply between a front cover and a shelf of spines.
_BOOK_HINTS = {
    "book_cover": (
        "This image shows a single front cover, so return exactly one entry."
    ),
    "multiple_book_covers": (
        "Several front covers face the camera. Return one entry per cover whose "
        "title is readable, and do not merge two books into one entry."
    ),
    "bookshelf": (
        "The books are shelved or stacked with mostly SPINES visible. Titles are "
        "often partial and set sideways, authors are frequently absent, and ISBNs "
        "are never readable here — use null. Transcribe only what is legible on "
        "each spine; skip spines you cannot read."
    ),
}


def _content_prompt(image_type: str) -> str | None:
    """Return the content prompt for ``image_type``, or ``None`` if it has none.

    ``None`` means "no per-type extraction applies" (``unknown``, or any label a
    future model emits that is not in the enum), in which case the caller keeps
    the generic classify-and-describe behaviour.
    """
    if image_type in _TRANSCRIPT_TYPES:
        return _TRANSCRIPT_PROMPT
    if image_type == "poster":
        return _POSTER_PROMPT
    if image_type in _BOOK_TYPES:
        return f"{_BOOK_PROMPT}\n\n{_BOOK_HINTS[image_type]}\n\n{_DESCRIPTION_NOTE}"
    return None


# Field-level salvage for the content prompts. Non-greedy and order-independent
# (unlike :data:`_DESCRIPTION_RE`, which is anchored to the end of the reply)
# because these replies carry two long string fields in either order.
_CONTENT_FIELD_RE = re.compile(
    r'"?(?:transcript|content)"?\s*[:=]\s*"(.*?)"\s*(?:,\s*"|\}|$)', re.IGNORECASE | re.DOTALL
)
_DESC_FIELD_RE = re.compile(
    r'"?description"?\s*[:=]\s*"(.*?)"\s*(?:,\s*"|\}|$)', re.IGNORECASE | re.DOTALL
)
_BOOKS_ARRAY_RE = re.compile(r'"?books"?\s*:\s*(\[.*\])', re.IGNORECASE | re.DOTALL)
_BOOK_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_TITLE_FIELD_RE = re.compile(r'"?title"?\s*[:=]\s*"([^"]*)"', re.IGNORECASE)
_AUTHORS_FIELD_RE = re.compile(r'"?authors?"?\s*[:=]\s*(?:\[([^\]]*)\]|"([^"]*)")', re.IGNORECASE)
_ISBN_FIELD_RE = re.compile(r'"?isbn(?:_1[03])?"?\s*[:=]\s*"?([0-9Xx][0-9Xx\s-]{8,})', re.IGNORECASE)

# Values models emit instead of admitting they cannot read a title. Dropping the
# entry is deliberate: a placeholder title becomes a bogus identifier lookup.
_PLACEHOLDER_VALUES = {
    "",
    "illegible",
    "n/a",
    "na",
    "none",
    "not visible",
    "null",
    "title",
    "unknown",
    "unknown author",
    "unknown title",
    "unreadable",
    "untitled",
}


def _normalize_type(raw: object) -> str:
    """Map a model's label onto :data:`_VALID_TYPES`, or ``"unknown"``.

    Vision models answer with the label as prose — ``"book cover"``,
    ``"Book Cover"``, ``"book-cover"`` — rather than the exact enum spelling.
    A strict membership test threw those away as ``unknown``, which at the
    admission gate rejects a *correctly* classified image and caches the
    rejection permanently. Case, spaces, and hyphens are therefore folded to the
    underscore form before matching, then :data:`_TYPE_ALIASES` maps common
    paraphrases (``"bookshelves"``, ``"book covers"``) onto the enum.
    """
    slug = re.sub(r"[\s-]+", "_", str(raw).strip().lower())
    slug = _TYPE_ALIASES.get(slug, slug)
    return slug if slug in _VALID_TYPES else "unknown"


def _salvage_vision_fields(content: str) -> tuple[str, str]:
    """Recover ``(image_type, description)`` when the model emits invalid JSON.

    Vision models frequently return a description containing unescaped quotes,
    which breaks strict JSON parsing. Rather than discard an otherwise good
    answer, pull the two fields directly; the greedy description match keeps any
    inner quotes as literal text.
    """
    m = _IMAGE_TYPE_RE.search(content)
    image_type = _normalize_type(m.group(1)) if m else "unknown"
    description = ""
    d = _DESCRIPTION_RE.search(content.strip())
    if d:
        description = d.group(1).strip()
    return image_type, description


# ── Book field normalisation (no lookup — see DESIGN.md §3.2) ─────────────────


def _clean_field(raw: object) -> str:
    """Strip a model-emitted string, blanking the placeholders it uses for "dunno"."""
    value = str(raw or "").strip().strip('"').strip()
    return "" if value.lower() in _PLACEHOLDER_VALUES else value


def _normalize_authors(raw: object) -> list[str]:
    """Coerce the ``authors`` field to a clean list — models emit list *or* string."""
    if isinstance(raw, str):
        raw = re.split(r",| and |;|&", raw)
    if not isinstance(raw, list | tuple):
        return []
    return [name for name in (_clean_field(a) for a in raw) if name]


def _normalize_isbn(raw: object) -> str | None:
    """Return a 10- or 13-character ISBN, or ``None``.

    Only the shape is checked here: checksum validation belongs to the lookup
    step (DESIGN.md §3.2), which is a separate, default-off mechanism. This module
    performs no lookup and makes no network call.
    """
    digits = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper()
    return digits if len(digits) in (10, 13) else None


def _normalize_books(raw: object) -> list[dict]:
    """Coerce the model's ``books`` payload into ``[{title, authors, isbn}]``.

    Entries without a readable title are dropped: downstream these feed
    identifier lookups, where a placeholder title produces a confidently wrong
    match while a missing entry produces none.
    """
    if isinstance(raw, dict):
        raw = raw.get("books", [])
    if not isinstance(raw, list | tuple):
        return []

    books: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"title": entry}
        if not isinstance(entry, dict):
            continue
        title = _clean_field(entry.get("title"))
        if not title:
            continue
        books.append({
            "title":   title,
            "authors": _normalize_authors(entry.get("authors") or entry.get("author")),
            "isbn":    _normalize_isbn(entry.get("isbn")),
        })
    return books


def _books_to_text(books: list[dict]) -> str:
    """Flatten extracted book fields into the lines that get indexed."""
    lines = []
    for book in books:
        line = book["title"]
        if book["authors"]:
            line += " — " + ", ".join(book["authors"])
        if book["isbn"]:
            line += f" (ISBN {book['isbn']})"
        lines.append(line)
    return "\n".join(lines)


def _salvage_books(raw: str) -> list[dict]:
    """Recover book entries from a reply whose JSON does not parse."""
    m = _BOOKS_ARRAY_RE.search(raw)
    block = m.group(1) if m else raw
    try:
        return _normalize_books(json.loads(block))
    except (json.JSONDecodeError, TypeError):
        pass

    entries: list[dict] = []
    for obj in _BOOK_OBJECT_RE.findall(block):
        title = _TITLE_FIELD_RE.search(obj)
        if not title:
            continue
        authors = _AUTHORS_FIELD_RE.search(obj)
        isbn = _ISBN_FIELD_RE.search(obj)
        entries.append({
            "title":   title.group(1),
            "authors": (authors.group(1) or authors.group(2)) if authors else "",
            "isbn":    isbn.group(1) if isbn else None,
        })
    return _normalize_books(entries)


def _salvage_content_fields(raw: str) -> tuple[str, str]:
    """Recover ``(content, description)`` from a malformed content-pass reply."""
    body = raw.strip()
    content = _CONTENT_FIELD_RE.search(body)
    description = _DESC_FIELD_RE.search(body)
    return (
        content.group(1).strip() if content else "",
        description.group(1).strip() if description else "",
    )


# ── Image encoding ────────────────────────────────────────────────────────────


def _encode_image(path: Path, max_px: int = 1024) -> str:
    """Return base64-encoded JPEG, downsampled to ``max_px`` on the longest side."""
    import io

    from PIL import Image, ImageOps

    with Image.open(path) as img:
        # Respect EXIF orientation so portrait phone photos aren't sent sideways
        # to the vision model (which classifies/describes rotated text poorly).
        img = ImageOps.exif_transpose(img).convert("RGB")
        w, h = img.size
        if max(w, h) > max_px:
            scale = max_px / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()


# ── Pass 1 + 2: classify + describe (single vision call) ─────────────────────


class VisionUnavailable(RuntimeError):
    """The vision backend itself failed to produce a classification.

    Deliberately distinct from a *genuine* ``"unknown"`` result: that means the
    model ran and judged the image uninteresting, which the admission gate
    rejects on purpose. A backend error (Ollama down, timeout, transport
    failure) is instead an environment problem. In ``strict`` mode
    :func:`classify_and_describe` raises this rather than returning ``"unknown"``,
    so the gate never mistakes an outage for a library full of uninteresting
    images and rejects (and caches) every one of them.
    """


def classify_and_describe(
    path: Path,
    model: str = "llava",
    provider: "VisionProvider | None" = None,
    *,
    strict: bool = False,
) -> tuple[str, str]:
    """Call the vision provider. Returns ``(image_type, description)``.

    ``provider`` overrides the configured vision backend — used by the admission
    gate to run a distinct (smaller/faster) classifier.

    On failure the behaviour depends on ``strict``:

    - ``strict=False`` (default, the main describe pass): degrade to
      ``("unknown", "")`` so an image still ingests without a type/description.
    - ``strict=True`` (the admission gate): raise :class:`VisionUnavailable`, so a
      backend outage surfaces as a *failed* image rather than a silent rejection.

    Note that a successful call which genuinely classifies the image as
    ``"unknown"`` never raises, even under ``strict`` — that is a real result the
    gate is entitled to reject.
    """
    try:
        vision = provider or get_vision_provider()
        b64 = _encode_image(path)
        content = vision.complete(_CLASSIFY_PROMPT, b64, model=model)

        try:
            parsed = _parse_llm_json(content)
            image_type = parsed.get("image_type", "unknown")
            description = parsed.get("description", "")
        except (ValueError, json.JSONDecodeError):
            # Model ignored the JSON grammar (or emitted stray quotes anyway):
            # salvage the fields instead of dropping to unknown/empty.
            image_type, description = _salvage_vision_fields(content)

        return _normalize_type(image_type), description

    except Exception as exc:
        if strict:
            raise VisionUnavailable(
                f"Vision backend failed to classify {path.name}: {exc}"
            ) from exc
        log.warning("Vision LLM failed for %s: %s", path.name, exc)
        return "unknown", ""


# ── Pass 2: per-type content extraction ───────────────────────────────────────


@dataclass
class ImageContent:
    """What the main vision pass extracted from one image.

    ``description`` stays a caption of the artifact (it feeds
    ``images.description`` and the browse card); ``content`` is the per-type
    extraction that actually earns retrieval — a transcript, a poster summary, or
    the flattened book lines. ``books`` carries the structured cover fields for a
    later identifier lookup; nothing here performs one.
    """

    image_type: str
    description: str = ""
    content: str = ""
    books: list[dict] = field(default_factory=list)


def _parse_content_reply(raw: str, image_type: str) -> tuple[str, list[dict], str]:
    """Parse a content-pass reply into ``(content, books, description)``."""
    wants_books = image_type in _BOOK_TYPES

    parsed: object = None
    try:
        parsed = _parse_llm_json(raw)
    except (ValueError, json.JSONDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        description = _clean_field(parsed.get("description"))
        if wants_books:
            books = _normalize_books(parsed.get("books"))
            content = _books_to_text(books)
        else:
            books = []
            content = str(parsed.get("transcript") or parsed.get("content") or "").strip()
        if content or books or description:
            return content, books, description

    # Either the JSON was invalid, or ``_parse_llm_json`` salvaged only an inner
    # object (its ``{...}`` fallback cannot span the nested book entries). Pull
    # the fields out of the raw text instead of discarding a good answer.
    content, description = _salvage_content_fields(raw)
    books = _salvage_books(raw) if wants_books else []
    if books:
        content = _books_to_text(books)
    return content, books, description


def extract_image_content(
    path: Path,
    image_type: str | None = None,
    model: str = "llava",
    provider: "VisionProvider | None" = None,
) -> ImageContent:
    """Run the main vision pass with the prompt that fits the image's category.

    ``image_type`` is the label the admission gate already resolved. Passing it
    skips classification entirely: the one call this makes spends its tokens on
    the content prompt for that category, so the better prompt is free.

    When it is ``None`` — the gate is disabled or ``--skip-gate`` was used — there
    is no label yet, so this falls back to classify-then-prompt (two calls).

    Failures degrade like :func:`classify_and_describe`: whatever was already
    resolved is returned, never raised. The gate is the only caller that needs an
    outage to surface, and it runs before this pass.
    """
    description = ""
    classified = image_type is None
    if classified:
        image_type, description = classify_and_describe(path, model=model, provider=provider)

    prompt = _content_prompt(image_type)
    if prompt is None:
        # "unknown" (or an unmapped label): no per-type extraction applies, so
        # keep the pre-existing generic behaviour.
        if not classified:
            image_type, description = classify_and_describe(path, model=model, provider=provider)
        return ImageContent(image_type=image_type, description=description)

    try:
        vision = provider or get_vision_provider()
        raw = vision.complete(prompt, _encode_image(path), model=model)
    except Exception as exc:
        log.warning("Vision content pass failed for %s: %s", path.name, exc)
        return ImageContent(image_type=image_type, description=description)

    content, books, parsed_description = _parse_content_reply(raw, image_type)
    return ImageContent(
        image_type  = image_type,
        description = parsed_description or description,
        content     = content,
        books       = books,
    )


# ── Pass 3: OCR ───────────────────────────────────────────────────────────────


def ocr_image(path: Path, lang: str = "eng") -> str:
    """Run OCR via the configured provider. Returns text or ``""`` on failure."""
    return get_ocr_provider().ocr(path, lang=lang)


# ── Pass 4: CLIP embedding ────────────────────────────────────────────────────


def clip_embed_image(path: Path) -> list[float] | None:
    """Return a normalised image embedding, or ``None`` on failure."""
    return get_image_embedder().embed_image(path)


def clip_embed_text(query: str) -> list[float] | None:
    """Embed a text query in the image-embedding space (for cross-modal search)."""
    return get_image_embedder().embed_text(query)


# ── Searchable text for description + OCR ────────────────────────────────────


def image_search_text(ocr_text: str, description: str, content: str = "") -> str | None:
    """Combine the extracted texts for Chroma text search (same collection as chunks).

    Still the single place image text is assembled. ``content`` is the per-type
    extraction (transcript, poster summary, book lines) and leads: it is the
    reason the photo exists, while the description is the least specific part and
    MiniLM truncates in the low hundreds of word-pieces.
    """
    combined = "\n\n".join(filter(None, [content, description, ocr_text])).strip()
    return combined or None
