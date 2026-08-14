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

from pka.json_utils import parse_llm_json as _parse_llm_json
from pka.providers import get_image_embedder, get_ocr_provider, get_vision_provider

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


_IMAGE_TYPE_RE = re.compile(r'"?image_type"?\s*[:=]\s*"?([a-z_]+)', re.IGNORECASE)
_DESCRIPTION_RE = re.compile(r'"?description"?\s*[:=]\s*"(.*)"\s*}?\s*$', re.IGNORECASE | re.DOTALL)


def _salvage_vision_fields(content: str) -> tuple[str, str]:
    """Recover ``(image_type, description)`` when the model emits invalid JSON.

    Vision models frequently return a description containing unescaped quotes,
    which breaks strict JSON parsing. Rather than discard an otherwise good
    answer, pull the two fields directly; the greedy description match keeps any
    inner quotes as literal text.
    """
    image_type = "unknown"
    m = _IMAGE_TYPE_RE.search(content)
    if m and m.group(1).lower() in _VALID_TYPES:
        image_type = m.group(1).lower()
    description = ""
    d = _DESCRIPTION_RE.search(content.strip())
    if d:
        description = d.group(1).strip()
    return image_type, description


# ── Image encoding ────────────────────────────────────────────────────────────


def _encode_image(path: Path, max_px: int = 1024) -> str:
    """Return base64-encoded JPEG, downsampled to ``max_px`` on the longest side."""
    import io

    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_px:
            scale = max_px / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()


# ── Pass 1 + 2: classify + describe (single vision call) ─────────────────────


def classify_and_describe(
    path: Path,
    model: str = "llava",
) -> tuple[str, str]:
    """Call the vision provider. Returns ``(image_type, description)``.

    Falls back to ``("unknown", "")`` on any failure.
    """
    try:
        b64 = _encode_image(path)
        content = get_vision_provider().complete(_CLASSIFY_PROMPT, b64, model=model)

        try:
            parsed = _parse_llm_json(content)
            image_type = parsed.get("image_type", "unknown")
            description = parsed.get("description", "")
        except (ValueError, json.JSONDecodeError):
            # Model ignored the JSON grammar (or emitted stray quotes anyway):
            # salvage the fields instead of dropping to unknown/empty.
            image_type, description = _salvage_vision_fields(content)

        if image_type not in _VALID_TYPES:
            image_type = "unknown"
        return image_type, description

    except Exception as exc:
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
