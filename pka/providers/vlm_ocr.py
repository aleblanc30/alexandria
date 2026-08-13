"""OCR via the vision model — transcribe image text with the same VLM used for
classification and description.

Unlike Tesseract, this handles slides, handwriting, and stylised covers far
better, at the cost of a vision-model call per image. It reuses the configured
:class:`VisionProvider`, so it works whether vision runs on Ollama or a remote
OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pka.config import settings as cfg
from pka.json_utils import parse_llm_json
from pka.providers import get_vision_provider

log = logging.getLogger(__name__)

_OCR_PROMPT = """Transcribe ALL text visible in this image, exactly as written.

Return ONLY a JSON object with a single key:
{"text": "<verbatim transcription, or an empty string if the image has no text>"}

Rules:
- Preserve reading order; separate lines with \\n.
- Do not translate, summarise, correct, or add commentary.
- No markdown, no explanation. Only the JSON object."""

# Salvage the text field when the model emits invalid JSON (e.g. unescaped
# quotes inside the transcription).
_TEXT_RE = re.compile(r'"?text"?\s*[:=]\s*"(.*)"\s*}?\s*$', re.IGNORECASE | re.DOTALL)


class VlmOcrProvider:
    """Transcribe image text using the configured vision model."""

    def ocr(self, path: Path, lang: str = "eng") -> str:
        # ``lang`` is accepted for interface parity with Tesseract but unused —
        # the VLM auto-detects script and language.
        model = cfg.vlm_ocr_model or cfg.vision_model or None
        try:
            from pka.ingestion.image_extractor import _encode_image

            b64 = _encode_image(path)
            content = get_vision_provider().complete(_OCR_PROMPT, b64, model=model)
        except Exception as exc:
            log.warning("VLM OCR failed for %s: %s", path.name, exc)
            return ""

        try:
            return str(parse_llm_json(content).get("text", "")).strip()
        except (ValueError, json.JSONDecodeError):
            m = _TEXT_RE.search(content.strip())
            return m.group(1).strip() if m else ""
