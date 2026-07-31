"""
Image ingestion pipeline.

Orchestrates the four extraction passes and persists results to:
  - ``archive.db`` : ``images`` + ``image_tags`` tables
  - ChromaDB      : two collections — ``alexandria_clip`` (CLIP vectors) and
                    ``alexandria_chunks`` (text vectors from OCR + description,
                    so images appear in unified text search).
"""
import logging
import time
import uuid
from pathlib import Path

import sqlalchemy as sa

from pka.connectors.images import ImageFile
from pka.constants import TagOrigin
from pka.db.queries import get_engine
from pka.db.schema import images
from pka.ingestion.image_extractor import (
    classify_and_describe,
    clip_embed_image,
    image_search_text,
    ocr_image,
)
from pka.ingestion.loops import run_embed_loop, run_metadata_loop

log = logging.getLogger(__name__)

CLIP_COLLECTION = "alexandria_clip"

# Cached Chroma client/collection — recreating these per call is expensive.
_clip_client = None
_clip_col = None


def _get_clip_collection():
    global _clip_client, _clip_col
    if _clip_col is None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        from pka.config import settings as cfg
        _clip_client = chromadb.PersistentClient(
            path=str(cfg.chroma_dir),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        _clip_col = _clip_client.get_or_create_collection(
            name=CLIP_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
    return _clip_col


def reset_clip_collection() -> None:
    """Drop the cached client and collection — used by the test suite."""
    global _clip_client, _clip_col
    _clip_client = None
    _clip_col = None


def _image_already_indexed(path: Path) -> int | None:
    """Return image DB id if already registered (row exists), else None."""
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(images.c.id).where(images.c.path == str(path))
        ).fetchone()
    return row[0] if row else None


def _image_already_embedded(path: Path) -> bool:
    """True if this image has already been through the OCR/CLIP/embed pass.

    Distinct from ``_image_already_indexed``: a row exists as soon as the
    metadata (register) pass runs, well before ``indexed_at`` is set by
    ``ingest_image``. Using the row-exists check here would skip every image
    during the embed pass, since they were all already registered.
    """
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(images.c.indexed_at).where(images.c.path == str(path))
        ).fetchone()
    return bool(row and row[0] is not None)


