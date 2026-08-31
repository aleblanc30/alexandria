"""Two-step admission gate for the image pipeline.

Before an image runs the expensive describe / OCR / CLIP passes it must clear
both gates:

  1. **Text coverage** — EasyOCR detects text boxes covering at least
     ``image_gate_text_coverage_min`` of the image area.
  2. **Category of interest** — a fast VLM (``image_gate_vision_*`` config,
     default Ollama ``moondream``) classifies it as a non-``unknown`` type.

The cheap local EasyOCR pass runs first; the VLM is only called if coverage
passes. Failing either gate is surfaced as a :class:`GateResult` with
``passed=False`` and a ``reason`` the caller records in the rejection cache.

EasyOCR is required whenever the gate is enabled: it is a core dependency
(``easyocr`` in ``pyproject.toml``), and the gate uses it directly regardless of
the configured ``ocr_provider``. If it cannot be imported the gate raises rather
than silently admitting everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from pka.config import settings as cfg
from pka.ingestion.image_extractor import classify_and_describe
from pka.providers import get_gate_vision_provider

log = logging.getLogger(__name__)

# Rejection reason codes (also stored in image_rejections.reason).
REASON_LOW_COVERAGE = "low_text_coverage"
REASON_NOT_CATEGORY = "not_category_of_interest"

# Cached EasyOCR provider for coverage measurement, independent of the
# configured ``ocr_provider`` (which may be the VLM backend). Cleared by
# :func:`reset_gate`, wired into the test suite.
_easyocr = None


def reset_gate() -> None:
    """Drop the cached EasyOCR instance — used by the test suite."""
    global _easyocr
    _easyocr = None


def _get_easyocr():
    """Return a cached EasyOCR provider, raising a clear error if unavailable.

    ``ensure_easyocr_available`` checks the actual ``easyocr`` wheel — importing
    the provider module alone is not enough, since it imports ``easyocr`` lazily
    and so succeeds even when the dependency is missing. Failing here (rather than
    letting ``text_coverage`` return ``0.0``) stops a broken install from
    silently rejecting every image at the gate.
    """
    global _easyocr
    if _easyocr is None:
        from pka.providers.easy_ocr import EasyOcrProvider, ensure_easyocr_available

        ensure_easyocr_available()
        _easyocr = EasyOcrProvider()
    return _easyocr


@dataclass
class GateResult:
    """Outcome of the admission gate for one image."""

    passed: bool
    reason: str | None  # None when passed
    image_type: str  # gate classifier label (reject record + content prompt)
    description: str  # gate classifier description (not reused downstream)
    text_coverage: float  # measured fraction 0..1


def gate_image(
    img_path: Path,
    *,
    coverage_min: float | None = None,
    vision_model: str | None = None,
    ocr_lang: str = "eng",
) -> GateResult:
    """Run the two-step gate for a single image path.

    The classification runs on the gate's own (smaller/faster) model. Its
    **label** is carried downstream — ``ingest_image`` reuses it to pick the
    per-type content prompt (DESIGN.md §3.2) rather than classifying a second
    time — while its **description** is not: the main pass produces its own with
    the larger ``vision_model``.
    """
    threshold = cfg.image_gate_text_coverage_min if coverage_min is None else coverage_min
    model = vision_model or cfg.image_gate_vision_model

    # ── Step 1: text coverage (cheap, local) ─────────────────────────────────
    coverage = _get_easyocr().text_coverage(img_path, lang=ocr_lang)
    if coverage < threshold:
        return GateResult(
            passed=False,
            reason=REASON_LOW_COVERAGE,
            image_type="unknown",
            description="",
            text_coverage=coverage,
        )

    # ── Step 2: category of interest (VLM, only if coverage passed) ───────────
    # strict=True: a backend outage must raise (→ failed image), never degrade to
    # "unknown" here, which the gate would otherwise reject and cache for good.
    image_type, description = classify_and_describe(
        img_path,
        model=model,
        provider=get_gate_vision_provider(),
        strict=True,
    )
    if image_type == "unknown":
        return GateResult(
            passed=False,
            reason=REASON_NOT_CATEGORY,
            image_type=image_type,
            description=description,
            text_coverage=coverage,
        )

    return GateResult(
        passed=True,
        reason=None,
        image_type=image_type,
        description=description,
        text_coverage=coverage,
    )
