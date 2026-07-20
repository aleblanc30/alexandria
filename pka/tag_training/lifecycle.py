"""Tag training session CRUD, train, accept, and overlay application."""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

import sqlalchemy as sa

from pka.clustering.cluster_tags import slugify_tag
from pka.constants import TagOrigin
from pka.db.queries import get_engine
from pka.db.schema import (
    documents,
    overlay_tags,
    source_tags,
    tag_training_labels,
    tag_training_sessions,
)
from pka.tag_training.engine import (
    TRAINING_LABEL_SOURCES,
    default_parameters,
    pseudo_labels_from_model,
    train_classifier,
    uncertainty_queue,
    unlabeled_doc_ids,
)
from pka.tag_training.llm_classifier import (
    classify_document_for_tag,
    negative_prompt_samples,
    seed_collection_samples,
)

log = logging.getLogger(__name__)


def _now() -> int:
    return int(time.time())


def _parse_parameters(raw: str | None) -> dict[str, Any]:
    if not raw:
        return default_parameters()
    try:
        data = json.loads(raw)
        out = default_parameters()
        out.update(data)
        return out
    except json.JSONDecodeError:
        return default_parameters()


def document_ids_for_source_tag(source_tag: str) -> list[int]:
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(source_tags.c.document_id)
            .where(source_tags.c.tag_string == source_tag)
            .distinct()
        ).fetchall()
    return [r[0] for r in rows]


def _upsert_labels(
    con: sa.Connection,
    session_id: int,
    labels: list[tuple[int, int]],
    source: str,
) -> None:
    if source not in TRAINING_LABEL_SOURCES:
        raise ValueError(
            f"Invalid label source '{source}'; use pseudo/pseudo_llm for model-assisted labels"
        )
    deduped = dict(labels)  # last occurrence wins, matching sequential upsert order
    if not deduped:
        return
    now = _now()
    existing = {
        r[0]: r[1]
        for r in con.execute(
            sa.select(tag_training_labels.c.document_id, tag_training_labels.c.id).where(
                (tag_training_labels.c.session_id == session_id)
                & tag_training_labels.c.document_id.in_(list(deduped))
            )
        ).fetchall()
    }
    updates = [
        {"row_id": existing[did], "label": label, "source": source, "created_at": now}
        for did, label in deduped.items() if did in existing
    ]
    inserts = [
        {
            "session_id": session_id, "document_id": did,
            "label": label, "source": source, "created_at": now,
        }
        for did, label in deduped.items() if did not in existing
    ]
    if updates:
        con.execute(
            tag_training_labels.update()
            .where(tag_training_labels.c.id == sa.bindparam("row_id"))
            .values(
                label=sa.bindparam("label"),
                source=sa.bindparam("source"),
                created_at=sa.bindparam("created_at"),
            ),
            updates,
        )
    if inserts:
        con.execute(tag_training_labels.insert(), inserts)


def _bootstrap_negatives_if_needed(con: sa.Connection, session_id: int, n: int = 5) -> int:
    """Random negative labels (source=auto) when the seed set has no negatives yet."""
    rows = con.execute(
        sa.select(tag_training_labels.c.label).where(
            (tag_training_labels.c.session_id == session_id)
            & tag_training_labels.c.source.in_(TRAINING_LABEL_SOURCES)
        )
    ).fetchall()
    if any(int(r[0]) == 0 for r in rows):
        return 0

    labeled = {
        r[0]
        for r in con.execute(
            sa.select(tag_training_labels.c.document_id).where(
                tag_training_labels.c.session_id == session_id
            )
        ).fetchall()
    }
    pool = [
        r[0]
        for r in con.execute(
            sa.select(documents.c.id).where(documents.c.doc_embedding.isnot(None))
        ).fetchall()
        if r[0] not in labeled
    ]
    if not pool:
        return 0
    sample = random.sample(pool, min(n, len(pool)))
    _upsert_labels(con, session_id, [(did, 0) for did in sample], "auto")
    return len(sample)


