#!/usr/bin/env python
"""Scan an images folder and run all extraction passes.

Usage::

    python scripts/run_images.py
    python scripts/run_images.py --folder ~/Pictures/research
    python scripts/run_images.py --skip-ocr
    python scripts/run_images.py --skip-clip
    python scripts/run_images.py --skip-vision
    python scripts/run_images.py --vision-model moondream
    python scripts/run_images.py --dry-run
    python scripts/run_images.py --search "neural network diagram"
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.config import settings as cfg
from pka.connectors.images import scan_images
from pka.db.queries import init_db
from pka.ingestion.image_pipeline import ingest_images, search_images_by_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("run_images")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder",       type=Path, default=None,
                        help="Override images folder path")
    parser.add_argument("--vision-model", type=str,  default=None,
                        help="Ollama vision model name (llava, moondream, …)")
    parser.add_argument("--ocr-lang",     type=str,  default=None,
                        help="Tesseract language code(s), e.g. eng+fra")
    parser.add_argument("--skip-ocr",     action="store_true")
    parser.add_argument("--skip-clip",    action="store_true")
    parser.add_argument("--skip-vision",  action="store_true")
    parser.add_argument("--force-reindex", action="store_true")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--search",       type=str, default=None,
                        help="Run a CLIP text search instead of indexing")
    args = parser.parse_args()

    init_db()

    if args.search:
        results = search_images_by_text(args.search)
        if not results:
            log.info("No results.")
        for r in results:
            log.info(
                "  [%s] %s  (dist=%.3f)",
                r["image_type"], r["filename"], r["distance"],
            )
        return

    folder = args.folder or cfg.images_dir
    log.info("Scanning %s…", folder)
    image_files = scan_images(folder)
    log.info("Found %d images", len(image_files))

    stats = ingest_images(
        image_files,
        skip_existing = not args.force_reindex,
        vision_model  = args.vision_model or cfg.vision_model,
        ocr_lang      = args.ocr_lang or cfg.ocr_lang,
        skip_ocr      = args.skip_ocr,
        skip_clip     = args.skip_clip,
        skip_vision   = args.skip_vision,
        dry_run       = args.dry_run,
    )

    log.info(
        "Done. processed=%d  skipped=%d  failed=%d",
        stats["processed"], stats["skipped"], stats["failed"],
    )
    log.info("By type: %s", stats["by_type"])


if __name__ == "__main__":
    main()
