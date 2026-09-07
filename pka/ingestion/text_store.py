"""Verbatim retention of extracted body text (``document_texts``).

Why this exists, per ``planning/FULL_TEXT_RETENTION.md``: after ingestion the
body text survives only as ``chunks.text``, which is whitespace-normalised, cut
into overlapping sentence windows, and missing every window shorter than
``min_chunk_chars``. Re-summarising, re-chunking, re-running an extraction fix
or auditing what the fetcher actually got therefore all require going back to
the network. Keeping the text costs a little disk and retires that.

**What gets stored** is text with no other verbatim home that cost a network
round trip or a slow extraction to produce: fetched bodies, and (later) book /
PDF extractions. Reddit's inline bodies (``reddit_items.body``), image OCR and
descriptions (``images``), Zotero abstracts and YouTube descriptions are all
either already retained or a millisecond re-read from their own source, so they
stay out — duplicating them would be disk spent to save nothing.

Deliberately *not* called from :func:`pka.ingestion.core.ingest_text_block`:
that runs once per block (a book calls it per section) and once per pass (the
generated summary goes through it too), so a write there would overwrite a body
with a summary. Runners call this explicitly, which keeps the invariant plain:
**one row is one document's body text.**
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import zlib

import sqlalchemy as sa

from pka.config import settings as cfg
from pka.db.queries import get_engine
from pka.db.schema import document_texts

log = logging.getLogger(__name__)

#: Value of ``document_texts.encoding`` this module writes. The column exists so
#: a later codec change is a migration rather than an archaeology exercise.
ENCODING = "zlib"

_COMPRESS_LEVEL = 6


def encode_text(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), _COMPRESS_LEVEL)


def decode_text(blob: bytes, encoding: str = ENCODING) -> str:
    if encoding == ENCODING:
        return zlib.decompress(blob).decode("utf-8")
    if encoding == "raw":
        return bytes(blob).decode("utf-8")
    raise ValueError(f"Unknown document_texts encoding: {encoding!r}")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def store_document_text(
    doc_id: int,
    text: str,
    *,
    blocks: list[dict] | None = None,
    dry_run: bool = False,
) -> bool:
    """Retain *text* as ``doc_id``'s body text. Returns whether a row was written.

    Idempotent: a second call for the same document replaces the row, so a
    re-fetch refreshes the text (and its hash) rather than duplicating it.

    Never raises. Retention is a convenience for later passes, not part of what
    makes a document ingested — a failure here must not cost the document its
    chunks, the same rule :func:`pka.ingestion.core.attach_summary_chunk`
    follows for summaries.
    """
    if dry_run or not cfg.retain_document_text:
        return False
    body = (text or "").strip()
    if not body:
        return False
    try:
        values = {
            "text": encode_text(body),
            "encoding": ENCODING,
            "char_count": len(body),
            "content_hash": content_hash(body),
            "blocks_json": json.dumps(blocks) if blocks else None,
            "extracted_at": int(time.time()),
        }
        with get_engine().begin() as con:
            existing = con.execute(
                sa.select(document_texts.c.id).where(document_texts.c.document_id == doc_id)
            ).fetchone()
            if existing:
                con.execute(
                    sa.update(document_texts)
                    .where(document_texts.c.document_id == doc_id)
                    .values(**values)
                )
            else:
                con.execute(sa.insert(document_texts).values(document_id=doc_id, **values))
        return True
    except Exception:
        log.exception("Storing document text failed for doc_id=%d", doc_id)
        return False


def load_document_text(doc_id: int) -> str | None:
    """The retained body text for *doc_id*, or ``None`` when there is none.

    ``None`` is the ordinary case for anything ingested before retention
    shipped: there is no backfill, and reconstructing text from chunks would
    look verbatim while being a reconstruction.
    """
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(document_texts.c.text, document_texts.c.encoding).where(
                document_texts.c.document_id == doc_id
            )
        ).fetchone()
    if not row:
        return None
    try:
        return decode_text(row[0], row[1] or ENCODING)
    except Exception:
        log.exception("Decoding stored text failed for doc_id=%d", doc_id)
        return None


def document_text_meta(doc_id: int) -> dict | None:
    """Size, hash, section map and timestamp for *doc_id*, without decompressing."""
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(
                document_texts.c.char_count,
                document_texts.c.content_hash,
                document_texts.c.blocks_json,
                document_texts.c.extracted_at,
                document_texts.c.encoding,
            ).where(document_texts.c.document_id == doc_id)
        ).fetchone()
    if not row:
        return None
    return {
        "char_count": row[0],
        "content_hash": row[1],
        "blocks": json.loads(row[2]) if row[2] else None,
        "extracted_at": row[3],
        "encoding": row[4],
    }
