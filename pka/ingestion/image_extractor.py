"""
Four extraction passes for each image:

  1. classify()     — vision LLM (llava/moondream/remote) → image_type label
  2. describe()     — vision LLM → prose description (reuses the classify call)
  3. ocr()          — OCR provider (VLM/EasyOCR) → raw text
  4. clip_embed()   — image-embed provider (CLIP) → float vector

All passes are independent and can be skipped selectively. Every pass delegates
to the backend selected in ``pka/providers/`` (Ollama/OpenRouter/OVH for vision,
VLM or EasyOCR for OCR, CLIP for embeddings); this module owns only the prompt,
image encoding, and JSON-salvage logic.
"""

import base64
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pka.json_utils import parse_llm_json as _parse_llm_json
from pka.providers import get_image_embedder, get_ocr_provider, get_vision_provider

if TYPE_CHECKING:
    from pka.providers.base import VisionProvider

log = logging.getLogger(__name__)

# Valid image type labels the LLM must choose from
_VALID_TYPES = {"book_cover", "slide", "poster", "notes", "whiteboard", "unknown"}

_CLASSIFY_PROMPT = """You are analysing an image from a personal research archive.

Return ONLY a JSON object with exactly these two keys:
{
  "image_type": "<one of: book_cover, slide, poster, notes, whiteboard, unknown>",
  "description": "<2-4 sentence description of the image content>"
}

Rules:
- book_cover   : a photograph or scan of a book/report/thesis cover
- slide        : a presentation slide (PowerPoint, Keynote, Beamer, etc.)
- poster       : an academic/conference poster or article figure
- notes        : handwritten or typed notes, sticky notes, notebook pages
- whiteboard   : a whiteboard or blackboard with writing or diagrams
- unknown      : anything else

No markdown, no explanation. Only the JSON object."""


_IMAGE_TYPE_RE = re.compile(
    r'"?image_type"?\s*[:=]\s*"?([a-z]+(?:[ _-][a-z]+)*)', re.IGNORECASE
)
_DESCRIPTION_RE = re.compile(r'"?description"?\s*[:=]\s*"(.*)"\s*}?\s*$', re.IGNORECASE | re.DOTALL)


def _normalize_type(raw: object) -> str:
    """Map a model's label onto :data:`_VALID_TYPES`, or ``"unknown"``.

    Vision models answer with the label as prose — ``"book cover"``,
    ``"Book Cover"``, ``"book-cover"`` — rather than the exact enum spelling.
    A strict membership test threw those away as ``unknown``, which at the
    admission gate rejects a *correctly* classified image and caches the
    rejection permanently. Case, spaces, and hyphens are therefore folded to the
    underscore form before matching.
    """
    slug = re.sub(r"[\s-]+", "_", str(raw).strip().lower())
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


def image_search_text(ocr_text: str, description: str) -> str | None:
    """Combine OCR + description for Chroma text search (same collection as chunks)."""
    combined = "\n\n".join(filter(None, [description, ocr_text])).strip()
    return combined or None