def create_session(
    tag: str,
    labels: list[dict[str, int]],
    *,
    provenance: dict[str, Any] | None = None,
    bootstrap_negatives: bool = True,
) -> dict[str, Any]:
    """Create session with initial L0 labels. Each item: doc_id, label (0|1)."""
    tag_slug = slugify_tag(tag) or tag.strip().lower()
    if not tag_slug:
        raise ValueError("Tag name is required")

    eng = get_engine()
    now = _now()
    pairs = [(int(item["doc_id"]), int(item["label"])) for item in labels]
    if not pairs:
        raise ValueError("At least one labeled document is required")

    with eng.begin() as con:
        res = con.execute(
            tag_training_sessions.insert().values(
                tag=tag_slug,
                status="labeling",
                parameters=json.dumps(default_parameters()),
                provenance=json.dumps(provenance) if provenance else None,
                created_at=now,
            )
        )
        session_id = int(res.inserted_primary_key[0])
        _upsert_labels(con, session_id, pairs, "seed")
        added = 0
        if bootstrap_negatives:
            added = _bootstrap_negatives_if_needed(con, session_id)

    train_session(session_id)
    result = get_session(session_id)
    if added:
        result["bootstrap_negatives_added"] = added
    return result


def create_session_from_source_tag(source_tag: str, target_tag: str) -> dict[str, Any]:
    doc_ids = document_ids_for_source_tag(source_tag)
    if not doc_ids:
        raise ValueError(f"No documents found for source tag '{source_tag}'")
    labels = [{"doc_id": did, "label": 1} for did in doc_ids]
    return create_session(
        target_tag,
        labels,
        provenance={"from_source_tag": source_tag},
    )


def get_session(session_id: int) -> dict[str, Any]:
    eng = get_engine()
    with eng.connect() as con:
        row = con.execute(
            sa.select(tag_training_sessions).where(
                tag_training_sessions.c.session_id == session_id
            )
        ).mappings().fetchone()
        if not row:
            raise LookupError(f"Session {session_id} not found")

        counts = con.execute(
            sa.select(
                tag_training_labels.c.label,
                sa.func.count(),
            )
            .where(
                (tag_training_labels.c.session_id == session_id)
                & tag_training_labels.c.source.in_(TRAINING_LABEL_SOURCES)
            )
            .group_by(tag_training_labels.c.label)
        ).fetchall()
        pos = neg = 0
        for label_val, n in counts:
            if int(label_val) == 1:
                pos = int(n)
            else:
                neg = int(n)

    params = _parse_parameters(row["parameters"])
    provenance = None
    if row["provenance"]:
        try:
            provenance = json.loads(row["provenance"])
        except json.JSONDecodeError:
            provenance = None

    train_stats = None
    if row["model_blob"]:
        try:
            train_stats = json.loads(row.get("notes") or "{}")
        except json.JSONDecodeError:
            train_stats = None

    return {
        "session_id": row["session_id"],
        "tag": row["tag"],
        "status": row["status"],
        "created_at": row["created_at"],
        "accepted_at": row["accepted_at"],
        "parameters": params,
        "provenance": provenance,
        "positive_count": pos,
        "negative_count": neg,
        "has_model": bool(row["model_blob"]),
        "train_stats": train_stats,
    }


def list_sessions() -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(tag_training_sessions.c.session_id)
            .order_by(tag_training_sessions.c.session_id.desc())
        ).fetchall()
    return [get_session(r[0]) for r in rows]


def add_user_labels(session_id: int, labels: list[dict[str, int]]) -> dict[str, Any]:
    session = get_session(session_id)
    if session["status"] != "labeling":
        raise ValueError("Resume the session before adding labels")
    pairs = [(int(item["doc_id"]), int(item["label"])) for item in labels]
    eng = get_engine()
    with eng.begin() as con:
        _upsert_labels(con, session_id, pairs, "user")
    train_session(session_id)
    return get_session(session_id)


def train_session(session_id: int) -> dict[str, Any]:
    model_blob, stats = train_classifier(session_id)
    eng = get_engine()
    with eng.begin() as con:
        if model_blob:
            con.execute(
                tag_training_sessions.update()
                .where(tag_training_sessions.c.session_id == session_id)
                .values(model_blob=model_blob, notes=json.dumps(stats))
            )
        else:
            con.execute(
                tag_training_sessions.update()
                .where(tag_training_sessions.c.session_id == session_id)
                .values(notes=json.dumps(stats))
            )
    out = get_session(session_id)
    out["train_stats"] = stats
    return out


def _set_learned_overlay(
    con: sa.Connection,
    doc_id: int,
    tag: str,
    confidence: float,
    now: int,
) -> None:
    from pka.clustering.cluster_tags import insert_overlay_tags

    # Delete-then-insert so confidence reflects the latest score.
    _clear_learned_overlay(con, doc_id, tag)
    insert_overlay_tags(
        con, [doc_id], tag, TagOrigin.LEARNED, confidence=float(confidence),
    )


