"""
Image ingestion pipeline.

Orchestrates the four extraction passes and persists results to:
  - ``archive.db`` : ``images`` + ``image_tags`` tables
  - ChromaDB      : two collections — ``alexandria_clip`` (CLIP vectors) and
                    ``alexandria_chunks`` (text vectors from the per-type content
                    extraction + description + OCR, so images appear in unified
                    text search).
"""

import json
import logging
import threading
import time
import uuid
from pathlib import Path

import sqlalchemy as sa

from pka.clustering.cluster_tags import insert_overlay_tags
from pka.config import settings as cfg
from pka.connectors.images import ImageFile
from pka.constants import FetchStatus, Source, TagOrigin
from pka.db.queries import (
    DocumentWrite,
    delete_image_document,
    existing_chunk_count,
    get_engine,
    get_rejected_paths,
    record_image_rejection,
    update_card_summary,
    upsert_document,
)
from pka.db.schema import images
from pka.ingestion.core import ingest_text_block
from pka.ingestion.image_extractor import (
    clip_embed_image,
    extract_image_content,
    image_search_text,
    ocr_image,
)
from pka.ingestion.loops import run_embed_loop, run_metadata_loop

log = logging.getLogger(__name__)

CLIP_COLLECTION = "alexandria_clip"

# ``delete(ids=…)`` binds one variable per id and SQLITE_MAX_VARIABLE_NUMBER is
# 32766, so a purge of a large image library must batch. Same ceiling and same
# size as ``vector_store``'s read paging.
_DELETE_BATCH_SIZE = 5_000

# Cached Chroma client/collection — recreating these per call is expensive.
# Creation is serialized, and the client itself comes from vector_store so the
# process holds exactly one Chroma client.
_clip_client = None
_clip_col = None
_clip_lock = threading.RLock()


