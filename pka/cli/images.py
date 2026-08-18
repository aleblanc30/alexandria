"""Scan an images folder and run all extraction passes.

Usage::

    alexandria images
    alexandria images --folder ~/Pictures/research
    alexandria images --folder ~/Pictures/a --folder ~/Pictures/b
    alexandria images --skip-ocr
    alexandria images --skip-clip
    alexandria images --skip-vision
    alexandria images --vision-model moondream
    alexandria images --dry-run
    alexandria images --search "neural network diagram"
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pka.cli._logging import setup_logging
from pka.config import settings as cfg
from pka.connectors.images import scan_image_dirs
from pka.db.queries import init_db
from pka.ingestion.image_pipeline import (
    ingest_images,
    search_images_by_inferred_text,
    search_images_by_text,
)

log = logging.getLogger("run_images")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria images")
    parser.add_argument("--folder",       type=Path, action="append", default=None,
                        help="Image folder to scan (repeatable; defaults to configured folders)")
    parser.add_argument("--vision-model", type=str,  default=None,
                        help="Ollama vision model name (llava, moondream, …)")
    parser.add_argument("--ocr-lang",     type=str,  default=None,
                        help="OCR language code(s), e.g. eng+fra")
    parser.add_argument("--skip-ocr",     action="store_true")
    parser.add_argument("--skip-clip",    action="store_true",
                        help="Skip the CLIP visual embedding pass "
                             "(already skipped unless ALEXANDRIA_CLIP_ENABLED=1)")
    parser.add_argument("--skip-vision",  action="store_true")
    parser.add_argument("--skip-gate",    action="store_true",
                        help="Bypass the two-step admission gate (text coverage + VLM category)")
    parser.add_argument("--force-reindex", action="store_true")
    parser.add_argument("--reset-rejections", action="store_true",
                        help="Clear the gate rejection cache before scanning, so "
                             "previously-rejected images are re-evaluated")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--search",       type=str, default=None,
                        help="Run an image text search instead of indexing "
                             "(inferred text, plus CLIP when enabled)")
    args = parser.parse_args(argv)

    setup_logging()
    init_db()

    if args.reset_rejections:
        from pka.db.queries import clear_image_rejections

        removed = clear_image_rejections()
        log.info("Cleared %d entries from the gate rejection cache", removed)

    if args.search:
        # Both paths, labelled — the CLIP one is empty unless clip_enabled.
        found = [("clip", r) for r in search_images_by_text(args.search)]
        found += [("text", r) for r in search_images_by_inferred_text(args.search)]
        if not found:
            log.info("No results.")
        for path, r in sorted(found, key=lambda pr: pr[1]["distance"]):
            log.info(
                "  [%s via %s] %s  (dist=%.3f)",
                r.get("image_type") or "?", path,
                r.get("filename") or r.get("document_id"), r["distance"],
            )
        return 0

    folders = args.folder or cfg.image_dirs
    log.info("Scanning %s…", ", ".join(str(f) for f in folders))
    image_files = scan_image_dirs(folders)
    log.info("Found %d images", len(image_files))

    stats = ingest_images(
        image_files,
        skip_existing = not args.force_reindex,
        vision_model  = args.vision_model or cfg.vision_model,
        ocr_lang      = args.ocr_lang or cfg.ocr_lang,
        skip_ocr      = args.skip_ocr or not cfg.ocr_enabled,
        skip_clip     = args.skip_clip or not cfg.clip_enabled,
        skip_vision   = args.skip_vision,
        skip_gate     = args.skip_gate,
        dry_run       = args.dry_run,
    )

    log.info(
        "Done. processed=%d  skipped=%d  rejected=%d  failed=%d",
        stats["processed"], stats["skipped"], stats.get("rejected", 0), stats["failed"],
    )
    log.info("By type: %s", stats["by_type"])
    if stats.get("by_reason"):
        log.info("Rejected by reason: %s", stats["by_reason"])
    return 0


if __name__ == "__main__":
    main()
