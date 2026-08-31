"""
Clustering engine: PCA-reduced embeddings → HDBSCAN → supervised UMAP viz → LLM labels.

Default pipeline (``cluster_space=pca``):
  1. Aggregate chunk embeddings per document (mean pooling; SQLite cache when available).
  2. PCA → 50d; hold ``pca_matrix`` for L1 and L2 HDBSCAN (cosine metric).
  3. L2 subclusters labelled via LLM from document title + content excerpts.
  4. L1 labels from L2 child labels when subclusters exist, else title + content.
  5. Supervised UMAP → 2d scatter (``y`` = L1 labels; noise = -1).

Legacy ``cluster_space=legacy_umap`` retains the old UMAP→HDBSCAN path for comparison.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
import sqlalchemy as sa
from sklearn.decomposition import PCA
from sklearn.preprocessing import normalize

from pka.clustering.run_progress import raise_if_cancelled
from pka.config import settings as cfg
from pka.db.queries import (
    get_engine,
    sample_cluster_documents,
    sample_cluster_documents_for_clusters,
)
from pka.db.schema import (
    cluster_assignments,
    cluster_runs,
    clusters,
)
from pka.json_utils import parse_llm_json as _parse_llm_json  # noqa: F401  (re-export for tests)

log = logging.getLogger(__name__)

ALGORITHM_PCA = "HDBSCAN-hierarchical-pca"
ALGORITHM_LEGACY = "HDBSCAN-hierarchical"


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class L2ClusterBatch:
    """Level-2 clustering result scoped to one L1 HDBSCAN cluster."""

    parent_l1_id: int
    doc_ids: list[int]
    labels: np.ndarray
    label_map: dict[int, str]
    desc_map: dict[int, str]


@dataclass
class ClusterRunResult:
    run_id: int
    n_clusters: int
    n_noise: int
    cluster_labels: dict[int, str]
    cluster_descriptions: dict[int, str]
    umap_2d: np.ndarray  # shape (n_docs, 2)
    doc_ids: list[int]
    assignments: dict[int, int]  # {doc_id: L1 hdbscan_label} (-1 = noise)
    diagnostics: dict


@dataclass
class _StepTimer:
    timings_ms: dict[str, float] = field(default_factory=dict)

    def record(self, name: str, start: float) -> None:
        self.timings_ms[name] = round((time.perf_counter() - start) * 1000, 1)


# ── Step 1: aggregate document embeddings ────────────────────────────────────


def _mean_pool_from_chroma(
    vector_ids: list[str],
    metadatas: list[dict],
    embeddings: dict[str, list[float]],
    source_filter: list[str] | None,
) -> tuple[list[int], np.ndarray]:
    doc_vecs: dict[int, list[list[float]]] = {}
    for vid, meta in zip(vector_ids, metadatas, strict=False):
        emb = embeddings.get(vid)
        if emb is None:
            continue
        doc_id = int(meta.get("document_id", -1))
        if doc_id == -1:
            continue
        if source_filter and meta.get("source") not in source_filter:
            continue
        doc_vecs.setdefault(doc_id, []).append(emb)

    if not doc_vecs:
        raise ValueError("No embeddings found after filtering.")

    doc_ids = sorted(doc_vecs.keys())
    matrix = np.array(
        [np.mean(doc_vecs[d], axis=0) for d in doc_ids],
        dtype=np.float32,
    )
    return doc_ids, matrix


def _load_document_embeddings(
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> tuple[list[int], np.ndarray]:
    """Mean-pool chunk embeddings per document; prefer SQLite cache when present."""
    from pka.clustering.doc_embeddings import load_cached_embeddings, refresh_document_embedding
    from pka.storage.vector_store import (
        fetch_embeddings_by_ids,
        fetch_records,
    )

    meta_page = fetch_records(include=["metadatas"])
    vector_ids = meta_page["ids"]
    metadatas = meta_page["metadatas"]
    if not vector_ids:
        raise ValueError("Vector store is empty — run ingestion first.")

    if run_id is not None:
        raise_if_cancelled(run_id)

    # Collect doc ids from metadata (respecting source filter)
    candidate_doc_ids: set[int] = set()
    for meta in metadatas:
        doc_id = int(meta.get("document_id", -1))
        if doc_id == -1:
            continue
        if source_filter and meta.get("source") not in source_filter:
            continue
        candidate_doc_ids.add(doc_id)

    if not candidate_doc_ids:
        raise ValueError("No embeddings found after filtering.")

    sorted_ids = sorted(candidate_doc_ids)
    cached, missing = load_cached_embeddings(sorted_ids)

    if missing:
        log.info(
            "Loading %d vectors from Chroma (%d docs without cache)…",
            len(vector_ids),
            len(missing),
        )
        embeddings, corrupt_ids = fetch_embeddings_by_ids(vector_ids)
        if corrupt_ids:
            affected_docs = {
                int(metadatas[i].get("document_id", -1))
                for i, vid in enumerate(vector_ids)
                if vid in corrupt_ids and metadatas[i].get("document_id") is not None
            }
            log.warning(
                "Skipping %d unreadable Chroma vectors (%d documents)",
                len(corrupt_ids),
                len(affected_docs),
            )
        doc_ids_chroma, matrix_chroma = _mean_pool_from_chroma(
            vector_ids,
            metadatas,
            embeddings,
            source_filter,
        )
        chroma_map = dict(zip(doc_ids_chroma, matrix_chroma, strict=False))
        for did in missing:
            if did in chroma_map:
                cached[did] = chroma_map[did]
                refresh_document_embedding(did)
    else:
        log.info("Loaded cached embeddings for %d documents", len(cached))

    doc_ids = sorted(cached.keys())
    matrix = np.stack([cached[d] for d in doc_ids], axis=0)
    log.info("Aggregated embeddings for %d documents", len(doc_ids))
    return doc_ids, matrix


# ── Step 2: PCA reduction ─────────────────────────────────────────────────────


def _run_pca(
    matrix: np.ndarray,
    n_components: int = 50,
) -> tuple[np.ndarray, float]:
    """Project to ``n_components`` dims. Returns (pca_matrix, variance_explained_sum)."""
    n_docs, n_features = matrix.shape
    n_comp = min(n_components, n_docs - 1, n_features)
    n_comp = max(2, n_comp)
    log.info("Running PCA (n_components=%d)…", n_comp)
    reducer = PCA(n_components=n_comp, random_state=42)
    pca_matrix = reducer.fit_transform(matrix).astype(np.float32)
    var_sum = float(reducer.explained_variance_ratio_.sum())
    log.info("PCA done. Shape: %s  variance explained: %.1f%%", pca_matrix.shape, var_sum * 100)
    return pca_matrix, var_sum


# ── Step 3: supervised UMAP (viz only) ───────────────────────────────────────


def _run_supervised_umap(
    matrix: np.ndarray,
    labels: np.ndarray,
    n_neighbors: int = 15,
    min_dist: float = 0.1,
) -> np.ndarray:
    """2d supervised UMAP for scatter plot; ``labels`` may contain -1 (noise)."""
    try:
        import umap
    except ImportError as e:
        raise ImportError("umap-learn is required: pip install umap-learn") from e

    n_docs = len(matrix)
    nn = max(2, min(n_neighbors, max(2, n_docs - 1)))
    log.info("Running supervised UMAP 2d (n_neighbors=%d)…", nn)
    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=nn,
        min_dist=min_dist,
        metric="cosine",
        random_state=42,
        n_jobs=-1,
    )
    reduced_2d = reducer.fit_transform(matrix, y=labels)
    log.info("Supervised UMAP done.")
    return reduced_2d.astype(np.float32)


def _run_umap_legacy(
    matrix: np.ndarray,
    n_components_cluster: int = 5,
    n_neighbors: int = 15,
    min_dist: float = 0.1,
    *,
    compute_2d: bool = True,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Legacy UMAP path for ``cluster_space=legacy_umap``."""
    try:
        import umap
    except ImportError as e:
        raise ImportError("umap-learn is required: pip install umap-learn") from e

    n_docs = len(matrix)
    nn = max(2, min(n_neighbors, max(2, n_docs - 1)))
    n_comp = min(n_components_cluster, max(2, n_docs - 1))

    log.info("Running legacy UMAP (n_neighbors=%d, n_components=%d)…", nn, n_comp)

    reducer_nd = umap.UMAP(
        n_components=n_comp,
        n_neighbors=nn,
        min_dist=min_dist,
        metric="cosine",
        random_state=42,
        n_jobs=-1,
    )
    reduced_nd = reducer_nd.fit_transform(matrix)

    reduced_2d = None
    if compute_2d:
        reducer_2d = umap.UMAP(
            n_components=2,
            n_neighbors=nn,
            min_dist=min_dist,
            metric="cosine",
            random_state=42,
            n_jobs=-1,
        )
        reduced_2d = reducer_2d.fit_transform(matrix)

    return reduced_nd.astype(np.float32), (
        reduced_2d.astype(np.float32) if reduced_2d is not None else None
    )


