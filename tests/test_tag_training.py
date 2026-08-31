"""Tests for active-learning tag training."""

import numpy as np
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from pka.clustering.doc_embeddings import EMBEDDING_DIM, embedding_to_blob
from pka.constants import TagOrigin
from pka.db.queries import get_engine, init_db, upsert_document
from pka.db.schema import overlay_tags, source_tags, tag_training_labels
from pka.tag_training import lifecycle


def _pos_vec(seed: float = 1.0) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[0] = seed
    v[1] = 0.5
    return v


def _neg_vec(seed: float = -1.0) -> np.ndarray:
    v = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    v[0] = seed
    v[1] = -0.5
    return v


def _set_embedding(doc_id: int, vec: np.ndarray) -> None:
    eng = get_engine()
    blob = embedding_to_blob(vec)
    with eng.begin() as con:
        con.execute(
            sa.text("UPDATE documents SET doc_embedding = :blob WHERE id = :id"),
            {"blob": blob, "id": doc_id},
        )


@pytest.fixture()
def client(empty_vector_store):
    init_db()
    from pka.api.main import app

    return TestClient(app, raise_server_exceptions=True)


def _seed_labeled_corpus() -> tuple[list[int], list[int], list[int]]:
    pos_ids, neg_ids, extra_ids = [], [], []
    for i in range(4):
        doc_id = upsert_document(
            "zotero",
            f"P{i}",
            f"Positive {i}",
            "https://example.com/p",
            1700000000 + i,
        )
        _set_embedding(doc_id, _pos_vec(1.0 + i * 0.01))
        pos_ids.append(doc_id)
    for i in range(4):
        doc_id = upsert_document(
            "firefox",
            f"N{i}",
            f"Negative {i}",
            "https://example.com/n",
            1700000100 + i,
        )
        _set_embedding(doc_id, _neg_vec(-1.0 - i * 0.01))
        neg_ids.append(doc_id)
    for i in range(3):
        doc_id = upsert_document(
            "calibre",
            f"U{i}",
            f"Unlabeled {i}",
            "https://example.com/u",
            1700000200 + i,
        )
        _set_embedding(doc_id, _pos_vec(0.3 + i * 0.01))
        extra_ids.append(doc_id)
    return pos_ids, neg_ids, extra_ids