def _clear_learned_overlay(con: sa.Connection, doc_id: int, tag: str) -> None:
    con.execute(
        overlay_tags.delete().where(
            (overlay_tags.c.document_id == doc_id)
            & (overlay_tags.c.tag == tag)
            & (overlay_tags.c.origin == str(TagOrigin.LEARNED))
        )
    )


def _apply_model_to_documents(
    con: sa.Connection,
    tag: str,
    model_blob: str,
    doc_ids: list[int],
    threshold: float,
    now: int,
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
            _set_learned_overlay(con, doc_id, tag, prob, now)
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
        now = _now()
        for tag, model_blob, params_raw in rows:
            params = _parse_parameters(params_raw)
            threshold = float(params.get("threshold", 0.5))
            applied += _apply_model_to_documents(
                con, tag, model_blob, [doc_id], threshold, now,
            )
    if applied:
        log.debug("Applied %d learned tag(s) to document %d", applied, doc_id)
    return applied


def resume_session(session_id: int) -> dict[str, Any]:
    """Reopen an accepted session for more active learning."""
    session = get_session(session_id)
    if session["status"] not in ("accepted", "archived"):
        if session["status"] == "labeling":
            return session
        raise ValueError(f"Cannot resume session in status '{session['status']}'")
    if not session["has_model"]:
        raise ValueError("Train the model before resuming")

    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            tag_training_sessions.update()
            .where(tag_training_sessions.c.session_id == session_id)
            .values(status="labeling", accepted_at=None)
        )
    train_session(session_id)
    return get_session(session_id)


def find_resumable_session_for_tag(tag: str) -> dict[str, Any] | None:
    """Latest accepted or in-progress session for a tag slug, if any."""
    tag_slug = slugify_tag(tag) or tag.strip().lower()
    eng = get_engine()
    with eng.connect() as con:
        row = con.execute(
            sa.select(tag_training_sessions.c.session_id)
            .where(
                (tag_training_sessions.c.tag == tag_slug)
                & tag_training_sessions.c.status.in_(("labeling", "accepted"))
            )
            .order_by(tag_training_sessions.c.session_id.desc())
            .limit(1)
        ).fetchone()
    if not row:
        return None
    return get_session(row[0])


def _require_labeling_session(session_id: int) -> dict[str, Any]:
    session = get_session(session_id)
    if session["status"] != "labeling":
        raise ValueError("Session must be in labeling status")
    return session


def _fetch_model_row(session_id: int):
    eng = get_engine()
    with eng.connect() as con:
        return con.execute(
            sa.select(tag_training_sessions.c.model_blob, tag_training_sessions.c.parameters)
            .where(tag_training_sessions.c.session_id == session_id)
        ).fetchone()


def _ensure_model_row(session_id: int):
    """Return (model_blob, parameters), training at most once. None when untrainable."""
    row = _fetch_model_row(session_id)
    if row and row[0]:
        return row
    train_session(session_id)
    row = _fetch_model_row(session_id)
    if row and row[0]:
        return row
    return None


def apply_pseudo_labels_model(session_id: int) -> dict[str, Any]:
    """Add high-confidence model pseudo-labels (default P≥0.95 / P≤0.05) and retrain."""
    _require_labeling_session(session_id)
    row = _ensure_model_row(session_id)
    if row is None:
        raise ValueError(
            "Cannot train a classifier for this session: it needs at least one "
            "positive and one negative label with document embeddings"
        )

    params = _parse_parameters(row[1])
    high = float(params.get("pseudo_label_high", 0.95))
    low = float(params.get("pseudo_label_low", 0.05))
    candidates = pseudo_labels_from_model(session_id, row[0], high=high, low=low)
    pairs = [(did, label) for did, label, _ in candidates]

    added_pos = sum(1 for _, label, _ in candidates if label == 1)
    added_neg = sum(1 for _, label, _ in candidates if label == 0)

    if pairs:
        with get_engine().begin() as con:
            _upsert_labels(con, session_id, pairs, "pseudo")
        train_session(session_id)

    out = get_session(session_id)
    out["pseudo_label_result"] = {
        "mode": "model",
        "added_positive": added_pos,
        "added_negative": added_neg,
        "pseudo_label_high": high,
        "pseudo_label_low": low,
    }
    return out


