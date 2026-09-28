"""Scoring documents against accepted tag models, and the learned overlay it writes.

The half of tag training that ingestion reaches: ``apply_learned_tags_for_document``
runs as the last step of ingesting every document. Session lifecycle (create,
train, accept, archive), which only the API drives, stays in
:mod:`pka.tag_training.lifecycle`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa

from pka.constants import TagOrigin
from pka.db.engine import get_engine
from pka.db.schema import documents, overlay_tags, tag_training_sessions
from pka.tag_training.engine import default_parameters

log = logging.getLogger(__name__)


def parse_parameters(raw: str | None) -> dict[str, Any]:
    """A session's stored ``parameters`` JSON over the defaults; the defaults if unreadable."""
    if not raw:
        return default_parameters()
    try:
        data = json.loads(raw)
        out = default_parameters()
        out.update(data)
        return out
    except json.JSONDecodeError:
        return default_parameters()


def _set_learned_overlay(
    con: sa.Connection,
    doc_id: int,
    tag: str,
    confidence: float,
) -> None:
    from pka.clustering.cluster_tags import insert_overlay_tags

    # Delete-then-insert so confidence reflects the latest score.
    _clear_learned_overlay(con, doc_id, tag)
    insert_overlay_tags(
        con,
        [doc_id],
        tag,
        TagOrigin.LEARNED,
        confidence=float(confidence),
    )


def _clear_learned_overlay(con: sa.Connection, doc_id: int, tag: str) -> None:
    con.execute(
        overlay_tags.delete().where(
            (overlay_tags.c.document_id == doc_id)
            & (overlay_tags.c.tag == tag)
            & (overlay_tags.c.origin == str(TagOrigin.LEARNED))
        )
    )


def apply_model_to_documents(
    con: sa.Connection,
    tag: str,
    model_blob: str,
    doc_ids: list[int],
    threshold: float,
) -> int:
    """Apply or clear learned overlay for each doc_id. Returns tags written."""
    from pka.tag_training.engine import predict_proba

    if not doc_ids:
        return 0
    scores = predict_proba(model_blob, doc_ids)
    applied = 0
    for doc_id in doc_ids:
        prob = scores.get(doc_id)
        if prob is None:
            continue
        if prob >= threshold:
            _set_learned_overlay(con, doc_id, tag, prob)
            applied += 1
        else:
            _clear_learned_overlay(con, doc_id, tag)
    return applied


def apply_learned_tags_for_document(doc_id: int) -> int:
    """Score one document against all accepted models; update overlay tags."""
    eng = get_engine()
    applied = 0
    with eng.begin() as con:
        blob = con.execute(
            sa.select(documents.c.doc_embedding).where(documents.c.id == doc_id)
        ).scalar()
        if not blob:
            return 0

        rows = con.execute(
            sa.select(
                tag_training_sessions.c.tag,
                tag_training_sessions.c.model_blob,
                tag_training_sessions.c.parameters,
            ).where(
                (tag_training_sessions.c.status == "accepted")
                & tag_training_sessions.c.model_blob.isnot(None)
            )
        ).fetchall()
        for tag, model_blob, params_raw in rows:
            params = parse_parameters(params_raw)
            threshold = float(params.get("threshold", 0.5))
            applied += apply_model_to_documents(
                con,
                tag,
                model_blob,
                [doc_id],
                threshold,
            )
    if applied:
        log.debug("Applied %d learned tag(s) to document %d", applied, doc_id)
    return applied