class TestTagTrainingLifecycle:
    def test_create_session_trains_model(self):
        init_db()
        pos_ids, _, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "ml-topic",
            [{"doc_id": did, "label": 1} for did in pos_ids[:3]],
        )
        assert session["has_model"] is True
        assert session["positive_count"] >= 3
        assert session["negative_count"] >= 1

    def test_queue_returns_uncertain_docs(self):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "topic-a",
            [{"doc_id": did, "label": 1} for did in pos_ids[:3]]
            + [{"doc_id": neg_ids[0], "label": 0}],
        )
        queue = lifecycle.get_queue(session["session_id"])
        assert queue
        assert "probability" in queue[0]
        assert "doc_id" in queue[0]

    def test_user_labels_retrain(self):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "topic-b",
            [{"doc_id": did, "label": 1} for did in pos_ids[:3]]
            + [{"doc_id": neg_ids[0], "label": 0}],
        )
        updated = lifecycle.add_user_labels(
            session["session_id"],
            [{"doc_id": neg_ids[1], "label": 0}],
        )
        assert updated["negative_count"] >= 2

    def test_accept_writes_learned_overlay_tags(self):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "learned-tag",
            [{"doc_id": did, "label": 1} for did in pos_ids]
            + [{"doc_id": did, "label": 0} for did in neg_ids],
        )
        accepted = lifecycle.accept_session(session["session_id"])
        assert accepted["status"] == "accepted"
        with get_engine().connect() as con:
            rows = con.execute(
                sa.select(overlay_tags.c.tag, overlay_tags.c.origin).where(
                    overlay_tags.c.tag == "learned-tag"
                )
            ).fetchall()
        assert rows
        assert all(r[1] == str(TagOrigin.LEARNED) for r in rows)

    def test_from_source_tag(self):
        init_db()
        doc_id = upsert_document(
            "zotero",
            "ST1",
            "Tagged",
            "https://example.com/t",
            1700000000,
        )
        for i, vec in enumerate([_neg_vec(), _neg_vec(), _neg_vec()]):
            nid = upsert_document(
                "firefox",
                f"STN{i}",
                f"Other{i}",
                f"https://example.com/n{i}",
                1700000001 + i,
            )
            _set_embedding(nid, vec)
        _set_embedding(doc_id, _pos_vec())
        eng = get_engine()
        with eng.begin() as con:
            con.execute(
                source_tags.insert().values(
                    document_id=doc_id,
                    tag_string="consensus",
                    source="zotero",
                )
            )
        session = lifecycle.create_session_from_source_tag("consensus", "consensus-learned")
        assert session["positive_count"] >= 1
        assert session["provenance"]["from_source_tag"] == "consensus"
        assert session["has_model"] is True

    def test_training_ignores_predicted_labels(self):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "no-predictions",
            [{"doc_id": did, "label": 1} for did in pos_ids[:2]],
            bootstrap_negatives=False,
        )
        sid = session["session_id"]
        lifecycle.add_user_labels(sid, [{"doc_id": neg_ids[0], "label": 0}])
        eng = get_engine()
        with eng.begin() as con:
            con.execute(
                tag_training_labels.insert().values(
                    session_id=sid,
                    document_id=neg_ids[1],
                    label=1,
                    source="predicted",
                    created_at=1700000000,
                )
            )
        from pka.tag_training.engine import load_label_matrix

        _, y, used, _ = load_label_matrix(sid)
        assert neg_ids[1] not in used
        assert neg_ids[0] in used

    def test_pseudo_label_model_threshold(self, monkeypatch):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        sure_pos = upsert_document(
            "zotero",
            "SP",
            "Sure positive",
            "https://example.com/sp",
            1700000300,
        )
        _set_embedding(sure_pos, _pos_vec(3.0))
        session = lifecycle.create_session(
            "pseudo-model",
            [{"doc_id": did, "label": 1} for did in pos_ids[:3]]
            + [{"doc_id": neg_ids[0], "label": 0}],
            bootstrap_negatives=False,
        )
        sid = session["session_id"]

        def fake_pseudo(_sid, _blob, *, high=0.95, low=0.05):
            assert high == 0.95
            assert low == 0.05
            return [(sure_pos, 1, 0.99)]

        monkeypatch.setattr(
            "pka.tag_training.lifecycle.pseudo_labels_from_model",
            fake_pseudo,
        )
        result = lifecycle.apply_pseudo_labels_model(sid)
        pr = result["pseudo_label_result"]
        assert pr["mode"] == "model"
        assert pr["added_positive"] == 1
        assert pr["pseudo_label_high"] == 0.95
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(tag_training_labels.c.source, tag_training_labels.c.label).where(
                    (tag_training_labels.c.session_id == sid)
                    & (tag_training_labels.c.document_id == sure_pos)
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "pseudo"
        assert row[1] == 1

    def test_llm_pseudo_uses_user_negatives(self, monkeypatch):
        init_db()
        pos_ids, neg_ids, extra_ids = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "pseudo-llm-user-neg",
            [{"doc_id": did, "label": 1} for did in pos_ids[:2]],
            bootstrap_negatives=False,
        )
        sid = session["session_id"]
        lifecycle.add_user_labels(sid, [{"doc_id": neg_ids[0], "label": 0}])

        captured: dict[str, str] = {}

        def fake_classify(
            tag,
            *,
            seed_samples,
            negative_samples,
            title,
            excerpt,
            model=None,
            negative_source="random",
        ):
            captured["negative_source"] = negative_source
            return 0, None

        monkeypatch.setattr("pka.tag_training.lifecycle.classify_document_for_tag", fake_classify)
        monkeypatch.setattr(
            "pka.tag_training.lifecycle.random.sample",
            lambda pool, k: extra_ids[:1],
        )
        result = lifecycle.apply_pseudo_labels_llm(sid, batch_size=1)
        assert result["pseudo_label_result"]["negative_source"] == "user"
        assert captured["negative_source"] == "user"

    def test_pseudo_label_llm(self, monkeypatch):
        init_db()
        pos_ids, neg_ids, extra_ids = _seed_labeled_corpus()
        from pka.db.queries import insert_chunks

        for did in extra_ids[:2]:
            insert_chunks(
                [
                    {
                        "document_id": did,
                        "chunk_index": 0,
                        "text": "machine learning systems research",
                        "token_count": 10,
                        "vector_id": f"v-{did}",
                    }
                ]
            )
        session = lifecycle.create_session(
            "pseudo-llm",
            [{"doc_id": did, "label": 1} for did in pos_ids[:2]],
            bootstrap_negatives=False,
        )
        sid = session["session_id"]

        def fake_classify(
            tag,
            *,
            seed_samples,
            negative_samples,
            title,
            excerpt,
            model=None,
            negative_source="random",
        ):
            if "Unlabeled 0" in title:
                return 1, None
            if "Unlabeled 1" in title:
                return 0, None
            return None, "skip"

        monkeypatch.setattr(
            "pka.tag_training.lifecycle.classify_document_for_tag",
            fake_classify,
        )
        monkeypatch.setattr(
            "pka.tag_training.lifecycle.random.sample",
            lambda pool, k: extra_ids[: min(k, len(extra_ids))],
        )
        result = lifecycle.apply_pseudo_labels_llm(sid, batch_size=5)
        pr = result["pseudo_label_result"]
        assert pr["mode"] == "llm"
        assert pr["added_positive"] == 1
        assert pr["added_negative"] == 1
        with get_engine().connect() as con:
            for did in extra_ids[:2]:
                row = con.execute(
                    sa.select(tag_training_labels.c.source, tag_training_labels.c.label).where(
                        (tag_training_labels.c.session_id == sid)
                        & (tag_training_labels.c.document_id == did)
                    )
                ).fetchone()
                assert row is not None, f"missing label for doc {did}"
                assert row[0] == "pseudo_llm"

    def test_create_session_reports_bootstrap_negatives(self):
        init_db()
        pos_ids, _, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "bootstrap-count",
            [{"doc_id": did, "label": 1} for did in pos_ids[:3]],
        )
        assert session.get("bootstrap_negatives_added", 0) >= 1
        assert session["negative_count"] >= 1

    def test_untrainable_session_queue_empty(self):
        """Single-class labels without embeddings must not recurse — queue is just empty."""
        init_db()
        doc_id = upsert_document(
            "zotero",
            "NOEMB",
            "No embedding",
            "https://example.com/x",
            1700000000,
        )
        session = lifecycle.create_session(
            "untrainable",
            [{"doc_id": doc_id, "label": 1}],
            bootstrap_negatives=False,
        )
        assert session["has_model"] is False
        assert lifecycle.get_queue(session["session_id"]) == []

    def test_untrainable_session_pseudo_label_raises(self):
        init_db()
        doc_id = upsert_document(
            "zotero",
            "NOEMB2",
            "No embedding",
            "https://example.com/y",
            1700000000,
        )
        session = lifecycle.create_session(
            "untrainable-2",
            [{"doc_id": doc_id, "label": 1}],
            bootstrap_negatives=False,
        )
        with pytest.raises(ValueError, match="Cannot train"):
            lifecycle.apply_pseudo_labels_model(session["session_id"])

    def test_upsert_labels_batch_insert_and_update(self):
        init_db()
        pos_ids, neg_ids, extra_ids = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "batch-upsert",
            [{"doc_id": did, "label": 1} for did in pos_ids[:2]]
            + [{"doc_id": neg_ids[0], "label": 0}],
            bootstrap_negatives=False,
        )
        sid = session["session_id"]
        eng = get_engine()
        with eng.begin() as con:
            lifecycle._upsert_labels(
                con,
                sid,
                [(pos_ids[0], 0), (extra_ids[0], 1)],
                "user",
            )
        with get_engine().connect() as con:
            rows = {
                r[0]: (r[1], r[2])
                for r in con.execute(
                    sa.select(
                        tag_training_labels.c.document_id,
                        tag_training_labels.c.label,
                        tag_training_labels.c.source,
                    ).where(tag_training_labels.c.session_id == sid)
                ).fetchall()
            }
        assert rows[pos_ids[0]] == (0, "user")  # updated in place
        assert rows[extra_ids[0]] == (1, "user")  # newly inserted
        assert rows[pos_ids[1]] == (1, "seed")  # untouched

    def test_upsert_rejects_predicted_source(self):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "reject-pred",
            [{"doc_id": did, "label": 1} for did in pos_ids[:2]]
            + [{"doc_id": neg_ids[0], "label": 0}],
            bootstrap_negatives=False,
        )
        eng = get_engine()
        with pytest.raises(ValueError, match="Invalid label source"):
            with eng.begin() as con:
                lifecycle._upsert_labels(
                    con,
                    session["session_id"],
                    [(neg_ids[1], 1)],
                    "predicted",
                )


