"""Move the archive to the configured embedding model (``alexandria reembed``).

Chunk vectors are only comparable within the model that made them, and three
things are derived from them, so a model change redoes all four in order:

1. the Chroma chunk index, rebuilt from SQLite chunk text with the
   ``embedding_model`` setting, which the collection then records;
2. every ``documents.doc_embedding``, the mean of its chunks;
3. every tag model that was trained (a logistic regression over document
   vectors), retrained from its labels; each accepted one is re-applied to the
   archive. A session that cannot be retrained loses its model rather than keep
   scoring the new vectors with weights fitted to the old ones;
4. clustering, which is not re-run here: a run takes minutes and its settings
   are the user's. A finished run stays browsable, and ``assign_new_docs``
   refuses to place new documents into a run from another model.

Nothing is re-fetched or re-chunked. Also the recovery path for a damaged
chunk index, with the same model.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa

from pka.clustering.doc_embeddings import refresh_document_embedding
from pka.db.chunks import document_ids_with_chunks
from pka.db.engine import get_engine
from pka.db.schema import documents, tag_training_sessions
from pka.storage import vector_store
from pka.tag_training import lifecycle
from pka.tag_training.scoring import apply_model_to_documents, parse_parameters

log = logging.getLogger(__name__)


def reembed() -> dict[str, Any]:
    """Rebuild chunk vectors, document vectors and tag models with the configured model."""
    stats: dict[str, Any] = {"embedding_model": None}

    stats.update(vector_store.rebuild_from_chunks())
    stats["embedding_model"] = vector_store.active_model_name()
    log.info("Chunk index rebuilt with %s", stats["embedding_model"])

    stats["documents"] = _refresh_documents()
    stats.update(_retrain_tag_models())
    stats["clustering_stale"] = True
    return stats


def _refresh_documents() -> int:
    doc_ids = sorted(document_ids_with_chunks())
    refreshed = 0
    for i, doc_id in enumerate(doc_ids, 1):
        try:
            if refresh_document_embedding(doc_id, announce=False):
                refreshed += 1
        except Exception:  # noqa: BLE001 - one document must not stop the re-embed
            log.exception("Could not refresh the embedding of document %d", doc_id)
        if i % 500 == 0:
            log.info("Refreshed %d / %d document embeddings", i, len(doc_ids))
    log.info("Refreshed %d / %d document embeddings", refreshed, len(doc_ids))
    return refreshed


def _retrain_tag_models() -> dict[str, Any]:
    """Retrain every session that has a model; re-apply the accepted ones."""
    eng = get_engine()
    with eng.connect() as con:
        sessions = con.execute(
            sa.select(
                tag_training_sessions.c.session_id,
                tag_training_sessions.c.tag,
                tag_training_sessions.c.status,
            ).where(tag_training_sessions.c.model_blob.isnot(None))
        ).fetchall()

    retrained = 0
    dropped: list[str] = []
    for session_id, tag, status in sessions:
        result = lifecycle.train_session(session_id)
        if "error" in result["train_stats"]:
            # train_session leaves the old blob in place when it cannot fit.
            with eng.begin() as con:
                con.execute(
                    tag_training_sessions.update()
                    .where(tag_training_sessions.c.session_id == session_id)
                    .values(model_blob=None)
                )
            dropped.append(tag)
            log.warning(
                "Tag model %r (session %d) could not be retrained: %s",
                tag,
                session_id,
                result["train_stats"]["error"],
            )
            continue
        retrained += 1
        if status == "accepted":
            _reapply(session_id)
    return {"tag_models_retrained": retrained, "tag_models_dropped": dropped}


def _reapply(session_id: int) -> None:
    with get_engine().begin() as con:
        tag, model_blob, raw = con.execute(
            sa.select(
                tag_training_sessions.c.tag,
                tag_training_sessions.c.model_blob,
                tag_training_sessions.c.parameters,
            ).where(tag_training_sessions.c.session_id == session_id)
        ).one()
        doc_ids = [
            r[0]
            for r in con.execute(
                sa.select(documents.c.id).where(documents.c.doc_embedding.isnot(None))
            )
        ]
        threshold = float(parse_parameters(raw).get("threshold", 0.5))
        applied = apply_model_to_documents(con, tag, model_blob, doc_ids, threshold)
    log.info("Tag model %r re-applied: %d document(s) tagged", tag, applied)