def _get_clip_collection():
    """The CLIP collection, on the *shared* Chroma client.

    Building a second ``PersistentClient`` for the same path used to race the
    text one: Chroma's per-path system cache is not thread-safe, and an image
    sync embedding while a text sync embeds is exactly two threads creating a
    client at once. See ``vector_store`` for what that failure looks like.
    """
    global _clip_client, _clip_col
    with _clip_lock:
        if _clip_col is None:
            from pka.storage.vector_store import get_client

            _clip_client = get_client()
            _clip_col = _clip_client.get_or_create_collection(
                name=CLIP_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
        return _clip_col


def reset_clip_collection() -> None:
    """Drop the cached client and collection — used by the test suite."""
    global _clip_client, _clip_col
    with _clip_lock:
        _clip_client = None
        _clip_col = None


def delete_clip_vectors(vector_ids: list[str]) -> int:
    """Remove CLIP vectors from the ``alexandria_clip`` collection.

    Returns the number of ids Chroma accepted (0 if none). Batched under the
    SQL variable ceiling, so a failing batch costs only its own ids rather than
    the whole call.
    """
    ids = [vid for vid in vector_ids if vid]
    if not ids:
        return 0
    col = _get_clip_collection()
    deleted = 0
    for i in range(0, len(ids), _DELETE_BATCH_SIZE):
        batch = ids[i : i + _DELETE_BATCH_SIZE]
        try:
            col.delete(ids=batch)
        except Exception as exc:
            log.warning("CLIP Chroma delete failed: %s", exc)
            continue
        deleted += len(batch)
    return deleted


def _ensure_image_document(img: ImageFile) -> int:
    """Create (or update) the unified ``documents`` row backing an image.

    Images are first-class documents (``source=image``): the file path is both
    ``source_id`` and ``url_or_path`` so the cover route can stream the bytes.
    """
    return upsert_document(
        DocumentWrite(
            source=Source.IMAGE,
            source_id=str(img.path),
            title=img.filename,
            url_or_path=str(img.path),
            date_added=img.date_taken,
            fetch_status=FetchStatus.AVAILABLE,
        )
    )


def _image_already_indexed(path: Path) -> int | None:
    """Return image DB id if already registered (row exists), else None."""
    with get_engine().connect() as con:
        row = con.execute(sa.select(images.c.id).where(images.c.path == str(path))).fetchone()
    return row[0] if row else None


def indexed_image_paths() -> set[str]:
    """Every image path already registered, in one query.

    The status probe asks "is this indexed?" once per scanned image; at a few
    thousand images that is a few thousand connections per poll.
    """
    with get_engine().connect() as con:
        return {row[0] for row in con.execute(sa.select(images.c.path))}


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


def gate_rejected_paths() -> set[str]:
    """Image paths the admission gate has already rejected.

    Empty when the gate is off, so a cached rejection from an earlier gated run
    cannot keep an image out once the user disables the gate.
    """
    return get_rejected_paths() if cfg.image_gate_enabled else set()


def admitted_images(image_files: list[ImageFile]) -> list[ImageFile]:
    """Drop gate-rejected paths — the images a pass can actually act on.

    Progress counters must be scoped to this set, not the raw scan: both passes
    skip rejected paths, so counting them as outstanding work pins a phase total
    the job can never reach.
    """
    rejected = gate_rejected_paths()
    if not rejected:
        return list(image_files)
    return [img for img in image_files if str(img.path) not in rejected]


def register_images(
    image_files: list[ImageFile],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Scan pass: persist image file records without OCR / CLIP / embedding."""
    # Images the gate previously rejected must not be re-registered on later
    # metadata runs — skip them up front (only while the gate is active).
    rejected_paths = gate_rejected_paths()

    def _register_one(img: ImageFile) -> str:
        if str(img.path) in rejected_paths:
            return "skipped"
        if _image_already_indexed(img.path):
            return "skipped"
        if dry_run:
            return "dry_run"
        doc_id = _ensure_image_document(img)
        eng = get_engine()
        with eng.begin() as con:
            con.execute(
                sa.text("""
                INSERT INTO images
                    (document_id, path, filename, image_type, width, height,
                     file_size, date_taken, indexed_at)
                VALUES
                    (:doc_id, :path, :fname, 'unknown', :w, :h, :sz, :dt, NULL)
                ON CONFLICT(path) DO UPDATE SET
                    document_id = excluded.document_id
            """),
                {
                    "doc_id": doc_id,
                    "path": str(img.path),
                    "fname": img.filename,
                    "w": img.width,
                    "h": img.height,
                    "sz": img.file_size,
                    "dt": img.date_taken,
                },
            )
        return "processed"

    return run_metadata_loop(
        image_files,
        known={},
        get_source_id=lambda img: str(img.path),
        persist=_register_one,
        progress_key=progress_key,
        skip_when_in_known=False,
    )


def _attach_book_synopses(doc_id: int, img: ImageFile, books: list[dict]) -> int:
    """Look each extracted book up and embed its synopsis as its own chunk.

    One chunk **per book**, not one blob: a shelf photo should match a query for
    any single title on it, and merging ten synopses into one vector would dilute
    every one of them.

    Returns the number of synopsis chunks added. Never raises — a lookup failure
    must not cost the image its ordinary ingestion. The network gate lives inside
    :func:`lookup_book` (``external_lookup_enabled``), so there is no flag check
    here; with the flag off this loop simply resolves nothing.
    """
    if not books:
        return 0
    from pka.ingestion.openlibrary import lookup_book

    added = 0
    for entry in books:
        title = str(entry.get("title") or "").strip()
        authors = [str(a) for a in (entry.get("authors") or []) if str(a).strip()]
        isbn = entry.get("isbn")
        if not title and not isbn:
            continue
        try:
            synopsis = lookup_book(title=title, authors=authors, isbn=isbn)
        except Exception as exc:
            log.warning("Book lookup failed for %r: %s", title or isbn, exc)
            continue
        if synopsis is None:
            continue

        text = synopsis.embed_text()
        if not text:
            continue

        # Chroma metadata takes scalars only — drop anything unresolved rather
        # than sending None.
        meta = {
            "title": img.filename,
            "modality": "image",
            "pass": "external_synopsis",
            "book_title": synopsis.title or title,
            "resolved_by": synopsis.resolved_by,
        }
        if synopsis.isbn:
            meta["isbn"] = synopsis.isbn
        if synopsis.work_key:
            meta["work_key"] = synopsis.work_key

        result = ingest_text_block(
            doc_id,
            text,
            Source.IMAGE,
            extra_metadata=meta,
            chunk_offset=existing_chunk_count(doc_id),
            min_chars=1,
        )
        if not result["skipped"]:
            added += result["chunks_added"]
    if added:
        log.info("Attached %d book synopsis chunk(s) to image %s", added, img.filename)
    return added


def ingest_image(
    img: ImageFile,
    vision_model: str = "llava",
    ocr_lang: str = "eng",
    skip_ocr: bool = False,
    skip_clip: bool = False,
    skip_vision: bool = False,
    skip_gate: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run all extraction passes for a single image and persist results."""
    # Label resolved by the gate (None when the gate did not run), used to pick
    # the per-type content prompt below.
    gate_type: str | None = None

    # ── Admission gate: text coverage + category of interest ─────────────────
    # Fail either gate → cache the path and stop before the expensive passes.
    if cfg.image_gate_enabled and not skip_gate:
        from pka.ingestion.image_gate import gate_image

        gate = gate_image(img.path, vision_model=cfg.image_gate_vision_model, ocr_lang=ocr_lang)
        if not gate.passed:
            if not dry_run:
                record_image_rejection(
                    str(img.path), gate.reason, gate.text_coverage, gate.image_type
                )
                # Drop any rows a prior metadata pass registered for this path so
                # rejected images leave nothing behind in the document/image
                # tables (and purge vectors if it had been fully ingested before).
                removed = delete_image_document(str(img.path))
                if removed["chunk_vector_ids"]:
                    from pka.storage import vector_store

                    vector_store.purge_vectors(removed["chunk_vector_ids"])
                if removed["clip_vector_id"]:
                    delete_clip_vectors([removed["clip_vector_id"]])
            return {
                "status": "rejected",
                "reason": gate.reason,
                "image_type": gate.image_type,
                "text_coverage": gate.text_coverage,
            }
        # NOTE: the gate's label — not the main pass's — is what becomes
        # ``images.image_type`` and the ``TagOrigin.INFERRED`` overlay tag whenever
        # the gate runs. That is a deliberate change: the gate model already
        # decided admission on this label and cached it in the rejection record, so
        # it is the authoritative one, and reusing it is what lets the main pass
        # spend its single call on the per-type content prompt instead of a third
        # classification. When the gate is off (or --skip-gate), the label still
        # comes from the main ``vision_model``, as before.
        gate_type = gate.image_type

    # ── Pass 1+2: classify (or reuse the gate label) + per-type content ───────
    # ``gate_type`` also survives skip_vision: it was resolved before this pass,
    # so the type/tag stays correct even with the describe pass turned off.
    image_type, description, content_text = (gate_type or "unknown"), "", ""
    books: list[dict] = []
    if not skip_vision:
        extracted = extract_image_content(img.path, image_type=gate_type, model=vision_model)
        image_type = extracted.image_type
        description = extracted.description
        content_text = extracted.content
        books = extracted.books

    # ── Pass 3: OCR ───────────────────────────────────────────────────────────
    ocr_text = ""
    if not skip_ocr:
        ocr_text = ocr_image(img.path, lang=ocr_lang)

    # ── Pass 4a: CLIP image embedding ─────────────────────────────────────────
    clip_vector = None
    if not skip_clip:
        clip_vector = clip_embed_image(img.path)

    # ── Pass 4b: searchable text (content + description + OCR → Chroma embeds) ─
    text_doc = image_search_text(ocr_text, description, content_text)

    if dry_run:
        return {
            "status": "dry_run",
            "image_type": image_type,
            "books": books,
            "has_ocr": bool(ocr_text),
            "has_clip": clip_vector is not None,
            "has_text_emb": text_doc is not None,
        }

    # ── Persist the unified document + image sidecar row ──────────────────────
    now = int(time.time())
    clip_vid = str(uuid.uuid4()) if clip_vector else None
    doc_id = _ensure_image_document(img)

    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            sa.text("""
            INSERT INTO images
                (document_id, path, filename, image_type, width, height,
                 file_size, date_taken, ocr_text, description, books_json,
                 clip_vector_id, indexed_at)
            VALUES
                (:doc_id,:path,:fname,:itype,:w,:h,:sz,:dt,:ocr,:desc,:books,
                 :cvid,:now)
            ON CONFLICT(path) DO UPDATE SET
                document_id    = excluded.document_id,
                image_type     = excluded.image_type,
                ocr_text       = excluded.ocr_text,
                description    = excluded.description,
                books_json     = excluded.books_json,
                clip_vector_id = excluded.clip_vector_id,
                indexed_at     = excluded.indexed_at
        """),
            {
                "doc_id": doc_id,
                "path": str(img.path),
                "fname": img.filename,
                "itype": image_type,
                "w": img.width,
                "h": img.height,
                "sz": img.file_size,
                "dt": img.date_taken,
                "ocr": ocr_text or None,
                "desc": description or None,
                "books": json.dumps(books) if books else None,
                "cvid": clip_vid,
                "now": now,
            },
        )

        row = con.execute(sa.select(images.c.id).where(images.c.path == str(img.path))).fetchone()
        image_id = row[0]

        # The vision classification becomes an inferred overlay tag on the
        # document, so it filters and displays like any other document tag.
        if image_type != "unknown":
            insert_overlay_tags(con, [doc_id], image_type, TagOrigin.INFERRED)

    # ── Description card + searchable text (chunks keyed by document_id) ───────
    update_card_summary(doc_id, description or None)
    ingest_text_block(
        doc_id,
        text_doc or "",
        Source.IMAGE,
        extra_metadata={"title": img.filename, "modality": "image"},
        fallback_text=img.filename,
    )

    # ── Book synopses (default-off identifier lookup; DESIGN.md §3.2) ─────────
    # Added as separate chunks so the browse card and ``images.description``
    # stay a truthful caption of the photograph itself.
    synopsis_chunks = _attach_book_synopses(doc_id, img, books)

    # ── CLIP vector for cross-modal (text → image) search ─────────────────────
    if clip_vector and clip_vid:
        try:
            col = _get_clip_collection()
            col.upsert(
                ids=[clip_vid],
                embeddings=[clip_vector],
                documents=[img.filename],
                metadatas=[
                    {
                        "document_id": doc_id,
                        "image_id": image_id,
                        "image_type": image_type,
                        "filename": img.filename,
                        "path": str(img.path),
                        "modality": "clip",
                    }
                ],
            )
        except Exception as exc:
            log.warning("CLIP Chroma upsert failed: %s", exc)

    return {
        "status": "ok",
        "image_type": image_type,
        # Extracted cover fields, also cached in ``images.books_json``. A later
        # (default-off) identifier lookup consumes these; nothing here looks
        # anything up or touches the network.
        "books": books,
        "synopsis_chunks": synopsis_chunks,
        "has_ocr": bool(ocr_text),
        "has_clip": clip_vector is not None,
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
    skip_gate: bool = False,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    by_type: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    rejected = 0

    # Images previously rejected by the gate are cached; skip re-gating them.
    gate_active = cfg.image_gate_enabled and not skip_gate
    rejected_paths = get_rejected_paths() if (skip_existing and gate_active) else set()

    def _should_skip(img: ImageFile) -> bool:
        if skip_existing and _image_already_embedded(img.path):
            return True
        return str(img.path) in rejected_paths

    def _process(img: ImageFile) -> tuple[bool, int]:
        nonlocal rejected
        result = ingest_image(
            img,
            vision_model=vision_model,
            ocr_lang=ocr_lang,
            skip_ocr=skip_ocr,
            skip_clip=skip_clip,
            skip_vision=skip_vision,
            skip_gate=skip_gate,
            dry_run=dry_run,
        )
        if result["status"] == "rejected":
            rejected += 1
            reason = result.get("reason") or "unknown"
            by_reason[reason] = by_reason.get(reason, 0) + 1
            return False, 0
        t = result["image_type"]
        by_type[t] = by_type.get(t, 0) + 1
        return True, 0

    stats = run_embed_loop(
        image_files,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda img, exc: log.exception("Failed image %s: %s", img.path.name, exc),
    )
    stats.pop("chunks", None)
    # Rejections surface as ``process`` returning False, which the loop tallies
    # under ``skipped``; split them back out into their own count.
    stats["skipped"] = max(0, stats["skipped"] - rejected)
    stats["rejected"] = rejected
    stats["by_type"] = by_type
    stats["by_reason"] = by_reason
    return stats


# ── Search helpers ────────────────────────────────────────────────────────────
#
# Two independent paths reach an image from a text query (DESIGN.md §3.3):
#
#   1. ``search_images_by_text``          — CLIP, query text → pixels. Opt-in.
#   2. ``search_images_by_inferred_text`` — MiniLM, query text → the text the
#      pipeline inferred *from* the picture (per-type content + description +
#      OCR, plus any synopsis chunks), i.e. the ``alexandria_chunks`` rows this
#      module already writes for every image.
#
# The second path is always available: those chunks are written whether or not
# CLIP ran, so an image stays findable with the visual index turned off.


def search_images_by_text(query: str, n: int = 10) -> list[dict]:
    """Return images whose CLIP embedding is nearest to the CLIP text embedding of ``query``.

    Returns ``[]`` immediately when ``clip_enabled`` is off — the gate lives here
    rather than in each caller so the CLI, ``/search`` and ``/images/search`` all
    inherit it, and so nothing loads the CLIP model just to answer a query
    against vectors that were never written.
    """
    if not cfg.clip_enabled:
        return []

    from pka.ingestion.image_extractor import clip_embed_text

    vec = clip_embed_text(query)
    if vec is None:
        return []

    col = _get_clip_collection()
    res = col.query(query_embeddings=[vec], n_results=n)
    out: list[dict] = []
    for i, vid in enumerate(res["ids"][0]):
        meta = res["metadatas"][0][i]
        out.append(
            {
                "vector_id": vid,
                "document_id": meta.get("document_id"),
                "filename": meta.get("filename"),
                "path": meta.get("path"),
                "image_type": meta.get("image_type"),
                "distance": res["distances"][0][i],
            }
        )
    return out


def search_images_by_inferred_text(query: str, n: int = 10) -> list[dict]:
    """Return images whose *inferred* text best matches ``query``.

    Queries the shared chunk collection restricted to ``source=image``, so this
    searches exactly what the extraction passes read out of the picture. Chunks
    are collapsed to their best-scoring one per document — a slide deck photo
    with a transcript chunk and a synopsis chunk is one result, not two.

    Hits carry ``document_id`` (not ``clip_vector_id``); resolve them with
    :func:`pka.api.image_hits.inferred_hits_to_image_out`.
    """
    from pka.storage import vector_store

    try:
        # Over-fetch: several chunks of the same image can occupy the top-n, and
        # non-image sources are excluded by the filter, not by re-ranking.
        hits = vector_store.query(
            query,
            n_results=max(n * 3, 10),
            where={"source": str(Source.IMAGE)},
        )
    except Exception as exc:
        log.warning("Image text search unavailable: %s", exc)
        return []

    best: dict[int, dict] = {}
    for hit in hits:
        meta = hit.get("metadata") or {}
        doc_id = meta.get("document_id")
        if doc_id is None:
            continue
        doc_id = int(doc_id)
        current = best.get(doc_id)
        if current is None or hit["distance"] < current["distance"]:
            best[doc_id] = {
                "document_id": doc_id,
                "vector_id": hit["vector_id"],
                "distance": hit["distance"],
                "text": hit.get("text") or "",
                # Which extraction rung produced the matching chunk
                # ("external_synopsis", "summary", … ; absent for the main
                # content+description+OCR block).
                "pass": meta.get("pass"),
                "filename": meta.get("title"),
            }
    ranked = sorted(best.values(), key=lambda h: h["distance"])
    return ranked[:n]