def apply_pseudo_labels_llm(
    session_id: int,
    *,
    batch_size: int | None = None,
) -> dict[str, Any]:
    """One-shot LLM on a random unlabeled subset; prompt uses tag, seed collection, random negatives."""
    session = _require_labeling_session(session_id)
    params = session["parameters"]
    limit = batch_size if batch_size is not None else int(params.get("pseudo_llm_batch_size", 20))
    seed_max = int(params.get("pseudo_llm_seed_max", 8))
    neg_n = int(params.get("pseudo_llm_negatives", 5))
    tag = session["tag"]

    seed_samples = seed_collection_samples(session_id, n_max=seed_max)
    if not seed_samples:
        raise ValueError("Need at least one seed document in the collection for LLM pseudo-labeling")

    neg_samples, neg_source = negative_prompt_samples(session_id, n_max=neg_n)

    from pka.db.queries import _doc_title_excerpts

    pool = unlabeled_doc_ids(session_id)
    if not pool:
        out = get_session(session_id)
        out["pseudo_label_result"] = {
            "mode": "llm",
            "added_positive": 0,
            "added_negative": 0,
            "errors": 0,
            "batch_size": 0,
            "pool_size": 0,
        }
        return out

    doc_ids = random.sample(pool, min(limit, len(pool)))
    eng = get_engine()
    added_pos = added_neg = 0
    errors = 0
    pairs: list[tuple[int, int]] = []

    with eng.connect() as con:
        by_id = _doc_title_excerpts(con, doc_ids)

    for doc_id in doc_ids:
        title, excerpt = by_id.get(doc_id, ("Untitled", ""))
        label, err = classify_document_for_tag(
            tag,
            seed_samples=seed_samples,
            negative_samples=neg_samples,
            title=title,
            excerpt=excerpt,
            negative_source=neg_source,
        )
        if err or label is None:
            errors += 1
            log.warning("LLM pseudo-label failed for doc %d: %s", doc_id, err)
            continue
        pairs.append((doc_id, label))
        if label == 1:
            added_pos += 1
        else:
            added_neg += 1

    if pairs:
        with eng.begin() as con:
            _upsert_labels(con, session_id, pairs, "pseudo_llm")
        train_session(session_id)

    out = get_session(session_id)
    out["pseudo_label_result"] = {
        "mode": "llm",
        "added_positive": added_pos,
        "added_negative": added_neg,
        "errors": errors,
        "batch_size": len(doc_ids),
        "pool_size": len(pool),
        "seed_examples": len(seed_samples),
        "negative_examples": len(neg_samples),
        "negative_source": neg_source,
    }
    return out


def get_queue(session_id: int) -> list[dict[str, Any]]:
    session = get_session(session_id)
    if session["status"] != "labeling":
        return []

    row = _ensure_model_row(session_id)
    if row is None:
        # Untrainable (single-class labels or no embeddings) — nothing to queue.
        return []

    params = _parse_parameters(row[1])
    batch = int(params.get("queue_batch_size", 10))
    return uncertainty_queue(session_id, row[0], batch_size=batch)


def accept_session(session_id: int) -> dict[str, Any]:
    session = get_session(session_id)
    if not session["has_model"]:
        raise ValueError("Train the model before accepting")

    eng = get_engine()
    with eng.begin() as con:
        row = con.execute(
            sa.select(tag_training_sessions.c.model_blob, tag_training_sessions.c.tag)
            .where(tag_training_sessions.c.session_id == session_id)
        ).fetchone()
        if not row or not row[0]:
            raise ValueError("No model on session")

        tag = row[1]
        params = _parse_parameters(
            con.execute(
                sa.select(tag_training_sessions.c.parameters).where(
                    tag_training_sessions.c.session_id == session_id
                )
            ).scalar()
        )
        threshold = float(params.get("threshold", 0.5))

        con.execute(
            tag_training_sessions.update()
            .where(
                (tag_training_sessions.c.tag == tag)
                & (tag_training_sessions.c.status == "accepted")
            )
            .values(status="archived")
        )

        now = _now()
        con.execute(
            tag_training_sessions.update()
            .where(tag_training_sessions.c.session_id == session_id)
            .values(status="accepted", accepted_at=now)
        )

        all_rows = con.execute(
            sa.select(documents.c.id).where(documents.c.doc_embedding.isnot(None))
        ).fetchall()
        doc_ids = [r[0] for r in all_rows]
        _apply_model_to_documents(con, tag, row[0], doc_ids, threshold, now)

    return get_session(session_id)


def archive_session(session_id: int) -> dict[str, Any]:
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            tag_training_sessions.update()
            .where(tag_training_sessions.c.session_id == session_id)
            .values(status="archived")
        )
    return get_session(session_id)
