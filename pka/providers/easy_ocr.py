"""Local OCR via EasyOCR (deep-learning detector + recognizer).

Replaces the old Tesseract path: EasyOCR handles slides, photos, and stylised
text far better and needs no system binary — only the ``easyocr`` wheel, which
rides on the ``torch`` we already pull in for CLIP. The recognition models are
downloaded and cached on first use.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# The config default is the Tesseract-style ``"eng"``; map the common 3-letter
# codes to the ISO codes EasyOCR expects. Unmapped codes pass through unchanged
# so an EasyOCR-native code (``"en"``, ``"ch_sim"``) also works.
_LANG_MAP = {
    "eng": "en", "fra": "fr", "deu": "de", "spa": "es", "ita": "it",
    "por": "pt", "rus": "ru", "jpn": "ja", "kor": "ko", "nld": "nl",
    "chi_sim": "ch_sim", "chi_tra": "ch_tra",
}


def _to_easyocr_langs(lang: str) -> list[str]:
    """Convert a Tesseract-style ``"eng+fra"`` string to EasyOCR ISO codes."""
    codes = [_LANG_MAP.get(part, part) for part in lang.split("+") if part]
    return codes or ["en"]


class EasyOcrProvider:
    """Run EasyOCR over an image file."""

    def __init__(self) -> None:
        # Readers load torch models and are expensive to build, so cache one per
        # language set. Tied to the instance, which ``reset_providers`` clears.
        self._readers: dict[tuple[str, ...], object] = {}

    def _reader(self, langs: list[str]):
        key = tuple(langs)
        reader = self._readers.get(key)
        if reader is None:
            import easyocr

            reader = easyocr.Reader(langs, gpu=False)
            self._readers[key] = reader
        return reader

    def ocr(self, path: Path, lang: str = "eng") -> str:
        try:
            reader = self._reader(_to_easyocr_langs(lang))
            # detail=0 → plain strings; paragraph=True groups words into lines.
            lines = reader.readtext(str(path), detail=0, paragraph=True)
            return "\n".join(s.strip() for s in lines if s and s.strip()).strip()
        except ImportError:
            log.debug("easyocr not installed — OCR skipped for %s", path.name)
            return ""
        except Exception as exc:
            log.warning("EasyOCR failed for %s: %s", path.name, exc)
            return ""
