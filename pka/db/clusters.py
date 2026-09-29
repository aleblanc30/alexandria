"""Document samples for cluster labelling prompts."""

import sqlalchemy as sa

from pka.db.cards import doc_title_excerpts


def sample_cluster_documents(
    con: sa.Connection,
    doc_ids: list[int],
    n: int = 8,
) -> list[tuple[str, str]]:
    """Return up to ``n`` (title, excerpt) pairs for cluster labelling prompts."""
    if not doc_ids:
        return []
    return list(doc_title_excerpts(con, doc_ids[:n]).values())


def sample_cluster_documents_for_clusters(
    con: sa.Connection,
    cluster_docs: dict[int, list[int]],
    n: int = 8,
) -> dict[int, list[tuple[str, str]]]:
    """Batch sample (title, excerpt) per cluster id."""
    all_ids = {did for docs in cluster_docs.values() for did in docs}
    if not all_ids:
        return {cid: [] for cid in cluster_docs}
    by_id = doc_title_excerpts(con, list(all_ids))
    return {
        cid: [by_id[d] for d in doc_ids if d in by_id][:n] for cid, doc_ids in cluster_docs.items()
    }
