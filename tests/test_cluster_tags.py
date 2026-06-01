"""Tests for cluster label → overlay tag helpers."""
import time

import pytest

from pka.clustering.cluster_tags import (
    apply_tag_to_documents,
    cluster_document_ids,
    label_to_tag,
    slugify_tag,
)
from pka.constants import TagOrigin
from pka.db.queries import get_engine, init_db, upsert_document
from pka.db.schema import cluster_assignments, clusters, cluster_runs


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


def _seed_cluster_with_docs(n_docs: int = 2) -> tuple[int, int, list[int]]:
    doc_ids = []
    for i in range(n_docs):
        doc_ids.append(
            upsert_document("zotero", f"T{i:03d}", f"Doc {i}", None, int(time.time()))
        )
    now = int(time.time())
    with get_engine().begin() as con:
        run_res = con.execute(
            cluster_runs.insert().values(
                timestamp=now, algorithm="test", parameters="{}", accepted=True,
            )
        )
        run_id = run_res.inserted_primary_key[0]
        cl_res = con.execute(
            clusters.insert().values(
                label="Raft Consensus", description="", created_at=now,
                run_id=run_id, level=1,
            )
        )
        cluster_id = cl_res.inserted_primary_key[0]
        for did in doc_ids:
            con.execute(
                cluster_assignments.insert().values(
                    document_id=did, cluster_id=cluster_id, run_id=run_id,
                    assigned_at=now, level=1,
                )
            )
    return cluster_id, run_id, doc_ids


class TestSlugify:
    def test_slugify_tag(self):
        assert slugify_tag("Distributed Systems") == "distributed-systems"

    def test_label_to_tag(self):
        assert label_to_tag("My Topic", 42) == "my-topic"
        assert label_to_tag("", 7) == "cluster-7"


class TestApplyTagToDocuments:
    def test_apply_and_idempotent(self):
        cluster_id, run_id, doc_ids = _seed_cluster_with_docs(2)
        with get_engine().begin() as con:
            a1, s1 = apply_tag_to_documents(
                con, doc_ids, "topic", TagOrigin.CLUSTER_L1,
            )
            a2, s2 = apply_tag_to_documents(
                con, doc_ids, "topic", TagOrigin.CLUSTER_L1,
            )
        assert a1 == 2 and s1 == 0
        assert a2 == 0 and s2 == 2

    def test_cluster_document_ids(self):
        cluster_id, run_id, doc_ids = _seed_cluster_with_docs(3)
        with get_engine().connect() as con:
            found = cluster_document_ids(con, cluster_id, run_id)
        assert set(found) == set(doc_ids)
