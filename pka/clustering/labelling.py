"""Step 5: cluster labelling — LLM prompts, the TF-IDF fallback, and relabelling.

Split out of ``engine.py`` (planning/M1_CLUSTERING_ENGINE_SPLIT.md). Together with
``persist.py`` this is one of the two clustering modules that touch the database.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import sqlalchemy as sa

from pka.clustering.run_progress import ClusterRunCancelled, raise_if_cancelled
from pka.clustering.types import L2ClusterBatch
from pka.config import settings as cfg
from pka.db.queries import (
    get_engine,
    sample_cluster_documents,
    sample_cluster_documents_for_clusters,
)
from pka.db.schema import cluster_assignments, clusters

log = logging.getLogger(__name__)


DocSample = tuple[str, str]  # (title, excerpt)


def _format_doc_sample_lines(samples: list[DocSample]) -> str:
    lines: list[str] = []
    for title, excerpt in samples:
        if excerpt.strip():
            lines.append(f"- Title: {title}\n  Excerpt: {excerpt}")
        else:
            lines.append(f"- Title: {title}")
    return "\n".join(lines)


def _regenerate_prompt_suffix(previous_label: str | None) -> str:
    if not previous_label or not previous_label.strip():
        return "\nProvide a fresh topic label and description; avoid generic placeholders.\n"
    return (
        f'\nThe current label is "{previous_label.strip()}".\n'
        "Provide a fresh alternative label and description (not identical wording).\n"
    )


def _json_response_hint(topic_hint: str) -> str:
    return (
        "\nRespond with ONLY valid JSON in this exact format:\n"
        '{"label": "' + topic_hint + '", "description": "<one sentence>"}\n'
        "No explanation, no markdown, just the JSON object."
    )


def _label_via_chat(
    prompt: str,
    model: str | None,
    temperature: float | None,
    *,
    fallback,
    what: str,
) -> tuple[str, str]:
    """Run an LLM labelling prompt; fall back to a TF-IDF label on error."""
    from pka.ollama_chat import chat_json

    parsed, err = chat_json(prompt, model=model, temperature=temperature)
    if err:
        log.warning("LLM %s failed: %s — using fallback", what, err)
        return fallback(), ""
    return parsed.get("label", "Unlabelled"), parsed.get("description", "")


def _label_cluster_with_llm(
    samples: list[DocSample],
    model: str | None = None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    """Call Ollama for a short label and one-sentence description."""
    if not samples:
        return "Unlabelled", ""

    prompt = (
        "You are labelling a topic cluster from a research library.\n"
        "Below are sample documents from the cluster (title and excerpt when available).\n"
        "Use both title and excerpt when present.\n\n"
        + _format_doc_sample_lines(samples)
        + (_regenerate_prompt_suffix(previous_label) if previous_label is not None else "")
        + _json_response_hint("<3-5 word topic name>")
    )
    return _label_via_chat(
        prompt,
        model,
        temperature,
        fallback=lambda: _tfidf_label(samples),
        what="labelling",
    )


def _label_parent_from_children_with_llm(
    child_labels: list[str],
    child_descriptions: list[str],
    model: str | None = None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    if not child_labels:
        return "Unlabelled", ""

    lines: list[str] = []
    for label, desc in zip(child_labels, child_descriptions, strict=False):
        if desc.strip():
            lines.append(f"- {label}: {desc}")
        else:
            lines.append(f"- {label}")

    prompt = (
        "You are naming a broad parent topic that groups several sub-clusters "
        "in a research library.\n"
        "Below are labels (and descriptions) of sub-clusters (do not rename them):\n\n"
        + "\n".join(lines)
        + (_regenerate_prompt_suffix(previous_label) if previous_label is not None else "")
        + _json_response_hint("<3-5 word broader topic name>")
    )
    return _label_via_chat(
        prompt,
        model,
        temperature,
        fallback=lambda: _tfidf_label_from_strings(child_labels),
        what="parent labelling",
    )


_TFIDF_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "in",
    "and",
    "to",
    "for",
    "with",
    "on",
    "is",
    "are",
    "by",
    "from",
    "at",
    "as",
    "that",
    "this",
    "its",
    "it",
}


def _tfidf_label_from_strings(texts: list[str], n_words: int = 4) -> str:
    from collections import Counter

    words: list[str] = []
    for t in texts:
        words.extend(re.findall(r"[a-z]{3,}", t.lower()))
    freq = Counter(w for w in words if w not in _TFIDF_STOPWORDS)
    top = [w for w, _ in freq.most_common(n_words)]
    return " / ".join(top) if top else "Unlabelled"


def _tfidf_label(samples: list[DocSample], n_words: int = 4) -> str:
    return _tfidf_label_from_strings(
        [f"{title} {excerpt}" for title, excerpt in samples],
        n_words,
    )


def _label_one_cluster(
    cid: int,
    samples: list[DocSample],
    skip_labelling: bool,
    chat_model: str | None,
) -> tuple[int, str, str]:
    if skip_labelling:
        return cid, _tfidf_label(samples), ""
    label, desc = _label_cluster_with_llm(samples, chat_model)
    return cid, label, desc


def _label_clusters(
    cluster_docs: dict[int, list[int]],
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[dict[int, str], dict[int, str]]:
    """Label clusters from document title + content samples (L2 subclusters)."""
    if not cluster_docs:
        return {}, {}

    eng = get_engine()
    with eng.connect() as con:
        samples_map = sample_cluster_documents_for_clusters(con, cluster_docs)

    label_map: dict[int, str] = {}
    desc_map: dict[int, str] = {}
    workers = 1 if skip_labelling else max(1, cfg.cluster_label_workers)
    cids = sorted(cluster_docs.keys())

    if workers == 1:
        for cid in cids:
            if run_id is not None:
                raise_if_cancelled(run_id)
            cid, label, desc = _label_one_cluster(
                cid,
                samples_map[cid],
                skip_labelling,
                chat_model,
            )
            label_map[cid] = label
            desc_map[cid] = desc
        return label_map, desc_map

    # Not a context manager: on cancellation we shut down without waiting for
    # already-dispatched futures, so a stop request isn't stuck behind a
    # ThreadPoolExecutor.__exit__() that blocks on shutdown(wait=True).
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(
                _label_one_cluster,
                cid,
                samples_map[cid],
                skip_labelling,
                chat_model,
            ): cid
            for cid in cids
        }
        for fut in as_completed(futures):
            if run_id is not None:
                raise_if_cancelled(run_id)
            cid, label, desc = fut.result()
            label_map[cid] = label
            desc_map[cid] = desc
            log.debug("Cluster %d → %s", cid, label)
    except ClusterRunCancelled:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return label_map, desc_map


def _label_l1_clusters(
    l1_cluster_docs: dict[int, list[int]],
    l2_batches: list[L2ClusterBatch],
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[dict[int, str], dict[int, str]]:
    """L1 labels from L2 children when available, else title+content on L1 docs."""
    batch_by_parent = {b.parent_l1_id: b for b in l2_batches}
    label_map: dict[int, str] = {}
    desc_map: dict[int, str] = {}
    eng = get_engine()

    with eng.connect() as con:
        fallback_samples = sample_cluster_documents_for_clusters(con, l1_cluster_docs)

    for l1_cid in sorted(l1_cluster_docs.keys()):
        if run_id is not None:
            raise_if_cancelled(run_id)
        batch = batch_by_parent.get(l1_cid)
        if batch and batch.label_map:
            child_labels = [batch.label_map[c] for c in sorted(batch.label_map)]
            child_descs = [batch.desc_map.get(c, "") for c in sorted(batch.label_map)]
            if skip_labelling:
                label_map[l1_cid] = _tfidf_label_from_strings(child_labels)
                desc_map[l1_cid] = ""
            else:
                label, desc = _label_parent_from_children_with_llm(
                    child_labels,
                    child_descs,
                    chat_model,
                )
                label_map[l1_cid] = label
                desc_map[l1_cid] = desc
        else:
            samples = fallback_samples.get(l1_cid, [])
            if skip_labelling:
                label_map[l1_cid] = _tfidf_label(samples)
                desc_map[l1_cid] = ""
            else:
                label, desc = _label_cluster_with_llm(samples, chat_model)
                label_map[l1_cid] = label
                desc_map[l1_cid] = desc
    return label_map, desc_map


def _label_cluster_from_docs(
    doc_ids: list[int],
    skip_labelling: bool,
    chat_model: str | None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    eng = get_engine()
    with eng.connect() as con:
        samples = sample_cluster_documents(con, doc_ids)
    if skip_labelling:
        return _tfidf_label(samples), ""
    return _label_cluster_with_llm(
        samples,
        chat_model,
        temperature=temperature,
        previous_label=previous_label,
    )


def _label_l1_db_cluster_from_children(
    con,
    cluster_id: int,
    run_id: int,
    chat_model: str | None,
    *,
    temperature: float | None = None,
    previous_label: str | None = None,
) -> tuple[str, str]:
    child_rows = con.execute(
        sa.select(clusters.c.label, clusters.c.description).where(
            (clusters.c.parent_cluster_id == cluster_id)
            & (clusters.c.run_id == run_id)
            & (clusters.c.level == 2)
        )
    ).fetchall()
    if len(child_rows) >= 2:
        child_labels = [r[0] or "" for r in child_rows]
        child_descs = [r[1] or "" for r in child_rows]
        return _label_parent_from_children_with_llm(
            child_labels,
            child_descs,
            chat_model,
            temperature=temperature,
            previous_label=previous_label,
        )
    return "", ""


def relabel_single_cluster(
    cluster_id: int,
    run_id: int,
    *,
    label_model: str | None = None,
    skip_labelling: bool = False,
) -> tuple[str, str]:
    """Re-run labelling for one persisted cluster; returns (label, description).

    Uses higher Ollama temperature and asks for a fresh label. L2 subcluster
    names in the database are never changed when regenerating an L1 parent.
    """
    from pka.ollama_chat import resolve_chat_model

    chat_model = resolve_chat_model(label_model)
    regen_temp = cfg.cluster_regenerate_temperature
    eng = get_engine()

    with eng.connect() as con:
        row = con.execute(
            sa.select(clusters.c.level, clusters.c.label, clusters.c.is_noise).where(
                (clusters.c.cluster_id == cluster_id) & (clusters.c.run_id == run_id)
            )
        ).fetchone()
        if not row:
            raise ValueError(f"Cluster {cluster_id} not found in run {run_id}")
        if row[2]:
            raise ValueError(f"Cluster {cluster_id} is the noise bucket and is not labelled")

        level = int(row[0] or 1)
        previous_label = row[1] or ""
        doc_ids = [
            r[0]
            for r in con.execute(
                sa.select(cluster_assignments.c.document_id).where(
                    (cluster_assignments.c.cluster_id == cluster_id)
                    & (cluster_assignments.c.run_id == run_id)
                    & (cluster_assignments.c.level == level)
                )
            ).fetchall()
        ]

        label, desc = "", ""
        if level == 2:
            label, desc = _label_cluster_from_docs(
                doc_ids,
                skip_labelling,
                chat_model,
                temperature=regen_temp,
                previous_label=previous_label,
            )
        else:
            label, desc = _label_l1_db_cluster_from_children(
                con,
                cluster_id,
                run_id,
                chat_model,
                temperature=regen_temp,
                previous_label=previous_label,
            )
            if not label:
                label, desc = _label_cluster_from_docs(
                    doc_ids,
                    skip_labelling,
                    chat_model,
                    temperature=regen_temp,
                    previous_label=previous_label,
                )

    now = int(time.time())
    with eng.begin() as con:
        con.execute(
            clusters.update()
            .where(clusters.c.cluster_id == cluster_id)
            .values(label=label, description=desc, created_at=now)
        )
    log.info("Relabelled cluster #%d → %s", cluster_id, label)
    return label, desc


def relabel_run_clusters(
    run_id: int,
    label_model: str | None = None,
) -> None:
    """Replace placeholder labels with LLM labels (L2 first, then L1 from children)."""
    from pka.ollama_chat import resolve_chat_model

    eng = get_engine()
    chat_model = resolve_chat_model(label_model)
    now = int(time.time())

    updates: list[tuple[int, str, str]] = []

    with eng.connect() as con:
        l2_rows = con.execute(
            sa.select(clusters.c.cluster_id).where(
                (clusters.c.run_id == run_id) & (clusters.c.level == 2)
            )
        ).fetchall()
        for (cid,) in l2_rows:
            doc_ids = [
                r[0]
                for r in con.execute(
                    sa.select(cluster_assignments.c.document_id).where(
                        (cluster_assignments.c.cluster_id == cid)
                        & (cluster_assignments.c.run_id == run_id)
                        & (cluster_assignments.c.level == 2)
                    )
                ).fetchall()
            ]
            label, desc = _label_cluster_from_docs(doc_ids, False, chat_model)
            updates.append((cid, label, desc))

        l1_rows = con.execute(
            sa.select(clusters.c.cluster_id).where(
                (clusters.c.run_id == run_id)
                & (clusters.c.level == 1)
                # The noise bucket keeps its fixed label: it is not a topic, so
                # there is nothing for the LLM to summarise (and it is typically
                # the largest member set in the run).
                & (clusters.c.is_noise == False)  # noqa: E712 — SQLA expression
            )
        ).fetchall()
        for (cid,) in l1_rows:
            label, desc = _label_l1_db_cluster_from_children(con, cid, run_id, chat_model)
            if not label:
                doc_ids = [
                    r[0]
                    for r in con.execute(
                        sa.select(cluster_assignments.c.document_id).where(
                            (cluster_assignments.c.cluster_id == cid)
                            & (cluster_assignments.c.run_id == run_id)
                            & (cluster_assignments.c.level == 1)
                        )
                    ).fetchall()
                ]
                label, desc = _label_cluster_from_docs(doc_ids, False, chat_model)
            updates.append((cid, label, desc))

    with eng.begin() as con:
        for cid, label, desc in updates:
            con.execute(
                clusters.update()
                .where(clusters.c.cluster_id == cid)
                .values(label=label, description=desc, created_at=now)
            )

    log.info("Relabelled %d clusters for run #%d", len(updates), run_id)
