"""Local OCR via Tesseract (``pytesseract``)."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class TesseractOcrProvider:
    """Run Tesseract OCR over an image file."""

    def ocr(self, path: Path, lang: str = "eng") -> str:
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
