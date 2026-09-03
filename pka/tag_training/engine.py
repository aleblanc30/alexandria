"""Binary classifier training and uncertainty sampling on doc embeddings."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import numpy as np
import sqlalchemy as sa

from pka.clustering.doc_embeddings import (
    embedding_to_blob,
    load_cached_embeddings,
)
from pka.db.queries import get_engine
from pka.db.schema import documents, tag_training_labels

if TYPE_CHECKING:
    from sklearn.linear_model import LogisticRegression

log = logging.getLogger(__name__)

# Rows that may train the classifier. Raw scores write only to ``overlay_tags``.
TRAINING_LABEL_SOURCES = ("seed", "user", "auto", "pseudo", "pseudo_llm")

MIN_POSITIVES_WARN = 5
DEFAULT_PARAMETERS: dict[str, Any] = {
    "threshold": 0.5,
    "queue_batch_size": 10,
    "pseudo_label_high": 0.95,
    "pseudo_label_low": 0.05,
    "pseudo_llm_batch_size": 20,
    "pseudo_llm_seed_max": 8,
    "pseudo_llm_negatives": 5,
}


def default_parameters() -> dict[str, Any]:
    return dict(DEFAULT_PARAMETERS)


def serialize_model(model: LogisticRegression) -> str:
    payload = {
        "coef": model.coef_.tolist(),
        "intercept": model.intercept_.tolist(),
        "classes": model.classes_.tolist(),
    }
    return json.dumps(payload)


def deserialize_model(blob: str) -> LogisticRegression:
    # Imported here, not at module scope: sklearn costs ~1s to import and the API
    # only ever reaches it through a training session (see planning audit P-2).
    from sklearn.linear_model import LogisticRegression

    data = json.loads(blob)
    model = LogisticRegression(max_iter=1000)
    model.classes_ = np.array(data["classes"], dtype=np.int64)
    model.coef_ = np.array(data["coef"], dtype=np.float64)
    model.intercept_ = np.array(data["intercept"], dtype=np.float64)
    return model


def _labeled_doc_ids(
    con: sa.Connection,
    session_id: int,
) -> set[int]:
    """Doc ids with training-table labels (excludes stray prediction rows)."""
    q = sa.select(tag_training_labels.c.document_id).where(
        (tag_training_labels.c.session_id == session_id)
        & tag_training_labels.c.source.in_(TRAINING_LABEL_SOURCES)
    )
    rows = con.execute(q).fetchall()
    return {r[0] for r in rows}


def _session_labeled_doc_ids(con: sa.Connection, session_id: int) -> set[int]:
    """All doc ids with any label row for this session (any source)."""
    rows = con.execute(
        sa.select(tag_training_labels.c.document_id).where(
            tag_training_labels.c.session_id == session_id
        )
    ).fetchall()
    return {r[0] for r in rows}


def unlabeled_doc_ids(session_id: int) -> list[int]:
    """Documents with no label row in this session."""
    eng = get_engine()
    with eng.connect() as con:
        labeled = _session_labeled_doc_ids(con, session_id)
        rows = con.execute(sa.select(documents.c.id)).fetchall()
    return [r[0] for r in rows if r[0] not in labeled]


def unlabeled_doc_ids_with_embeddings(session_id: int) -> list[int]:
    eng = get_engine()
    with eng.connect() as con:
        labeled = _session_labeled_doc_ids(con, session_id)
        rows = con.execute(
            sa.select(documents.c.id).where(documents.c.doc_embedding.isnot(None))
        ).fetchall()
    return [r[0] for r in rows if r[0] not in labeled]


def pseudo_labels_from_model(
    session_id: int,
    model_blob: str,
    *,
    high: float = 0.95,
    low: float = 0.05,
) -> list[tuple[int, int, float]]:
    """High-confidence pseudo labels: (doc_id, label, probability)."""
    doc_ids = unlabeled_doc_ids_with_embeddings(session_id)
    scores = predict_proba(model_blob, doc_ids)
    out: list[tuple[int, int, float]] = []
    for doc_id, prob in scores.items():
        if prob >= high:
            out.append((doc_id, 1, prob))
        elif prob <= low:
            out.append((doc_id, 0, prob))
    return out


def load_label_matrix(
    session_id: int,
) -> tuple[np.ndarray, np.ndarray, list[int], list[int]]:
    """Return X, y, doc_ids used, doc_ids skipped (missing embedding)."""
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(
                tag_training_labels.c.document_id,
                tag_training_labels.c.label,
            ).where(
                (tag_training_labels.c.session_id == session_id)
                & tag_training_labels.c.source.in_(TRAINING_LABEL_SOURCES)
            )
        ).fetchall()
    if not rows:
        return np.empty((0, 0)), np.array([]), [], []

    doc_ids = [r[0] for r in rows]
    labels = [int(r[1]) for r in rows]
    found, missing = load_cached_embeddings(doc_ids)
    used_ids = [did for did in doc_ids if did in found]
    if not used_ids:
        return np.empty((0, 0)), np.array([]), [], missing

    y = np.array([labels[doc_ids.index(did)] for did in used_ids], dtype=np.int64)
    from sklearn.preprocessing import normalize

    X = np.stack([found[did] for did in used_ids], axis=0)
    X = normalize(X, norm="l2", axis=1)
    skipped = [did for did in doc_ids if did in missing]
    return X, y, used_ids, skipped


def train_classifier(session_id: int) -> tuple[str | None, dict[str, Any]]:
    """Fit logistic regression; return (model_blob, stats)."""
    X, y, used_ids, skipped = load_label_matrix(session_id)
    stats: dict[str, Any] = {
        "labeled_count": len(used_ids) + len(skipped),
        "train_count": len(used_ids),
        "skipped_missing_embedding": len(skipped),
        "positive_count": int((y == 1).sum()) if len(y) else 0,
        "negative_count": int((y == 0).sum()) if len(y) else 0,
        "warn_small_seed": bool(len(y) and (y == 1).sum() < MIN_POSITIVES_WARN),
    }
    if len(used_ids) < 2 or len(np.unique(y)) < 2:
        stats["error"] = "Need at least one positive and one negative with embeddings"
        return None, stats

    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(max_iter=1000)
    model.fit(X, y)
    stats["accuracy"] = float(model.score(X, y))
    return serialize_model(model), stats


def predict_proba(model_blob: str, doc_ids: list[int]) -> dict[int, float]:
    """Return P(positive) per document id."""
    if not doc_ids:
        return {}
    model = deserialize_model(model_blob)
    found, _ = load_cached_embeddings(doc_ids)
    if not found:
        return {}
    from sklearn.preprocessing import normalize

    ids = list(found.keys())
    X = normalize(np.stack([found[did] for did in ids], axis=0), norm="l2", axis=1)
    probs = model.predict_proba(X)
    pos_idx = list(model.classes_).index(1)
    return {did: float(probs[i, pos_idx]) for i, did in enumerate(ids)}


def uncertainty_queue(
    session_id: int,
    model_blob: str,
    *,
    batch_size: int = 10,
) -> list[dict[str, Any]]:
    """Documents with embeddings not yet labeled, sorted by uncertainty."""
    eng = get_engine()
    with eng.connect() as con:
        labeled = _session_labeled_doc_ids(con, session_id)
        rows = con.execute(
            sa.select(documents.c.id, documents.c.title).where(
                documents.c.doc_embedding.isnot(None)
            )
        ).fetchall()

    candidates = [(r[0], r[1] or "") for r in rows if r[0] not in labeled]
    if not candidates:
        return []

    doc_ids = [c[0] for c in candidates]
    scores = predict_proba(model_blob, doc_ids)
    ranked = sorted(
        candidates,
        key=lambda item: abs(scores.get(item[0], 0.5) - 0.5),
    )
    out: list[dict[str, Any]] = []
    for doc_id, title in ranked[:batch_size]:
        p = scores.get(doc_id, 0.5)
        out.append(
            {
                "doc_id": doc_id,
                "title": title,
                "probability": p,
                "uncertainty": abs(p - 0.5),
            }
        )
    return out


def score_all_unlabeled(
    session_id: int, model_blob: str, threshold: float
) -> list[tuple[int, float]]:
    """Return (doc_id, probability) for unlabeled docs above threshold."""
    doc_ids = unlabeled_doc_ids_with_embeddings(session_id)
    scores = predict_proba(model_blob, doc_ids)
    return [(did, p) for did, p in scores.items() if p >= threshold]


def set_doc_embedding_for_test(doc_id: int, vec: np.ndarray) -> None:
    """Test helper: persist a synthetic embedding blob."""
    eng = get_engine()
    blob = embedding_to_blob(vec.astype(np.float32))
    with eng.begin() as con:
        con.execute(documents.update().where(documents.c.id == doc_id).values(doc_embedding=blob))
