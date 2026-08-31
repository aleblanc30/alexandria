"""One-shot LLM binary classification for tag-training pseudo-labels."""

from __future__ import annotations

import logging
import random

import sqlalchemy as sa

from pka.config import settings as cfg
from pka.db.queries import _doc_title_excerpts, get_engine
from pka.db.schema import documents, tag_training_labels
from pka.ollama_chat import chat_json
from pka.tag_training.engine import _session_labeled_doc_ids

log = logging.getLogger(__name__)

DocSample = tuple[str, str]


def _format_samples(samples: list[DocSample], *, empty_msg: str) -> str:
    if not samples:
        return empty_msg
    lines: list[str] = []
    for title, excerpt in samples:
        if excerpt.strip():
            lines.append(f"- Title: {title}\n  Excerpt: {excerpt[:500]}")
        else:
            lines.append(f"- Title: {title}")
    return "\n".join(lines)


def seed_collection_samples(session_id: int, *, n_max: int = 8) -> list[DocSample]:
    """Seed-collection positives (``source=seed``, ``label=1``) for the LLM prompt."""
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(tag_training_labels.c.document_id)
            .where(
                (tag_training_labels.c.session_id == session_id)
                & (tag_training_labels.c.source == "seed")
                & (tag_training_labels.c.label == 1)
            )
            .order_by(tag_training_labels.c.id)
        ).fetchall()
        pos_ids = [r[0] for r in rows][:n_max]
        if not pos_ids:
            return []
        by_id = _doc_title_excerpts(con, pos_ids)
    return [by_id[d] for d in pos_ids if d in by_id]


def user_negative_prompt_samples(session_id: int, *, n_max: int = 5) -> list[DocSample]:
    """User-confirmed negatives (``source=user``, ``label=0``) for the LLM prompt."""
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(tag_training_labels.c.document_id)
            .where(
                (tag_training_labels.c.session_id == session_id)
                & (tag_training_labels.c.source == "user")
                & (tag_training_labels.c.label == 0)
            )
            .order_by(tag_training_labels.c.id.desc())
        ).fetchall()
        neg_ids = [r[0] for r in rows][:n_max]
        if not neg_ids:
            return []
        by_id = _doc_title_excerpts(con, neg_ids)
    return [by_id[d] for d in neg_ids if d in by_id]


def random_negative_prompt_samples(session_id: int, *, n: int = 5) -> list[DocSample]:
    """Random documents outside the session labels to contrast the seed collection."""
    eng = get_engine()
    with eng.connect() as con:
        labeled = _session_labeled_doc_ids(con, session_id)
        pool = [
            r[0] for r in con.execute(sa.select(documents.c.id)).fetchall() if r[0] not in labeled
        ]
        if not pool:
            return []
        sample_ids = random.sample(pool, min(n, len(pool)))
        by_id = _doc_title_excerpts(con, sample_ids)
    return [by_id[d] for d in sample_ids if d in by_id]


def negative_prompt_samples(session_id: int, *, n_max: int = 5) -> tuple[list[DocSample], str]:
    """Negative examples for the prompt: user negatives when present, else random."""
    user_negs = user_negative_prompt_samples(session_id, n_max=n_max)
    if user_negs:
        return user_negs, "user"
    return random_negative_prompt_samples(session_id, n=n_max), "random"


def classify_document_for_tag(
    tag: str,
    *,
    seed_samples: list[DocSample],
    negative_samples: list[DocSample],
    title: str,
    excerpt: str,
    model: str | None = None,
    negative_source: str = "random",
) -> tuple[int | None, str | None]:
    """Return (label 0|1, error_message). None label on failure or invalid JSON."""
    if negative_source == "user":
        neg_heading = "User-confirmed negatives — documents that should NOT have this tag:\n"
    else:
        neg_heading = "Random contrast examples — documents that should NOT have this tag:\n"
    prompt = (
        "You are labeling documents for a personal research library.\n"
        f'Target tag: "{tag}"\n'
        "Decide whether the document below should receive this tag (1) or not (0).\n\n"
        "Seed collection — documents that should have this tag:\n"
        + _format_samples(seed_samples, empty_msg="(none provided)\n")
        + "\n\n"
        + neg_heading
        + _format_samples(negative_samples, empty_msg="(none provided)\n")
        + "\n\nDocument to classify:\n"
    )
    if excerpt.strip():
        prompt += f"- Title: {title}\n  Excerpt: {excerpt[:800]}\n"
    else:
        prompt += f"- Title: {title}\n"
    prompt += (
        '\nRespond with ONLY valid JSON: {"label": 0} or {"label": 1}\nNo markdown, no explanation.'
    )

    parsed, err = chat_json(
        prompt,
        model=model,
        temperature=0.0,
        timeout=cfg.tag_training_llm_chat_timeout_seconds,
    )
    if err:
        return None, err
    if not isinstance(parsed, dict):
        return None, "invalid JSON object"
    raw = parsed.get("label")
    try:
        label = int(raw)
    except (TypeError, ValueError):
        return None, f"invalid label field: {raw!r}"
    if label not in (0, 1):
        return None, f"label must be 0 or 1, got {label}"
    return label, None
