"""Local OCR via EasyOCR (deep-learning detector + recognizer).

Replaces the old Tesseract path: EasyOCR handles slides, photos, and stylised
text far better and needs no system binary — only the ``easyocr`` wheel, which
rides on the ``torch`` we already pull in for CLIP. The recognition models are
downloaded and cached on first use.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class EasyOcrUnavailable(RuntimeError):
    """The EasyOCR backend itself cannot be loaded (the wheel is not installed).

    Deliberately distinct from a per-image OCR failure. A missing install is an
    environment/config problem that must *surface*: in the admission gate a
    text-coverage of ``0.0`` means *reject*, so silently returning ``0.0`` here
    would reject an entire image library the moment EasyOCR is absent. Callers
    let this propagate; they only swallow genuine per-image failures.
    """


_UNAVAILABLE_MSG = (
    "EasyOCR is required for image text extraction / the admission gate, but "
    "'easyocr' is not installed. Install it (a core dependency: "
    "pip install -e '.[dev]') or disable the gate with "
    "ALEXANDRIA_IMAGE_GATE_ENABLED=0."
)


def ensure_easyocr_available() -> None:
    """Raise :class:`EasyOcrUnavailable` if the ``easyocr`` wheel is not present.

    Checks the real dependency (not the wrapper module, which imports ``easyocr``
    lazily and so would import fine even when the wheel is missing).
    """
    if importlib.util.find_spec("easyocr") is None:
        raise EasyOcrUnavailable(_UNAVAILABLE_MSG)


def _import_easyocr():
    try:
        import easyocr
    except ImportError as exc:  # pragma: no cover - guarded by ensure_easyocr_available
        raise EasyOcrUnavailable(_UNAVAILABLE_MSG) from exc
    return easyocr

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


def _oriented_rgb_array(path: Path):
    """Load ``path`` as an EXIF-oriented RGB numpy array for EasyOCR.

    EasyOCR's built-in file loading mishandles some EXIF-rotated photos (portrait
    phone shots carry ``Orientation=6``), yielding an empty array that crashes
    its internal ``cv2.resize`` with ``!ssize.empty()``. Decoding through PIL and
    applying ``exif_transpose`` sidesteps that and also feeds the recognizer
    upright text, which OCRs far better. Passing the resulting array to
    ``readtext`` bypasses EasyOCR's own loader entirely.
    """
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        oriented = ImageOps.exif_transpose(im).convert("RGB")
        return np.asarray(oriented)


def _polygon_area(box) -> float:
    """Shoelace area of an EasyOCR bounding box (a 4-point polygon).

    ``box`` is a sequence of ``[x, y]`` corners (ints or numpy scalars). Returns
    the absolute area in pixels², or ``0.0`` if the box is malformed.
    """
    try:
        pts = [(float(x), float(y)) for x, y in box]
    except (TypeError, ValueError):
        return 0.0
    if len(pts) < 3:
        return 0.0
    area = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


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
            easyocr = _import_easyocr()
            reader = easyocr.Reader(langs, gpu=False)
            self._readers[key] = reader
        return reader

    def ocr(self, path: Path, lang: str = "eng") -> str:
        try:
            reader = self._reader(_to_easyocr_langs(lang))
            # detail=0 → plain strings; paragraph=True groups words into lines.
            lines = reader.readtext(_oriented_rgb_array(path), detail=0, paragraph=True)
            return "\n".join(s.strip() for s in lines if s and s.strip()).strip()
        except EasyOcrUnavailable:
            # Missing install → surface it; do not degrade to "no text".
            raise
        except Exception as exc:
            log.warning("EasyOCR failed for %s: %s", path.name, exc)
            return ""

    def text_coverage(self, path: Path, lang: str = "eng") -> float:
        """Return the fraction of the image (0..1) covered by detected text.

        Runs detection with per-box polygons (``detail=1``, ``paragraph=False``
        so boxes aren't merged into oversized paragraph hulls), sums their
        areas, and divides by the image area. Overlapping boxes make this a
        slight over-estimate — fine for a threshold gate.

        Returns ``0.0`` on a genuine per-image failure (unreadable/corrupt file),
        so the caller treats it as text-free. A missing EasyOCR install instead
        raises :class:`EasyOcrUnavailable` — never silently ``0.0`` — so a broken
        environment cannot reject an entire library at the gate.
        """
        try:
            arr = _oriented_rgb_array(path)
            h, w = arr.shape[:2]
            total = float(w * h)
            if total <= 0:
                return 0.0

            reader = self._reader(_to_easyocr_langs(lang))
            results = reader.readtext(arr, detail=1, paragraph=False)

            covered = 0.0
            for box, *_ in results:
                covered += _polygon_area(box)
            return max(0.0, min(covered / total, 1.0))
        except EasyOcrUnavailable:
            raise
        except Exception as exc:
            log.warning("EasyOCR coverage failed for %s: %s", path.name, exc)
            return 0.0
