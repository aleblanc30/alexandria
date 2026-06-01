"""
Clustering engine: hierarchical document-level embeddings → UMAP → HDBSCAN → LLM labels.

Pipeline:
  1. Aggregate chunk embeddings per document (mean pooling).
  2. Global UMAP + HDBSCAN → level-1 clusters.
  3. Per L1 cluster: local UMAP + HDBSCAN → level-2 sub-clusters.
  4. LLM labelling for L1 and L2 clusters.
  5. Persist to ``cluster_runs`` (with 2-D coords), ``clusters``,
     ``cluster_assignments`` (level 1 and level 2).

The run is always stored regardless of acceptance. Acceptance is a separate
UI action that sets ``cluster_runs.accepted = True``.
"""
import json
import logging
import re
import time
from dataclasses import dataclass

import numpy as np
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.db.queries import get_engine
from pka.db.schema import (
    cluster_assignments,
    cluster_runs,
    clusters,
    documents,
)

log = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


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
    umap_2d: np.ndarray                     # shape (n_docs, 2)
    doc_ids: list[int]
    assignments: dict[int, int]             # {doc_id: L1 hdbscan_label} (-1 = noise)
    diagnostics: dict


def _parse_llm_json(raw: str) -> dict:
    """Strip Markdown code fences and parse JSON, falling back to a regex match."""
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


# ── Step 1: aggregate document embeddings (paginated) ────────────────────────