class TestTagTrainingApi:
    def test_create_session_endpoint(self, client):
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        r = client.post(
            "/tag-training/sessions",
            json={
                "tag": "api-tag",
                "labels": (
                    [{"doc_id": did, "label": 1} for did in pos_ids[:3]]
                    + [{"doc_id": did, "label": 0} for did in neg_ids[:2]]
                ),
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["tag"] == "api-tag"
        assert data["has_model"] is True

    def test_from_source_tag_endpoint(self, client):
        doc_id = upsert_document(
            "zotero",
            "ST2",
            "Tagged",
            "https://example.com/t2",
            1700000000,
        )
        _set_embedding(doc_id, _pos_vec())
        eng = get_engine()
        with eng.begin() as con:
            con.execute(
                source_tags.insert().values(
                    document_id=doc_id,
                    tag_string="raft",
                    source="zotero",
                )
            )
        r = client.post(
            "/tag-training/sessions/from-source-tag",
            json={
                "source_tag": "raft",
                "target_tag": "raft-learned",
            },
        )
        assert r.status_code == 200
        assert r.json()["tag"] == "raft-learned"

    def test_queue_and_label_flow(self, client):
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        created = client.post(
            "/tag-training/sessions",
            json={
                "tag": "flow-tag",
                "labels": [{"doc_id": did, "label": 1} for did in pos_ids[:3]]
                + [{"doc_id": neg_ids[0], "label": 0}],
            },
        ).json()
        sid = created["session_id"]
        q = client.get(f"/tag-training/sessions/{sid}/queue")
        assert q.status_code == 200
        assert isinstance(q.json(), list)
        labeled = client.post(
            f"/tag-training/sessions/{sid}/labels",
            json={
                "labels": [{"doc_id": neg_ids[0], "label": 0}],
            },
        )
        assert labeled.status_code == 200

    def test_resume_accepted_session(self):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "resume-tag",
            [{"doc_id": did, "label": 1} for did in pos_ids]
            + [{"doc_id": did, "label": 0} for did in neg_ids],
        )
        lifecycle.accept_session(session["session_id"])
        resumed = lifecycle.resume_session(session["session_id"])
        assert resumed["status"] == "labeling"
        assert resumed["has_model"] is True

    def test_apply_learned_tag_to_new_document(self):
        init_db()
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        session = lifecycle.create_session(
            "auto-tag",
            [{"doc_id": did, "label": 1} for did in pos_ids]
            + [{"doc_id": did, "label": 0} for did in neg_ids],
        )
        lifecycle.accept_session(session["session_id"])
        new_id = upsert_document(
            "zotero",
            "NEW1",
            "New paper",
            "https://example.com/new",
            1700000999,
        )
        _set_embedding(new_id, _pos_vec(1.05))
        from pka.tag_training.lifecycle import apply_learned_tags_for_document

        n = apply_learned_tags_for_document(new_id)
        assert n >= 1
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(overlay_tags.c.tag).where(
                    (overlay_tags.c.document_id == new_id)
                    & (overlay_tags.c.origin == str(TagOrigin.LEARNED))
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "auto-tag"

    def test_pseudo_label_endpoint(self, client, monkeypatch):
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        created = client.post(
            "/tag-training/sessions",
            json={
                "tag": "api-pseudo",
                "labels": [{"doc_id": did, "label": 1} for did in pos_ids[:3]]
                + [{"doc_id": did, "label": 0} for did in neg_ids[:2]],
            },
        ).json()
        sid = created["session_id"]
        r = client.post(f"/tag-training/sessions/{sid}/pseudo-label", json={"mode": "model"})
        assert r.status_code == 200
        body = r.json()
        assert body.get("pseudo_label_result", {}).get("mode") == "model"

    def test_resume_endpoint(self, client):
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        created = client.post(
            "/tag-training/sessions",
            json={
                "tag": "api-resume",
                "labels": [{"doc_id": did, "label": 1} for did in pos_ids]
                + [{"doc_id": did, "label": 0} for did in neg_ids],
            },
        ).json()
        client.post(f"/tag-training/sessions/{created['session_id']}/accept")
        r = client.post(f"/tag-training/sessions/{created['session_id']}/resume")
        assert r.status_code == 200
        assert r.json()["status"] == "labeling"

    def test_pseudo_label_untrainable_400(self, client):
        doc_id = upsert_document(
            "zotero",
            "NOEMB3",
            "No embedding",
            "https://example.com/z",
            1700000000,
        )
        created = client.post(
            "/tag-training/sessions",
            json={
                "tag": "untrainable-api",
                "labels": [{"doc_id": doc_id, "label": 1}],
            },
        ).json()
        r = client.post(
            f"/tag-training/sessions/{created['session_id']}/pseudo-label",
            json={"mode": "model"},
        )
        assert r.status_code == 400
        assert "Cannot train" in r.json()["detail"]

    def test_list_documents_learned_tags_filter(self, client):
        pos_ids, neg_ids, _ = _seed_labeled_corpus()
        created = client.post(
            "/tag-training/sessions",
            json={
                "tag": "filter-tag",
                "labels": [{"doc_id": did, "label": 1} for did in pos_ids]
                + [{"doc_id": did, "label": 0} for did in neg_ids],
            },
        ).json()
        client.post(f"/tag-training/sessions/{created['session_id']}/accept")
        r = client.get("/documents", params=[("learned_tags", "filter-tag")])
        assert r.status_code == 200
        assert r.json()["total"] >= 1
