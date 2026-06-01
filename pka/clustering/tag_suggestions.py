"""Cluster tag suggestions and bulk overlay-tag application."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import sqlalchemy as sa

from pka.constants import TagOrigin
from pka.db.schema import cluster_assignments, documents, overlay_tags, source_tags
from pka.ollama_chat import chat_json, resolve_chat_model

log = logging.getLogger(__name__)

COVERAGE_THRESHOLD = 0.3
_TAG_CACHE: dict[tuple[int, int], TagSuggestionResult] = {}
_SLUG_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE_RE = re.compile(r"[\s_]+")
_HYPHEN_RE = re.compile(r"-+")


@dataclass
class TagCandidate:
    tag: str
    source: str   # llm | existing
    coverage: float
    doc_count: int


@dataclass
class TagSuggestionResult:
    suggested_tag: str
    candidates: list[TagCandidate]
    llm_error: str | None = None


def slugify_tag(text: str, max_len: int = 64) -> str:
    """Normalize free text into a lowercase hyphenated tag string."""
    s = _SLUG_RE.sub("", text.lower().strip())
    s = _HYPHEN_RE.sub("-", _SPACE_RE.sub("-", s)).strip("-")
    return s[:max_len]


def _norm_key(tag: str) -> str:
    return slugify_tag(tag) or tag.lower().strip()


def suggest_tag_with_llm(
    titles: list[str],
    existing_tags: list[str],
    label: str,
    model: str | None = None,
) -> tuple[str, str | None]:
    """Ask Ollama for a compact tag. Returns ``(tag, error)``."""
    if not titles:
        return "", "No document titles in cluster"

    prompt = (
        "Suggest ONE short tag for this topic cluster in a personal research library.\n"
        "Rules: 1-2 words, lowercase, hyphens for multi-word tags, no punctuation.\n"
        "Prefer reusing an existing tag from the list when it fits.\n\n"
        f"Cluster topic label: {label or 'unknown'}\n\n"
        "Sample document titles:\n"
        + "\n".join(f"- {t}" for t in titles[:12])
        + "\n\n"
    )
    if existing_tags:
        prompt += "Existing tags on cluster documents:\n" + ", ".join(existing_tags[:20]) + "\n\n"
    prompt += 'Respond with ONLY valid JSON: {"tag": "<tag>"}'

    parsed, err = chat_json(prompt, model=model)
    if err:
        return "", err
    tag = slugify_tag(str(parsed.get("tag", "")))
    if not tag:
        return "", "LLM returned an empty tag"
    return tag, None


def _coverage_candidates(
    con,
    doc_ids: list[int],
    n_docs: int,
) -> list[TagCandidate]:
    if not doc_ids:
        return []
    rows = con.execute(
        sa.select(source_tags.c.tag_string, sa.func.count().label("n"))
        .where(source_tags.c.document_id.in_(doc_ids))
        .group_by(source_tags.c.tag_string)
        .order_by(sa.desc("n"))
        .limit(10)
    ).fetchall()
    out: list[TagCandidate] = []
    for tag, count in rows:
        coverage = round(count / n_docs, 3)
        if coverage >= COVERAGE_THRESHOLD:
            out.append(TagCandidate(
                tag=tag,
                source="existing",
                coverage=coverage,
                doc_count=int(count),
            ))
    return out


def pick_suggested_tag(candidates: list[TagCandidate]) -> str:
    """LLM first, then slugified label, then high-coverage existing tags."""
    for c in candidates:
        if c.source in ("llm", "label"):
            return c.tag
    for c in candidates:
        if c.source == "existing" and c.coverage >= COVERAGE_THRESHOLD:
            return c.tag
    return ""


def build_tag_suggestions(
    con,
    cluster_id: int,
    run_id: int,
    label: str | None,
    *,
    label_model: str | None = None,
    max_candidates: int = 3,
    refresh: bool = False,
    use_llm: bool = True,
) -> TagSuggestionResult:
    """Build tag candidates: LLM (default) plus coverage-based existing tags."""
    cache_key = (cluster_id, run_id)
    if not refresh and cache_key in _TAG_CACHE:
        return _TAG_CACHE[cache_key]

    doc_ids = cluster_document_ids(con, cluster_id, run_id)
    n_docs = len(doc_ids)
    coverage = _coverage_candidates(con, doc_ids, n_docs) if n_docs else []

    llm_tag, llm_error = "", None
    if use_llm:
        titles = titles_for_cluster(con, cluster_id, run_id)
        existing_tag_names = [c.tag for c in coverage]
        llm_tag, llm_error = suggest_tag_with_llm(
            titles, existing_tag_names, label or "", label_model,
        )
    else:
        llm_tag = slugify_tag(label or "") or f"cluster-{cluster_id}"

    merged: dict[str, TagCandidate] = {}
    if llm_tag:
        source = "llm" if use_llm else "label"
        merged[_norm_key(llm_tag)] = TagCandidate(
            tag=llm_tag, source=source, coverage=0.0, doc_count=0,
        )
    for c in coverage:
        key = _norm_key(c.tag)
        if key and key not in merged:
            merged[key] = c

    # Primary candidate first, then existing by coverage
    ordered: list[TagCandidate] = []
    if llm_tag:
        ordered.append(merged[_norm_key(llm_tag)])
    ordered.extend(c for c in coverage if _norm_key(c.tag) in merged and c.source == "existing")

    candidates = ordered[:max_candidates]
    suggested = pick_suggested_tag(ordered)
    result = TagSuggestionResult(
        suggested_tag=suggested, candidates=candidates, llm_error=llm_error,
    )
    _TAG_CACHE[cache_key] = result
    return result


def top_tags_for_cluster(con, cluster_id: int, run_id: int, limit: int = 10) -> list[str]:
    doc_ids = [
        r[0]
        for r in con.execute(
            sa.select(cluster_assignments.c.document_id)
            .where(
                (cluster_assignments.c.cluster_id == cluster_id)
                & (cluster_assignments.c.run_id == run_id)
            )
            .limit(200)
        ).fetchall()
    ]
    if not doc_ids:
        return []
    rows = con.execute(
        sa.select(source_tags.c.tag_string, sa.func.count().label("n"))
        .where(source_tags.c.document_id.in_(doc_ids))
        .group_by(source_tags.c.tag_string)
        .order_by(sa.desc("n"))
        .limit(limit)
    ).fetchall()
    return [r[0] for r in rows]


def titles_for_cluster(
    con,
    cluster_id: int,
    run_id: int,
    limit: int = 20,
) -> list[str]:
    doc_ids = cluster_document_ids(con, cluster_id, run_id)[:limit]
    if not doc_ids:
        return []
    rows = con.execute(
        sa.select(documents.c.title).where(documents.c.id.in_(doc_ids))
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def cluster_document_ids(con, cluster_id: int, run_id: int) -> list[int]:
    return [
        r[0]
        for r in con.execute(
            sa.select(cluster_assignments.c.document_id).where(
                (cluster_assignments.c.cluster_id == cluster_id)
                & (cluster_assignments.c.run_id == run_id)
            )
        ).fetchall()
    ]


def apply_tag_to_documents(
    con,
    doc_ids: list[int],
    tag: str,
    origin: TagOrigin = TagOrigin.LLM,
) -> tuple[int, int]:
    """Insert overlay tag for each document. Returns (applied, skipped)."""
    tag = tag.strip()
    if not tag or not doc_ids:
        return 0, 0

    now = int(time.time())
    applied = skipped = 0
    for doc_id in doc_ids:
        existing = con.execute(
            sa.select(sa.func.count())
            .select_from(overlay_tags)
            .where(
                (overlay_tags.c.document_id == doc_id)
                & (overlay_tags.c.tag == tag)
                & (overlay_tags.c.origin == str(origin))
            )
        ).scalar()
        if existing:
            skipped += 1
            continue
        con.execute(
            sa.text("""
                INSERT INTO overlay_tags
                    (document_id, tag, origin, created_at)
                VALUES (:did, :tag, :origin, :now)
            """),
            {
                "did": doc_id,
                "tag": tag,
                "origin": str(origin),
                "now": now,
            },
        )
        applied += 1
    return applied, skipped