# ── Step 4: HDBSCAN clustering ────────────────────────────────────────────────


def adaptive_cluster_params(n_docs: int) -> tuple[int, int, int]:
    """Derive HDBSCAN/UMAP params that target moderately sized clusters."""
    if n_docs < 8:
        return max(2, n_docs // 3), 2, max(2, n_docs - 1)

    target_clusters = max(4, min(12, round(n_docs**0.5)))
    min_cluster_size = max(3, n_docs // (target_clusters * 2))
    min_samples = max(2, min_cluster_size // 2)
    n_neighbors = max(5, min(30, n_docs // 4))
    return min_cluster_size, min_samples, n_neighbors


def _normalize_for_cosine(matrix: np.ndarray) -> np.ndarray:
    return normalize(matrix, norm="l2", axis=1).astype(np.float32)


def _run_hdbscan(
    reduced: np.ndarray,
    min_cluster_size: int = 5,
    min_samples: int | None = None,
    *,
    metric: str = "cosine",
) -> np.ndarray:
    """HDBSCAN clustering.

    When ``metric="cosine"``, rows are L2-normalized and euclidean distance is used
    (equivalent to cosine on unit vectors; supported by both hdbscan and sklearn).
    """
    if metric == "cosine":
        data = _normalize_for_cosine(reduced)
    else:
        data = reduced.astype(np.float32, copy=False)

    hdb_kwargs = dict(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_method="leaf",
    )

    try:
        import hdbscan as hdbscan_lib

        clusterer = hdbscan_lib.HDBSCAN(prediction_data=True, **hdb_kwargs)
        labels = clusterer.fit_predict(data)
        backend = "hdbscan pkg"
    except ImportError:
        from sklearn.cluster import HDBSCAN as SkHDBSCAN

        sk_kwargs = dict(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples or 1,
        )
        try:
            clusterer = SkHDBSCAN(cluster_selection_method="leaf", metric="euclidean", **sk_kwargs)
        except TypeError:
            clusterer = SkHDBSCAN(**sk_kwargs)
        labels = clusterer.fit_predict(data)
        backend = "sklearn"

    log.info(
        "HDBSCAN (%s): %d clusters, %d noise",
        backend,
        len(set(labels)) - (1 if -1 in labels else 0),
        (labels == -1).sum(),
    )
    return labels


# ── Step 5: LLM cluster labelling ─────────────────────────────────────────────

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

    with ThreadPoolExecutor(max_workers=workers) as pool:
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
            sa.select(clusters.c.level, clusters.c.label).where(
                (clusters.c.cluster_id == cluster_id) & (clusters.c.run_id == run_id)
            )
        ).fetchone()
        if not row:
            raise ValueError(f"Cluster {cluster_id} not found in run {run_id}")

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
                (clusters.c.run_id == run_id) & (clusters.c.level == 1)
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


# ── Step 6: persist to DB ─────────────────────────────────────────────────────


def create_run_placeholder() -> int:
    """Insert a run row immediately so the UI can show status=running."""
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        res = con.execute(
            cluster_runs.insert().values(
                timestamp=now,
                algorithm=ALGORITHM_PCA,
                parameters=json.dumps({}),
                accepted=False,
                status="running",
            )
        )
        return res.inserted_primary_key[0]


def set_run_status(run_id: int, status: str, *, notes: str | None = None) -> None:
    values: dict = {"status": status}
    if notes is not None:
        values["notes"] = notes
    with get_engine().begin() as con:
        con.execute(cluster_runs.update().where(cluster_runs.c.run_id == run_id).values(**values))


def _write_hierarchical_clusters(
    con,
    run_id: int,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    now: int,
) -> tuple[int, int, int]:
    """Persist L1/L2 clusters and assignments. Returns (n_l1, n_l2, n_assignments)."""

    def _insert_cluster(label, description, level, parent_cluster_id):
        res = con.execute(
            clusters.insert().values(
                label=label,
                description=description,
                created_at=now,
                run_id=run_id,
                level=level,
                parent_cluster_id=parent_cluster_id,
            )
        )
        return res.inserted_primary_key[0]

    def _collect_assignments(rows, doc_ids_iter, raw_labels, db_ids, level):
        for doc_id, raw_label in zip(doc_ids_iter, raw_labels, strict=False):
            db_cid = db_ids.get(raw_label, -1)
            if db_cid == -1:
                continue
            rows.append(
                {
                    "document_id": doc_id,
                    "cluster_id": db_cid,
                    "run_id": run_id,
                    "score": None,
                    "assigned_at": now,
                    "level": level,
                }
            )

    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    l1_db_ids: dict[int, int] = {
        cid: _insert_cluster(
            l1_label_map.get(cid, f"Cluster {cid}"),
            l1_desc_map.get(cid, ""),
            1,
            None,
        )
        for cid in l1_unique
    }

    assignment_rows: list[dict] = []
    _collect_assignments(assignment_rows, doc_ids, l1_labels.tolist(), l1_db_ids, 1)

    n_l2 = 0
    for batch in l2_batches:
        parent_db_id = l1_db_ids.get(batch.parent_l1_id)
        if parent_db_id is None:
            continue
        l2_unique = sorted(set(batch.labels.tolist()) - {-1})
        l2_db_ids: dict[int, int] = {}
        for l2_cid in l2_unique:
            l2_db_ids[l2_cid] = _insert_cluster(
                batch.label_map.get(l2_cid, f"Subcluster {l2_cid}"),
                batch.desc_map.get(l2_cid, ""),
                2,
                parent_db_id,
            )
            n_l2 += 1

        _collect_assignments(
            assignment_rows,
            batch.doc_ids,
            batch.labels.tolist(),
            l2_db_ids,
            2,
        )

    if assignment_rows:
        con.execute(cluster_assignments.insert(), assignment_rows)

    return len(l1_unique), n_l2, len(assignment_rows)


def _run_level2_pass_core(
    doc_ids: list[int],
    l1_labels: np.ndarray,
    *,
    compute_l2_labels,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[list[L2ClusterBatch], int, int]:
    """Subcluster each L1 group; ``compute_l2_labels`` produces per-group L2 labels."""
    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    l2_batches: list[L2ClusterBatch] = []
    l2_noise = 0
    l2_skipped = 0

    for l1_cid in l1_unique:
        member_doc_ids = [doc_ids[i] for i, lbl in enumerate(l1_labels.tolist()) if lbl == l1_cid]
        n_sub = len(member_doc_ids)
        sub_mcs, sub_ms, sub_nn = adaptive_cluster_params(n_sub)
        if n_sub < sub_mcs:
            l2_skipped += 1
            continue

        if run_id is not None:
            raise_if_cancelled(run_id)

        l2_labels = compute_l2_labels(member_doc_ids, sub_mcs, sub_ms, sub_nn)
        l2_unique = sorted(set(l2_labels.tolist()) - {-1})
        l2_noise += int((l2_labels == -1).sum())

        if len(l2_unique) < 2:
            l2_skipped += 1
            continue

        l2_cluster_docs: dict[int, list[int]] = {c: [] for c in l2_unique}
        for doc_id, lbl in zip(member_doc_ids, l2_labels.tolist(), strict=False):
            if lbl != -1:
                l2_cluster_docs[lbl].append(doc_id)

        l2_label_map, l2_desc_map = _label_clusters(
            l2_cluster_docs,
            skip_labelling,
            chat_model,
            run_id,
        )
        l2_batches.append(
            L2ClusterBatch(
                parent_l1_id=l1_cid,
                doc_ids=member_doc_ids,
                labels=l2_labels,
                label_map=l2_label_map,
                desc_map=l2_desc_map,
            )
        )

    return l2_batches, l2_noise, l2_skipped


def _run_level2_pass(
    cluster_matrix: np.ndarray,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    *,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
    hdbscan_metric: str = "cosine",
) -> tuple[list[L2ClusterBatch], int, int]:
    """Run HDBSCAN inside each L1 cluster on ``cluster_matrix`` slices."""
    doc_id_to_idx = {d: i for i, d in enumerate(doc_ids)}

    def _compute(member_doc_ids, sub_mcs, sub_ms, sub_nn):
        sub_matrix = cluster_matrix[[doc_id_to_idx[d] for d in member_doc_ids]]
        return _run_hdbscan(sub_matrix, sub_mcs, sub_ms, metric=hdbscan_metric)

    return _run_level2_pass_core(
        doc_ids,
        l1_labels,
        compute_l2_labels=_compute,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )


def _run_level2_pass_legacy(
    matrix: np.ndarray,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    *,
    n_components: int,
    min_dist: float,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[list[L2ClusterBatch], int, int]:
    """Legacy L2: local UMAP + HDBSCAN per L1 cluster."""
    doc_id_to_idx = {d: i for i, d in enumerate(doc_ids)}

    def _compute(member_doc_ids, sub_mcs, sub_ms, sub_nn):
        sub_matrix = matrix[[doc_id_to_idx[d] for d in member_doc_ids]]
        sub_reduced_nd, _ = _run_umap_legacy(
            sub_matrix,
            n_components_cluster=min(n_components, max(2, len(member_doc_ids) - 1)),
            n_neighbors=sub_nn,
            min_dist=min_dist,
            compute_2d=False,
        )
        return _run_hdbscan(sub_reduced_nd, sub_mcs, sub_ms, metric="euclidean")

    return _run_level2_pass_core(
        doc_ids,
        l1_labels,
        compute_l2_labels=_compute,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )


def _build_umap_records(
    doc_ids: list[int],
    l1_labels: np.ndarray,
    umap_2d: np.ndarray,
) -> list[dict]:
    return [
        {
            "doc_id": int(doc_ids[i]),
            "x": round(float(umap_2d[i, 0]), 5),
            "y": round(float(umap_2d[i, 1]), 5),
            "cluster_id": int(l1_labels[i]),
        }
        for i in range(len(doc_ids))
    ]


def _commit_run(
    write_run_row,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    umap_2d: np.ndarray,
    *,
    verb: str,
) -> int:
    """Insert/update the run row (via ``write_run_row``) and write its clusters."""
    eng = get_engine()
    now = int(time.time())
    umap_records = _build_umap_records(doc_ids, l1_labels, umap_2d)

    with eng.begin() as con:
        run_id = write_run_row(con, now, umap_records)
        n_l1, n_l2, n_assign = _write_hierarchical_clusters(
            con,
            run_id,
            doc_ids,
            l1_labels,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            now,
        )

    log.info(
        "%s run #%d (%d L1, %d L2 clusters, %d assignments, %d UMAP points)",
        verb,
        run_id,
        n_l1,
        n_l2,
        n_assign,
        len(umap_records),
    )
    return run_id


def _finalize_run(
    run_id: int,
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    algorithm: str,
    params: dict,
    umap_2d: np.ndarray,
) -> None:
    """Fill in a placeholder run row created at trigger time."""

    def _write(con, now, umap_records) -> int:
        con.execute(
            cluster_runs.update()
            .where(cluster_runs.c.run_id == run_id)
            .values(
                timestamp=now,
                algorithm=algorithm,
                parameters=json.dumps(params),
                accepted=False,
                status="finished",
                umap_points=json.dumps(umap_records),
            )
        )
        return run_id

    _commit_run(
        _write,
        doc_ids,
        l1_labels,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        umap_2d,
        verb="Finalized",
    )


def _persist_run(
    doc_ids: list[int],
    l1_labels: np.ndarray,
    l1_label_map: dict[int, str],
    l1_desc_map: dict[int, str],
    l2_batches: list[L2ClusterBatch],
    algorithm: str,
    params: dict,
    umap_2d: np.ndarray,
) -> int:
    """Write ``cluster_runs``, ``clusters``, and ``cluster_assignments``."""

    def _write(con, now, umap_records) -> int:
        res = con.execute(
            cluster_runs.insert().values(
                timestamp=now,
                algorithm=algorithm,
                parameters=json.dumps(params),
                accepted=False,
                status="finished",
                umap_points=json.dumps(umap_records),
            )
        )
        return res.inserted_primary_key[0]

    return _commit_run(
        _write,
        doc_ids,
        l1_labels,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        umap_2d,
        verb="Persisted",
    )


def _build_cluster_docs(
    doc_ids: list[int],
    labels: np.ndarray,
) -> dict[int, list[int]]:
    unique = sorted(set(labels.tolist()) - {-1})
    cluster_docs: dict[int, list[int]] = {c: [] for c in unique}
    for doc_id, lbl in zip(doc_ids, labels.tolist(), strict=False):
        if lbl != -1:
            cluster_docs[lbl].append(doc_id)
    return cluster_docs


def _run_pca_pipeline(
    doc_ids: list[int],
    matrix: np.ndarray,
    *,
    mcs: int,
    ms: int | None,
    nn: int,
    min_dist: float,
    pca_components: int,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
    timer: _StepTimer,
) -> tuple[
    np.ndarray,
    np.ndarray,
    dict[int, str],
    dict[int, str],
    list[L2ClusterBatch],
    int,
    int,
    int,
    float,
    dict,
]:
    """PCA → HDBSCAN L1/L2 → labels → supervised UMAP. Returns pipeline artifacts."""
    t0 = time.perf_counter()
    pca_matrix, var_sum = _run_pca(matrix, pca_components)
    timer.record("pca_ms", t0)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l1_labels = _run_hdbscan(pca_matrix, mcs, ms, metric="cosine")
    timer.record("hdbscan_l1_ms", t0)

    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    n_l1 = len(l1_unique)
    n_noise = int((l1_labels == -1).sum())
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l2_batches, l2_noise, l2_skipped = _run_level2_pass(
        pca_matrix,
        doc_ids,
        l1_labels,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
        hdbscan_metric="cosine",
    )
    timer.record("hdbscan_l2_ms", t0)

    t0 = time.perf_counter()
    l1_label_map, l1_desc_map = _label_l1_clusters(
        l1_cluster_docs,
        l2_batches,
        skip_labelling,
        chat_model,
        run_id,
    )
    timer.record("label_l1_ms", t0)

    t0 = time.perf_counter()
    reduced_2d = _run_supervised_umap(pca_matrix, l1_labels, nn, min_dist)
    timer.record("umap_viz_ms", t0)

    params = dict(
        cluster_space="pca",
        pca_components=pca_components,
        pca_variance=round(var_sum, 4),
        cluster_metric="cosine",
        viz="supervised_umap",
        min_cluster_size=mcs,
        min_samples=ms,
        n_neighbors=nn,
        min_dist=min_dist,
        cluster_selection="leaf",
        hierarchical=True,
        l2_skipped_parents=l2_skipped,
    )
    return (
        l1_labels,
        reduced_2d,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        n_l1,
        n_noise,
        l2_noise,
        var_sum,
        params,
    )


def _run_legacy_pipeline(
    doc_ids: list[int],
    matrix: np.ndarray,
    *,
    mcs: int,
    ms: int | None,
    nn: int,
    min_dist: float,
    n_components: int,
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
    timer: _StepTimer,
) -> tuple[
    np.ndarray,
    np.ndarray,
    dict[int, str],
    dict[int, str],
    list[L2ClusterBatch],
    int,
    int,
    int,
    dict,
]:
    t0 = time.perf_counter()
    reduced_nd, reduced_2d = _run_umap_legacy(
        matrix,
        n_components_cluster=n_components,
        n_neighbors=nn,
        min_dist=min_dist,
    )
    timer.record("umap_ms", t0)
    assert reduced_2d is not None

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l1_labels = _run_hdbscan(reduced_nd, mcs, ms, metric="euclidean")
    timer.record("hdbscan_l1_ms", t0)

    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    n_l1 = len(l1_unique)
    n_noise = int((l1_labels == -1).sum())
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    l2_batches, l2_noise, l2_skipped = _run_level2_pass_legacy(
        matrix,
        doc_ids,
        l1_labels,
        n_components=n_components,
        min_dist=min_dist,
        skip_labelling=skip_labelling,
        chat_model=chat_model,
        run_id=run_id,
    )
    timer.record("hdbscan_l2_ms", t0)

    t0 = time.perf_counter()
    l1_label_map, l1_desc_map = _label_l1_clusters(
        l1_cluster_docs,
        l2_batches,
        skip_labelling,
        chat_model,
        run_id,
    )
    timer.record("label_l1_ms", t0)

    params = dict(
        cluster_space="legacy_umap",
        min_cluster_size=mcs,
        min_samples=ms,
        n_neighbors=nn,
        min_dist=min_dist,
        n_components=n_components,
        cluster_selection="leaf",
        hierarchical=True,
        l2_skipped_parents=l2_skipped,
    )
    return (
        l1_labels,
        reduced_2d,
        l1_label_map,
        l1_desc_map,
        l2_batches,
        n_l1,
        n_noise,
        l2_noise,
        params,
    )


def _spawn_async_relabel(run_id: int, label_model: str | None) -> None:
    import threading

    def _worker() -> None:
        try:
            relabel_run_clusters(run_id, label_model=label_model)
        except Exception:
            log.exception("Async relabel failed for run #%d", run_id)

    threading.Thread(target=_worker, daemon=True, name=f"relabel-{run_id}").start()


# ── Public entry point ────────────────────────────────────────────────────────


def run_clustering(
    min_cluster_size: int | None = None,
    min_samples: int | None = None,
    n_neighbors: int | None = None,
    min_dist: float = 0.1,
    n_components: int = 5,
    pca_components: int | None = None,
    label_model: str | None = None,
    skip_labelling: bool = False,
    async_labelling: bool | None = None,
    cluster_space: str | None = None,
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> ClusterRunResult:
    """Full clustering pipeline. Returns diagnostics for the UI acceptance panel."""
    from pka.ollama_chat import resolve_chat_model

    timer = _StepTimer()
    space = cluster_space or cfg.cluster_space
    pca_comp = pca_components if pca_components is not None else cfg.cluster_pca_components
    do_async = async_labelling if async_labelling is not None else cfg.cluster_async_labelling
    defer_llm = do_async and not skip_labelling

    if run_id is not None:
        raise_if_cancelled(run_id)

    t0 = time.perf_counter()
    doc_ids, matrix = _load_document_embeddings(source_filter, run_id=run_id)
    timer.record("load_embeddings_ms", t0)
    n_docs = len(doc_ids)

    auto_mcs, auto_ms, auto_nn = adaptive_cluster_params(n_docs)
    mcs = min_cluster_size if min_cluster_size is not None else auto_mcs
    ms = min_samples if min_samples is not None else auto_ms
    nn = n_neighbors if n_neighbors is not None else auto_nn
    chat_model = resolve_chat_model(label_model)

    if space == "legacy_umap":
        algorithm = ALGORITHM_LEGACY
        (
            l1_labels,
            reduced_2d,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            n_l1,
            n_noise,
            l2_noise,
            params,
        ) = _run_legacy_pipeline(
            doc_ids,
            matrix,
            mcs=mcs,
            ms=ms,
            nn=nn,
            min_dist=min_dist,
            n_components=n_components,
            skip_labelling=skip_labelling or defer_llm,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        params["adaptive"] = min_cluster_size is None
    else:
        algorithm = ALGORITHM_PCA
        (
            l1_labels,
            reduced_2d,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            n_l1,
            n_noise,
            l2_noise,
            _var,
            params,
        ) = _run_pca_pipeline(
            doc_ids,
            matrix,
            mcs=mcs,
            ms=ms,
            nn=nn,
            min_dist=min_dist,
            pca_components=pca_comp,
            skip_labelling=skip_labelling or defer_llm,
            chat_model=chat_model,
            run_id=run_id,
            timer=timer,
        )
        params["adaptive"] = min_cluster_size is None

    n_l2 = sum(len(set(b.labels.tolist()) - {-1}) for b in l2_batches)
    l1_cluster_docs = _build_cluster_docs(doc_ids, l1_labels)

    if run_id is not None:
        raise_if_cancelled(run_id)
        _finalize_run(
            run_id,
            doc_ids,
            l1_labels,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            algorithm=algorithm,
            params=params,
            umap_2d=reduced_2d,
        )
        persisted_id = run_id
    else:
        persisted_id = _persist_run(
            doc_ids,
            l1_labels,
            l1_label_map,
            l1_desc_map,
            l2_batches,
            algorithm=algorithm,
            params=params,
            umap_2d=reduced_2d,
        )

    if defer_llm:
        _spawn_async_relabel(persisted_id, label_model)

    l1_sizes = {cid: len(l1_cluster_docs[cid]) for cid in sorted(l1_cluster_docs)}
    n_clusters_total = n_l1 + n_l2
    diagnostics = {
        "n_clusters": n_clusters_total,
        "n_l1_clusters": n_l1,
        "n_l2_clusters": n_l2,
        "n_noise": n_noise,
        "n_l2_noise": l2_noise,
        "l2_skipped_parents": params.get("l2_skipped_parents", 0),
        "cluster_sizes": l1_sizes,
        "size_min": min(l1_sizes.values()) if l1_sizes else 0,
        "size_max": max(l1_sizes.values()) if l1_sizes else 0,
        "size_mean": round(sum(l1_sizes.values()) / len(l1_sizes), 1) if l1_sizes else 0,
        "timings_ms": timer.timings_ms,
        "async_labelling": defer_llm,
    }

    return ClusterRunResult(
        run_id=persisted_id,
        n_clusters=n_clusters_total,
        n_noise=n_noise,
        cluster_labels=l1_label_map,
        cluster_descriptions=l1_desc_map,
        umap_2d=reduced_2d,
        doc_ids=doc_ids,
        assignments={did: int(lbl) for did, lbl in zip(doc_ids, l1_labels.tolist(), strict=False)},
        diagnostics=diagnostics,
    )