def _load_document_embeddings(
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> tuple[list[int], np.ndarray]:
    """Mean-pool chunk embeddings per document."""
    from pka.clustering.run_progress import raise_if_cancelled
    from pka.storage.vector_store import (
        fetch_embeddings_by_ids,
        get_collection,
    )

    col = get_collection()
    meta_page = col.get(include=["metadatas"])
    vector_ids = meta_page["ids"]
    metadatas = meta_page["metadatas"]
    if not vector_ids:
        raise ValueError("Vector store is empty — run ingestion first.")

    if run_id is not None:
        raise_if_cancelled(run_id)

    log.info("Loading %d vectors from Chroma…", len(vector_ids))
    embeddings, corrupt_ids = fetch_embeddings_by_ids(vector_ids)
    if corrupt_ids:
        affected_docs = {
            int(metadatas[i].get("document_id", -1))
            for i, vid in enumerate(vector_ids)
            if vid in corrupt_ids
            and metadatas[i].get("document_id") is not None
        }
        log.warning(
            "Skipping %d unreadable Chroma vectors (%d documents; re-embed affected sources to repair)",
            len(corrupt_ids),
            len(affected_docs),
        )

    doc_vecs: dict[int, list[list[float]]] = {}
    for vid, meta in zip(vector_ids, metadatas):
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
    log.info("Aggregated embeddings for %d documents", len(doc_ids))
    return doc_ids, matrix


# ── Step 2: UMAP reduction ────────────────────────────────────────────────────

def _run_umap(
    matrix: np.ndarray,
    n_components_cluster: int = 5,
    n_neighbors: int = 15,
    min_dist: float = 0.1,
    *,
    compute_2d: bool = True,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Return ``(reduced_nd, reduced_2d)``. ``reduced_2d`` is None when ``compute_2d=False``."""
    try:
        import umap
    except ImportError as e:
        raise ImportError("umap-learn is required: pip install umap-learn") from e

    n_docs = len(matrix)
    nn = max(2, min(n_neighbors, max(2, n_docs - 1)))
    n_comp = min(n_components_cluster, max(2, n_docs - 1))

    log.info(
        "Running UMAP (n_neighbors=%d, n_components=%d)…",
        nn, n_comp,
    )

    reducer_nd = umap.UMAP(
        n_components = n_comp,
        n_neighbors  = nn,
        min_dist     = min_dist,
        metric       = "cosine",
        random_state = 42,
    )
    reduced_nd = reducer_nd.fit_transform(matrix)

    reduced_2d = None
    if compute_2d:
        reducer_2d = umap.UMAP(
            n_components = 2,
            n_neighbors  = nn,
            min_dist     = min_dist,
            metric       = "cosine",
            random_state = 42,
        )
        reduced_2d = reducer_2d.fit_transform(matrix)

    log.info("UMAP done. Output shape: %s", reduced_nd.shape)
    return reduced_nd.astype(np.float32), (
        reduced_2d.astype(np.float32) if reduced_2d is not None else None
    )


# ── Step 3: HDBSCAN clustering ────────────────────────────────────────────────

def adaptive_cluster_params(n_docs: int) -> tuple[int, int, int]:
    """Derive HDBSCAN/UMAP params that target moderately sized clusters."""
    if n_docs < 8:
        return max(2, n_docs // 3), 2, max(2, n_docs - 1)

    target_clusters = max(4, min(12, round(n_docs ** 0.5)))
    min_cluster_size = max(3, n_docs // (target_clusters * 2))
    min_samples = max(2, min_cluster_size // 2)
    n_neighbors = max(5, min(30, n_docs // 4))
    return min_cluster_size, min_samples, n_neighbors


def _run_hdbscan(
    reduced: np.ndarray,
    min_cluster_size: int = 5,
    min_samples: int | None = None,
) -> np.ndarray:
    """Try the ``hdbscan`` package first; fall back to sklearn ≥ 1.3."""
    try:
        import hdbscan as hdbscan_lib
        clusterer = hdbscan_lib.HDBSCAN(
            min_cluster_size = min_cluster_size,
            min_samples      = min_samples,
            metric           = "euclidean",
            cluster_selection_method = "leaf",
            prediction_data  = True,
        )
        labels = clusterer.fit_predict(reduced)
        log.info(
            "HDBSCAN (hdbscan pkg): %d clusters, %d noise",
            len(set(labels)) - (1 if -1 in labels else 0),
            (labels == -1).sum(),
        )
        return labels

    except ImportError:
        from sklearn.cluster import HDBSCAN as SkHDBSCAN
        kwargs: dict = dict(
            min_cluster_size = min_cluster_size,
            min_samples      = min_samples or 1,
        )
        try:
            clusterer = SkHDBSCAN(cluster_selection_method="leaf", **kwargs)
        except TypeError:
            clusterer = SkHDBSCAN(**kwargs)
        labels = clusterer.fit_predict(reduced)
        log.info(
            "HDBSCAN (sklearn): %d clusters, %d noise",
            len(set(labels)) - (1 if -1 in labels else 0),
            (labels == -1).sum(),
        )
        return labels


# ── Step 4: LLM cluster labelling ─────────────────────────────────────────────

def _sample_titles_for_cluster(
    doc_ids_in_cluster: list[int],
    n: int = 8,
) -> list[str]:
    eng = get_engine()
    with eng.connect() as con:
        rows = con.execute(
            sa.select(documents.c.title)
            .where(documents.c.id.in_(doc_ids_in_cluster))
            .limit(n)
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def _label_cluster_with_llm(titles: list[str], model: str | None = None) -> tuple[str, str]:
    """Call Ollama for a short label and one-sentence description."""
    if not titles:
        return "Unlabelled", ""

    from pka.ollama_chat import chat_json

    prompt = (
        "You are labelling a topic cluster from a research library.\n"
        "Below are sample document titles from the cluster:\n\n"
        + "\n".join(f"- {t}" for t in titles)
        + "\n\nRespond with ONLY valid JSON in this exact format:\n"
        '{"label": "<3-5 word topic name>", "description": "<one sentence>"}\n'
        "No explanation, no markdown, just the JSON object."
    )

    parsed, err = chat_json(prompt, model=model)
    if err:
        log.warning("LLM labelling failed: %s — using fallback", err)
        return _tfidf_label(titles), ""
    return parsed.get("label", "Unlabelled"), parsed.get("description", "")


def _tfidf_label(titles: list[str], n_words: int = 4) -> str:
    from collections import Counter
    STOPWORDS = {
        "the", "a", "an", "of", "in", "and", "to", "for", "with", "on",
        "is", "are", "by", "from", "at", "as", "that", "this", "its", "it",
    }
    words: list[str] = []
    for t in titles:
        words.extend(re.findall(r"[a-z]{3,}", t.lower()))
    freq = Counter(w for w in words if w not in STOPWORDS)
    top = [w for w, _ in freq.most_common(n_words)]
    return " / ".join(top) if top else "Unlabelled"


# ── Step 5: persist to DB (with UMAP coords) ─────────────────────────────────

def create_run_placeholder() -> int:
    """Insert a run row immediately so the UI can show status=running."""
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        res = con.execute(
            cluster_runs.insert().values(
                timestamp  = now,
                algorithm  = "HDBSCAN-hierarchical",
                parameters = json.dumps({}),
                accepted   = False,
                status     = "running",
            )
        )
        return res.inserted_primary_key[0]


def set_run_status(run_id: int, status: str, *, notes: str | None = None) -> None:
    values: dict = {"status": status}
    if notes is not None:
        values["notes"] = notes
    with get_engine().begin() as con:
        con.execute(
            cluster_runs.update()
            .where(cluster_runs.c.run_id == run_id)
            .values(**values)
        )


def _label_clusters(
    cluster_docs: dict[int, list[int]],
    skip_labelling: bool,
    chat_model: str | None,
    run_id: int | None,
) -> tuple[dict[int, str], dict[int, str]]:
    from pka.clustering.run_progress import raise_if_cancelled

    label_map: dict[int, str] = {}
    desc_map: dict[int, str] = {}
    for cid in sorted(cluster_docs.keys()):
        if run_id is not None:
            raise_if_cancelled(run_id)
        titles = _sample_titles_for_cluster(cluster_docs[cid])
        if skip_labelling:
            label_map[cid] = _tfidf_label(titles)
            desc_map[cid] = ""
        else:
            label, desc = _label_cluster_with_llm(titles, chat_model)
            label_map[cid] = label
            desc_map[cid] = desc
            log.debug("Cluster %d → %s", cid, label)
    return label_map, desc_map


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
    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    l1_db_ids: dict[int, int] = {}

    for cid in l1_unique:
        res2 = con.execute(
            clusters.insert().values(
                label             = l1_label_map.get(cid, f"Cluster {cid}"),
                description       = l1_desc_map.get(cid, ""),
                created_at        = now,
                run_id            = run_id,
                level             = 1,
                parent_cluster_id = None,
            )
        )
        l1_db_ids[cid] = res2.inserted_primary_key[0]

    assignment_rows: list[dict] = []
    for doc_id, raw_label in zip(doc_ids, l1_labels.tolist()):
        db_cid = l1_db_ids.get(raw_label, -1)
        if db_cid == -1:
            continue
        assignment_rows.append({
            "document_id": doc_id,
            "cluster_id":  db_cid,
            "run_id":      run_id,
            "score":       None,
            "assigned_at": now,
            "level":       1,
        })

    n_l2 = 0
    for batch in l2_batches:
        parent_db_id = l1_db_ids.get(batch.parent_l1_id)
        if parent_db_id is None:
            continue
        l2_unique = sorted(set(batch.labels.tolist()) - {-1})
        l2_db_ids: dict[int, int] = {}
        for l2_cid in l2_unique:
            res2 = con.execute(
                clusters.insert().values(
                    label             = batch.label_map.get(l2_cid, f"Subcluster {l2_cid}"),
                    description       = batch.desc_map.get(l2_cid, ""),
                    created_at        = now,
                    run_id            = run_id,
                    level             = 2,
                    parent_cluster_id = parent_db_id,
                )
            )
            l2_db_ids[l2_cid] = res2.inserted_primary_key[0]
            n_l2 += 1

        for doc_id, raw_label in zip(batch.doc_ids, batch.labels.tolist()):
            db_cid = l2_db_ids.get(raw_label, -1)
            if db_cid == -1:
                continue
            assignment_rows.append({
                "document_id": doc_id,
                "cluster_id":  db_cid,
                "run_id":      run_id,
                "score":       None,
                "assigned_at": now,
                "level":       2,
            })

    if assignment_rows:
        con.execute(cluster_assignments.insert(), assignment_rows)

    return len(l1_unique), n_l2, len(assignment_rows)


def _run_level2_pass(
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
    """Run local UMAP+HDBSCAN inside each L1 cluster. Returns batches, noise, skipped."""
    from pka.clustering.run_progress import raise_if_cancelled

    doc_id_to_idx = {d: i for i, d in enumerate(doc_ids)}
    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    l2_batches: list[L2ClusterBatch] = []
    l2_noise = 0
    l2_skipped = 0

    for l1_cid in l1_unique:
        member_doc_ids = [
            doc_ids[i] for i, lbl in enumerate(l1_labels.tolist()) if lbl == l1_cid
        ]
        n_sub = len(member_doc_ids)
        sub_mcs, sub_ms, sub_nn = adaptive_cluster_params(n_sub)
        if n_sub < sub_mcs:
            l2_skipped += 1
            continue

        if run_id is not None:
            raise_if_cancelled(run_id)

        sub_matrix = matrix[[doc_id_to_idx[d] for d in member_doc_ids]]
        sub_reduced_nd, _ = _run_umap(
            sub_matrix,
            n_components_cluster = min(n_components, max(2, n_sub - 1)),
            n_neighbors          = sub_nn,
            min_dist             = min_dist,
            compute_2d           = False,
        )
        l2_labels = _run_hdbscan(sub_reduced_nd, sub_mcs, sub_ms)
        l2_unique = sorted(set(l2_labels.tolist()) - {-1})
        l2_noise += int((l2_labels == -1).sum())

        if len(l2_unique) < 2:
            l2_skipped += 1
            continue

        l2_cluster_docs: dict[int, list[int]] = {c: [] for c in l2_unique}
        for doc_id, lbl in zip(member_doc_ids, l2_labels.tolist()):
            if lbl != -1:
                l2_cluster_docs[lbl].append(doc_id)

        l2_label_map, l2_desc_map = _label_clusters(
            l2_cluster_docs, skip_labelling, chat_model, run_id,
        )
        l2_batches.append(L2ClusterBatch(
            parent_l1_id = l1_cid,
            doc_ids      = member_doc_ids,
            labels       = l2_labels,
            label_map    = l2_label_map,
            desc_map     = l2_desc_map,
        ))

    return l2_batches, l2_noise, l2_skipped


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
    eng = get_engine()
    now = int(time.time())

    umap_records = [
        {
            "doc_id":     int(doc_ids[i]),
            "x":          round(float(umap_2d[i, 0]), 5),
            "y":          round(float(umap_2d[i, 1]), 5),
            "cluster_id": int(l1_labels[i]),
        }
        for i in range(len(doc_ids))
    ]

    with eng.begin() as con:
        con.execute(
            cluster_runs.update()
            .where(cluster_runs.c.run_id == run_id)
            .values(
                timestamp   = now,
                algorithm   = algorithm,
                parameters  = json.dumps(params),
                accepted    = False,
                status      = "finished",
                umap_points = json.dumps(umap_records),
            )
        )
        n_l1, n_l2, n_assign = _write_hierarchical_clusters(
            con, run_id, doc_ids, l1_labels,
            l1_label_map, l1_desc_map, l2_batches, now,
        )

    log.info(
        "Finalized run #%d (%d L1, %d L2 clusters, %d assignments, %d UMAP points)",
        run_id, n_l1, n_l2, n_assign, len(umap_records),
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
    """Write ``cluster_runs`` (with ``umap_points``), ``clusters``, and
    ``cluster_assignments``. ``umap_2d`` shape: ``(n_docs, 2)``.
    """
    eng = get_engine()
    now = int(time.time())

    umap_records = [
        {
            "doc_id":     int(doc_ids[i]),
            "x":          round(float(umap_2d[i, 0]), 5),
            "y":          round(float(umap_2d[i, 1]), 5),
            "cluster_id": int(l1_labels[i]),
        }
        for i in range(len(doc_ids))
    ]

    with eng.begin() as con:
        res = con.execute(
            cluster_runs.insert().values(
                timestamp   = now,
                algorithm   = algorithm,
                parameters  = json.dumps(params),
                accepted    = False,
                status      = "finished",
                umap_points = json.dumps(umap_records),
            )
        )
        run_id = res.inserted_primary_key[0]
        n_l1, n_l2, n_assign = _write_hierarchical_clusters(
            con, run_id, doc_ids, l1_labels,
            l1_label_map, l1_desc_map, l2_batches, now,
        )

    log.info(
        "Persisted run #%d (%d L1, %d L2 clusters, %d assignments, %d UMAP points)",
        run_id, n_l1, n_l2, n_assign, len(umap_records),
    )
    return run_id


# ── Public entry point ────────────────────────────────────────────────────────

def run_clustering(
    min_cluster_size: int | None = None,
    min_samples: int | None = None,
    n_neighbors: int | None = None,
    min_dist: float = 0.1,
    n_components: int = 5,
    label_model: str | None = None,
    skip_labelling: bool = False,
    source_filter: list[str] | None = None,
    run_id: int | None = None,
) -> ClusterRunResult:
    """Full clustering pipeline. Returns diagnostics for the UI acceptance panel."""
    from pka.clustering.run_progress import raise_if_cancelled
    from pka.ollama_chat import resolve_chat_model

    if run_id is not None:
        raise_if_cancelled(run_id)

    doc_ids, matrix = _load_document_embeddings(source_filter, run_id=run_id)
    n_docs = len(doc_ids)

    auto_mcs, auto_ms, auto_nn = adaptive_cluster_params(n_docs)
    mcs = min_cluster_size if min_cluster_size is not None else auto_mcs
    ms = min_samples if min_samples is not None else auto_ms
    nn = n_neighbors if n_neighbors is not None else auto_nn
    chat_model = resolve_chat_model(label_model)

    params = dict(
        min_cluster_size = mcs,
        min_samples      = ms,
        n_neighbors      = nn,
        min_dist         = min_dist,
        n_components     = n_components,
        cluster_selection = "leaf",
        adaptive         = min_cluster_size is None,
        hierarchical     = True,
    )

    if run_id is not None:
        raise_if_cancelled(run_id)

    reduced_nd, reduced_2d = _run_umap(
        matrix,
        n_components_cluster = n_components,
        n_neighbors          = nn,
        min_dist             = min_dist,
    )
    assert reduced_2d is not None

    if run_id is not None:
        raise_if_cancelled(run_id)

    l1_labels = _run_hdbscan(reduced_nd, mcs, ms)

    l1_unique = sorted(set(l1_labels.tolist()) - {-1})
    n_l1 = len(l1_unique)
    n_noise = int((l1_labels == -1).sum())

    l1_cluster_docs: dict[int, list[int]] = {c: [] for c in l1_unique}
    for doc_id, lbl in zip(doc_ids, l1_labels.tolist()):
        if lbl != -1:
            l1_cluster_docs[lbl].append(doc_id)

    if not skip_labelling:
        log.info("Labelling %d L1 clusters via LLM…", n_l1)
    l1_label_map, l1_desc_map = _label_clusters(
        l1_cluster_docs, skip_labelling, chat_model, run_id,
    )

    if run_id is not None:
        raise_if_cancelled(run_id)

    l2_batches, l2_noise, l2_skipped = _run_level2_pass(
        matrix, doc_ids, l1_labels,
        n_components = n_components,
        min_dist     = min_dist,
        skip_labelling = skip_labelling,
        chat_model   = chat_model,
        run_id       = run_id,
    )
    n_l2 = sum(
        len(set(b.labels.tolist()) - {-1}) for b in l2_batches
    )
    params["l2_skipped_parents"] = l2_skipped

    algorithm = "HDBSCAN-hierarchical"
    if run_id is not None:
        raise_if_cancelled(run_id)
        _finalize_run(
            run_id, doc_ids, l1_labels,
            l1_label_map, l1_desc_map, l2_batches,
            algorithm = algorithm,
            params    = params,
            umap_2d   = reduced_2d,
        )
        persisted_id = run_id
    else:
        persisted_id = _persist_run(
            doc_ids, l1_labels,
            l1_label_map, l1_desc_map, l2_batches,
            algorithm = algorithm,
            params    = params,
            umap_2d   = reduced_2d,
        )

    l1_sizes = {cid: len(l1_cluster_docs[cid]) for cid in l1_unique}
    n_clusters_total = n_l1 + n_l2
    diagnostics = {
        "n_clusters":      n_clusters_total,
        "n_l1_clusters":   n_l1,
        "n_l2_clusters":   n_l2,
        "n_noise":         n_noise,
        "n_l2_noise":      l2_noise,
        "l2_skipped_parents": l2_skipped,
        "cluster_sizes":   l1_sizes,
        "size_min":        min(l1_sizes.values()) if l1_sizes else 0,
        "size_max":        max(l1_sizes.values()) if l1_sizes else 0,
        "size_mean":       round(sum(l1_sizes.values()) / len(l1_sizes), 1) if l1_sizes else 0,
    }

    return ClusterRunResult(
        run_id               = persisted_id,
        n_clusters           = n_clusters_total,
        n_noise              = n_noise,
        cluster_labels       = l1_label_map,
        cluster_descriptions = l1_desc_map,
        umap_2d              = reduced_2d,
        doc_ids              = doc_ids,
        assignments          = {
            did: int(lbl) for did, lbl in zip(doc_ids, l1_labels.tolist())
        },
        diagnostics          = diagnostics,
    )