def register_images(
    image_files: list[ImageFile],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Scan pass: persist image file records without OCR / CLIP / embedding."""
    def _register_one(img: ImageFile) -> str:
        if _image_already_indexed(img.path):
            return "skipped"
        if dry_run:
            return "dry_run"
        eng = get_engine()
        with eng.begin() as con:
            con.execute(sa.text("""
                INSERT INTO images
                    (path, filename, image_type, width, height, file_size,
                     date_taken, indexed_at)
                VALUES
                    (:path, :fname, 'unknown', :w, :h, :sz, :dt, NULL)
                ON CONFLICT(path) DO NOTHING
            """), {
                "path": str(img.path), "fname": img.filename,
                "w": img.width, "h": img.height, "sz": img.file_size,
                "dt": img.date_taken,
            })
        return "processed"

    return run_metadata_loop(
        image_files,
        known={},
        get_source_id=lambda img: str(img.path),
        persist=_register_one,
        progress_key=progress_key,
        skip_when_in_known=False,
    )


def ingest_image(
    img: ImageFile,
    vision_model: str = "llava",
    ocr_lang: str = "eng",
    skip_ocr: bool = False,
    skip_clip: bool = False,
    skip_vision: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run all extraction passes for a single image and persist results."""
    # ── Pass 1+2: classify + describe ────────────────────────────────────────
    image_type, description = ("unknown", "")
    if not skip_vision:
        image_type, description = classify_and_describe(img.path, model=vision_model)

    # ── Pass 3: OCR ───────────────────────────────────────────────────────────
    ocr_text = ""
    if not skip_ocr:
        ocr_text = ocr_image(img.path, lang=ocr_lang)

    # ── Pass 4a: CLIP image embedding ─────────────────────────────────────────
    clip_vector = None
    if not skip_clip:
        clip_vector = clip_embed_image(img.path)

    # ── Pass 4b: searchable text (OCR + description → Chroma embeds) ─────────
    text_doc = image_search_text(ocr_text, description)

    if dry_run:
        return {
            "status":       "dry_run",
            "image_type":   image_type,
            "has_ocr":      bool(ocr_text),
            "has_clip":     clip_vector is not None,
            "has_text_emb": text_doc is not None,
        }

    # ── Persist to SQLite ─────────────────────────────────────────────────────
    now = int(time.time())
    clip_vid = str(uuid.uuid4()) if clip_vector else None
    text_vid = str(uuid.uuid4()) if text_doc else None

    eng = get_engine()
    with eng.begin() as con:
        con.execute(sa.text("""
            INSERT INTO images
                (path, filename, image_type, width, height, file_size,
                 date_taken, ocr_text, description, clip_vector_id,
                 text_vector_id, indexed_at)
            VALUES
                (:path,:fname,:itype,:w,:h,:sz,:dt,:ocr,:desc,:cvid,:tvid,:now)
            ON CONFLICT(path) DO UPDATE SET
                image_type     = excluded.image_type,
                ocr_text       = excluded.ocr_text,
                description    = excluded.description,
                clip_vector_id = excluded.clip_vector_id,
                text_vector_id = excluded.text_vector_id,
                indexed_at     = excluded.indexed_at
        """), {
            "path": str(img.path), "fname": img.filename,
            "itype": image_type,
            "w": img.width, "h": img.height, "sz": img.file_size,
            "dt": img.date_taken,
            "ocr": ocr_text or None, "desc": description or None,
            "cvid": clip_vid, "tvid": text_vid, "now": now,
        })

        row = con.execute(
            sa.select(images.c.id).where(images.c.path == str(img.path))
        ).fetchone()
        image_id = row[0]

        if image_type != "unknown":
            con.execute(sa.text("""
                INSERT OR IGNORE INTO image_tags (image_id, tag, origin)
                VALUES (:iid, :tag, :origin)
            """), {
                "iid": image_id,
                "tag": image_type,
                "origin": str(TagOrigin.INFERRED),
            })

    # ── Persist to Chroma ─────────────────────────────────────────────────────
    meta_base = {
        "image_id":   image_id,
        "image_type": image_type,
        "filename":   img.filename,
        "path":       str(img.path),
    }

    if clip_vector and clip_vid:
        try:
            col = _get_clip_collection()
            col.upsert(
                ids        = [clip_vid],
                embeddings = [clip_vector],
                documents  = [img.filename],
                metadatas  = [{**meta_base, "modality": "clip"}],
            )
        except Exception as exc:
            log.warning("CLIP Chroma upsert failed: %s", exc)

    if text_doc and text_vid:
        try:
            from pka.storage.vector_store import upsert_chunks
            upsert_chunks(
                ids       = [text_vid],
                texts     = [text_doc],
                metadatas = [{**meta_base, "modality": "text", "source": "image"}],
            )
        except Exception as exc:
            log.warning("Text Chroma upsert failed: %s", exc)

    return {
        "status":       "ok",
        "image_type":   image_type,
        "has_ocr":      bool(ocr_text),
        "has_clip":     clip_vector is not None,
        "has_text_emb": text_doc is not None,
    }


def ingest_images(
    image_files: list[ImageFile],
    skip_existing: bool = True,
    vision_model: str = "llava",
    ocr_lang: str = "eng",
    skip_ocr: bool = False,
    skip_clip: bool = False,
    skip_vision: bool = False,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    by_type: dict[str, int] = {}

    def _process(img: ImageFile) -> tuple[bool, int]:
        result = ingest_image(
            img,
            vision_model = vision_model,
            ocr_lang     = ocr_lang,
            skip_ocr     = skip_ocr,
            skip_clip    = skip_clip,
            skip_vision  = skip_vision,
            dry_run      = dry_run,
        )
        t = result["image_type"]
        by_type[t] = by_type.get(t, 0) + 1
        return True, 0

    stats = run_embed_loop(
        image_files,
        should_skip=lambda img: skip_existing and _image_already_embedded(img.path),
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda img, exc: log.exception("Failed image %s: %s", img.path.name, exc),
    )
    stats.pop("chunks", None)
    stats["by_type"] = by_type
    return stats


# ── Cross-modal search helper ─────────────────────────────────────────────────

def search_images_by_text(query: str, n: int = 10) -> list[dict]:
    """Return images whose CLIP embedding is nearest to the CLIP text embedding of ``query``."""
    from pka.ingestion.image_extractor import clip_embed_text

    vec = clip_embed_text(query)
    if vec is None:
        return []

    col = _get_clip_collection()
    res = col.query(query_embeddings=[vec], n_results=n)
    out: list[dict] = []
    for i, vid in enumerate(res["ids"][0]):
        out.append({
            "vector_id":  vid,
            "filename":   res["metadatas"][0][i].get("filename"),
            "path":       res["metadatas"][0][i].get("path"),
            "image_type": res["metadatas"][0][i].get("image_type"),
            "distance":   res["distances"][0][i],
        })
    return out
