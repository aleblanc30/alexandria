"""
Four extraction passes for each image:

  1. classify()     — vision LLM (llava/moondream) → image_type label
  2. describe()     — vision LLM → prose description (reuses the classify call)
  3. ocr()          — Tesseract → raw text
  4. clip_embed()   — CLIP model via transformers → float vector

All passes are independent and can be skipped selectively. The vision LLM
calls are batched into a single prompt that returns both classification and
description, avoiding two round-trips.
"""
import base64
import json
import logging
import re
from pathlib import Path

import httpx

from pka.config import settings as cfg

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


_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _parse_llm_json(raw: str) -> dict:
    """Strip Markdown code fences and parse JSON.

    Falls back to extracting the first ``{...}`` block if direct parse fails.
    """
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


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


# ── Pass 1 + 2: classify + describe (single LLM call) ────────────────────────

def classify_and_describe(
    path: Path,
    model: str = "llava",
) -> tuple[str, str]:
    """Call the Ollama vision model. Returns ``(image_type, description)``.

    Falls back to ``("unknown", "")`` on any failure.
    """
    try:
        b64 = _encode_image(path)
        resp = httpx.post(
            f"{cfg.ollama_base_url}/api/chat",
            json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": _CLASSIFY_PROMPT,
                    "images": [b64],
                }],
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        parsed = _parse_llm_json(content)

        image_type = parsed.get("image_type", "unknown")
        if image_type not in _VALID_TYPES:
            image_type = "unknown"
        return image_type, parsed.get("description", "")

    except Exception as exc:
        log.warning("Vision LLM failed for %s: %s", path.name, exc)
        return "unknown", ""


# ── Pass 3: OCR ───────────────────────────────────────────────────────────────

def ocr_image(path: Path, lang: str = "eng") -> str:
    """Run Tesseract OCR. Returns extracted text or empty string on failure."""
    try:
        import pytesseract
        from PIL import Image

        with Image.open(path) as img:
            grey = img.convert("L")  # greyscale boosts Tesseract accuracy
            text = pytesseract.image_to_string(grey, lang=lang)
            return text.strip()

    except ImportError:
        log.debug("pytesseract not installed — OCR skipped for %s", path.name)
        return ""
    except Exception as exc:
        log.warning("OCR failed for %s: %s", path.name, exc)
        return ""


# ── Pass 4: CLIP embedding ────────────────────────────────────────────────────

_clip_model = None
_clip_processor = None


def _load_clip():
    global _clip_model, _clip_processor
    if _clip_model is None:
        from transformers import CLIPModel, CLIPProcessor
        model_name = cfg.clip_model
        log.info("Loading CLIP model %s…", model_name)
        _clip_processor = CLIPProcessor.from_pretrained(model_name)
        _clip_model = CLIPModel.from_pretrained(model_name)
        _clip_model.eval()
        log.info("CLIP model loaded.")
    return _clip_model, _clip_processor


def clip_embed_image(path: Path) -> list[float] | None:
    """Return a normalised CLIP image embedding, or ``None`` on failure."""
    try:
        import torch
        from PIL import Image

        model, processor = _load_clip()
        with Image.open(path) as img:
            img = img.convert("RGB")
            inputs = processor(images=img, return_tensors="pt")

        with torch.no_grad():
            features = model.get_image_features(**inputs)
            features = features / features.norm(dim=-1, keepdim=True)

        return features.squeeze().tolist()

    except Exception as exc:
        log.warning("CLIP embedding failed for %s: %s", path.name, exc)
        return None


def clip_embed_text(query: str) -> list[float] | None:
    """Embed a text query in the CLIP text space (for cross-modal search)."""
    try:
        import torch
        model, processor = _load_clip()
        inputs = processor(text=[query], return_tensors="pt", padding=True)
        with torch.no_grad():
            features = model.get_text_features(**inputs)
            features = features / features.norm(dim=-1, keepdim=True)
        return features.squeeze().tolist()
    except Exception as exc:
        log.warning("CLIP text embedding failed: %s", exc)
        return None


# ── Text embedding for description + OCR ─────────────────────────────────────

def embed_image_text(ocr_text: str, description: str) -> list[float] | None:
    """Combine OCR + description and embed with the standard text embedder."""
    combined = "\n\n".join(filter(None, [description, ocr_text])).strip()
    if not combined:
        return None
    try:
        from pka.ingestion.embedder import embed_one
        return embed_one(combined)
    except Exception as exc:
        log.warning("Text embedding for image failed: %s", exc)
        return None
