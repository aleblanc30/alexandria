"""Single chunk → embed → persist path for text-bearing documents."""
from __future__ import annotations

import uuid

from pka.config import settings as cfg
from pka.constants import Source
from pka.db.queries import insert_chunks
from pka.ingestion.chunker import sentence_window_chunks
from pka.storage.vector_store import upsert_chunks


def ingest_text_block(
    doc_id: int,
    text: str,
    source: Source,
    extra_metadata: dict | None = None,
    chunk_offset: int = 0,
    dry_run: bool = False,
    min_chars: int | None = None,
) -> dict:
    """Chunk, embed, and persist a single block of text for a document.

    Returns:
        ``{"chunks_added": int, "skipped": bool}``.
    """
    if not text or not text.strip():
        return {"chunks_added": 0, "skipped": True}

    chunk_texts = sentence_window_chunks(
        text,
        window    = cfg.chunk_sentences,
        overlap   = cfg.chunk_overlap,
        min_chars = min_chars if min_chars is not None else cfg.min_chunk_chars,
    )
    if not chunk_texts:
        return {"chunks_added": 0, "skipped": True}

    if dry_run:
        return {"chunks_added": len(chunk_texts), "skipped": False}

    vector_ids = [str(uuid.uuid4()) for _ in chunk_texts]

    base_meta = {
        "document_id": doc_id,
        "source": str(source),
        **(extra_metadata or {}),
    }

    upsert_chunks(
        ids       = vector_ids,
        texts     = chunk_texts,
        metadatas = [
            {**base_meta, "chunk_index": chunk_offset + i}
            for i in range(len(chunk_texts))
        ],
    )
    insert_chunks([
        {
            "document_id": doc_id,
            "chunk_index": chunk_offset + i,
            "text":        t,
            "token_count": len(t.split()),
            "vector_id":   vid,
        }
        for i, (t, vid) in enumerate(zip(chunk_texts, vector_ids, strict=True))
    ])
    return {"chunks_added": len(chunk_texts), "skipped": False}
