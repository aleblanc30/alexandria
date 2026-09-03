"""Tests for the selective purge registry and the enrichment retrigger.

The load-bearing ones are the round trips: purge → retrigger → the artifact is
actually back. A purge target that deletes an expensive artifact and offers no
way to regenerate it is a trap, and the skip gates that decide "already done"
are keyed on a *different* artifact than the one purged
(PURGE_AND_PROVENANCE_PLAN.md §5.2.1).
"""

import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from pka.config import settings as cfg
from pka.db.queries import get_engine, init_db, insert_chunks
from pka.db.schema import chunks, documents, images, overlay_tags
from pka.ingestion.chunker import sentence_window_chunks
from pka.purge import TARGETS, purge_target
from tests.conftest import make_document


def _sentences(n: int, word: str = "Bees") -> str:
    return " ".join(
        f"{word} number {i} do a specific and moderately wordy thing." for i in range(n)
    )


def _seed_fetched_doc(
    source: str = "firefox", source_id: str = "F1", *, sentences: int = 24
) -> int:
    """A document whose body text is stored the way ingestion stores it.

    Chunked by the real chunker, so a reassembly test sees genuine overlap
    rather than a convenient fixture.
    """
    doc_id = make_document(
        source,
        source_id,
        f"{source} doc",
        f"https://example.com/{source_id}",
        int(time.time()),
        fetch_status="fetched",
    )
    texts = sentence_window_chunks(
        _sentences(sentences),
        window=cfg.chunk_sentences,
        overlap=cfg.chunk_overlap,
        min_chars=cfg.min_chunk_chars,
    )
    insert_chunks(
        [
            {
                "document_id": doc_id,
                "chunk_index": i,
                "text": text,
                "token_count": len(text.split()),
                "vector_id": f"vec-{source_id}-{i}",
            }
            for i, text in enumerate(texts)
        ]
    )
    return doc_id


def _add_summary(doc_id: int, text: str = "A study of bees.") -> None:
    """Attach the artifacts a real summary pass leaves behind."""
    eng = get_engine()
    with eng.begin() as con:
        con.execute(
            documents.update().where(documents.c.id == doc_id).values(generated_summary=text)
        )
        n = con.execute(
            sa.select(sa.func.count()).select_from(chunks).where(chunks.c.document_id == doc_id)
        ).scalar()
    insert_chunks(
        [
            {
                "document_id": doc_id,
                "chunk_index": n,
                "text": text,
                "token_count": len(text.split()),
                "vector_id": f"vec-summary-{doc_id}",
                "chunk_pass": "summary",
            }
        ]
    )


def _chunk_passes(doc_id: int) -> list:
    with get_engine().connect() as con:
        return [
            r[0]
            for r in con.execute(
                sa.select(chunks.c.chunk_pass).where(chunks.c.document_id == doc_id)
            ).fetchall()
        ]


def _summary_of(doc_id: int) -> str | None:
    with get_engine().connect() as con:
        return con.execute(
            sa.select(documents.c.generated_summary).where(documents.c.id == doc_id)
        ).scalar()


@pytest.fixture()
def db(empty_vector_store):
    init_db()


@pytest.fixture()
def client(empty_vector_store):
    init_db()
    from pka.api.main import app

    return TestClient(app, raise_server_exceptions=True)


# ── The round trips (§5.2.1) ────────────────────────────────────────────────


class TestSummaryRoundTrip:
    @pytest.fixture()
    def summary_on(self, monkeypatch):
        monkeypatch.setattr(cfg, "bookmark_summary_enabled", True)

    @pytest.fixture()
    def chat(self, monkeypatch):
        """Scripted summariser — the suite never reaches a real model."""
        import pka.ingestion.summarize as sz

        calls: list[str] = []

        def _chat_json(prompt, model=None, temperature=None, timeout=90):
            calls.append(prompt)
            return ({"summary": "Bees build hives. They also make honey."}, None)

        monkeypatch.setattr(sz, "chat_json", _chat_json)
        return calls

    def test_purge_leaves_body_chunks_and_enrich_restores_the_summary(
        self, db, mock_chroma, summary_on, chat
    ):
        doc_id = _seed_fetched_doc()
        _add_summary(doc_id)
        body_chunks = len([p for p in _chunk_passes(doc_id) if p != "summary"])
        assert body_chunks > 1, "fixture must produce a multi-chunk body"

        purge_target("summaries")

        # The expensive thing (fetched text) survives; only the summary is gone.
        assert _summary_of(doc_id) is None
        assert _chunk_passes(doc_id) == [None] * body_chunks

        from pka.ingestion.enrich import enrich_summaries

        stats = enrich_summaries()

        assert stats == {"candidates": 1, "summarised": 1, "skipped": 0}
        assert _summary_of(doc_id) == "Bees build hives. They also make honey."
        assert "summary" in _chunk_passes(doc_id)
        assert chat, "the summariser was never called"

    def test_enrich_skips_documents_that_still_have_a_summary(
        self, db, mock_chroma, summary_on, chat
    ):
        doc_id = _seed_fetched_doc()
        _add_summary(doc_id)

        from pka.ingestion.enrich import enrich_summaries

        assert enrich_summaries()["candidates"] == 0
        assert not chat, "a cached summary must not pay for inference again"

    def test_enrich_respects_the_summary_flag_being_off(self, db, mock_chroma, chat):
        """The flag is the single gate for the whole mechanism (DESIGN.md §1.1)."""
        _seed_fetched_doc()

        from pka.ingestion.enrich import enrich_summaries

        stats = enrich_summaries()

        assert stats["summarised"] == 0
        assert not chat


class TestImageTextRoundTrip:
    def _seed_image(self, path, *, indexed: bool = True) -> int:
        doc_id = make_document("image", str(path), "a.jpg", str(path), int(time.time()))
        with get_engine().begin() as con:
            con.execute(
                images.insert().values(
                    document_id=doc_id,
                    path=str(path),
                    filename="a.jpg",
                    image_type="photo",
                    description="a bee on a flower",
                    ocr_text="BEE",
                    books_json='[{"title": "Bees"}]',
                    indexed_at=int(time.time()) if indexed else None,
                )
            )
        insert_chunks(
            [
                {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "text": "a bee on a flower BEE",
                    "token_count": 5,
                    "vector_id": "vec-img-0",
                }
            ]
        )
        return doc_id

    def test_purge_reopens_the_skip_gate(self, db, tmp_path):
        """``indexed_at`` — not ``description`` — is what the pipeline checks."""
        from pka.ingestion.image_pipeline import _image_already_embedded

        path = tmp_path / "a.jpg"
        self._seed_image(path)
        assert _image_already_embedded(path)

        purge_target("image_text")

        assert not _image_already_embedded(path)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(images.c.description, images.c.ocr_text, images.c.books_json)
            ).fetchone()
        assert row == (None, None, None)

    def test_purge_removes_chunks_so_a_re_run_cannot_duplicate_them(self, db, tmp_path):
        """The image pipeline appends at existing_chunk_count without deduping."""
        doc_id = self._seed_image(tmp_path / "a.jpg")

        purge_target("image_text")

        with get_engine().connect() as con:
            n = con.execute(
                sa.select(sa.func.count()).select_from(chunks).where(chunks.c.document_id == doc_id)
            ).scalar()
        assert n == 0


# ── Registry invariants ─────────────────────────────────────────────────────


def _seed_a_bit_of_everything() -> None:
    doc_id = _seed_fetched_doc()
    _add_summary(doc_id)
    _seed_fetched_doc("zotero", "Z1")
    now = int(time.time())
    with get_engine().begin() as con:
        con.execute(
            overlay_tags.insert().values(
                document_id=doc_id, tag="llm-tag", origin="llm", created_at=now
            )
        )
        con.execute(
            overlay_tags.insert().values(
                document_id=doc_id, tag="mine", origin="manual", created_at=now
            )
        )


@pytest.mark.parametrize("key", sorted(TARGETS))
def test_dry_run_count_matches_what_the_purge_deletes(db, key):
    """A dry run is a promise about what pressing the button will do."""
    _seed_a_bit_of_everything()

    counted = purge_target(key, dry_run=True)
    purged = purge_target(key, dry_run=False)

    for name, n in counted.items():
        assert purged[name] == n, f"{key}: {name} counted {n}, purged {purged[name]}"


@pytest.mark.parametrize("key", sorted(TARGETS))
def test_no_target_touches_user_authored_tags(db, key):
    """Tier 1 is off limits to every target in the registry (plan §3)."""
    _seed_a_bit_of_everything()

    purge_target(key)

    with get_engine().connect() as con:
        origins = {
            r[0] for r in con.execute(sa.select(overlay_tags.c.origin).distinct()).fetchall()
        }
    assert "manual" in origins


def test_unknown_target_raises(db):
    with pytest.raises(ValueError, match="Unknown purge target"):
        purge_target("nope")


def test_source_filter_scopes_the_purge(db):
    firefox_doc = _seed_fetched_doc("firefox", "F1")
    zotero_doc = _seed_fetched_doc("zotero", "Z1")
    _add_summary(firefox_doc)
    _add_summary(zotero_doc)

    counts = purge_target("summaries", source="firefox")

    assert counts["documents"] == 1
    assert _summary_of(firefox_doc) is None
    assert _summary_of(zotero_doc) == "A study of bees."


def test_purge_summaries_clears_the_summary_vector(db, mock_chroma):
    doc_id = _seed_fetched_doc()
    _add_summary(doc_id)

    counts = purge_target("summaries")

    assert counts["chunks"] == 1
    assert counts["vectors_purged"] == 1
    _store, col = mock_chroma
    assert col.delete.call_args.kwargs["ids"] == [f"vec-summary-{doc_id}"]


def test_vectors_purge_forgets_the_ids_but_keeps_the_text(db, monkeypatch):
    """rebuild_from_chunks re-embeds from chunks.text, so the text must survive."""
    dropped: list[bool] = []
    monkeypatch.setattr(
        "pka.storage.vector_store.drop_document_collection", lambda: dropped.append(True)
    )
    doc_id = _seed_fetched_doc()
    with get_engine().begin() as con:
        con.execute(documents.update().where(documents.c.id == doc_id).values(doc_embedding=b"xx"))

    counts = purge_target("vectors")

    assert dropped == [True]
    assert counts["chunks"] > 0
    assert counts["documents"] == 1
    with get_engine().connect() as con:
        rows = con.execute(sa.select(chunks.c.vector_id, chunks.c.text)).fetchall()
        embedding = con.execute(sa.select(documents.c.doc_embedding)).scalar()
    assert rows and all(r.vector_id is None for r in rows)
    assert all(r.text for r in rows), "chunk text is what a rebuild reads"
    assert embedding is None


def test_machine_tags_purge_keeps_source_and_manual_origins(db):
    doc_id = _seed_fetched_doc()
    now = int(time.time())
    with get_engine().begin() as con:
        for origin in (
            "llm",
            "cluster_l1",
            "cluster_l2",
            "inferred",
            "manual",
            "learned",
            "source",
        ):
            con.execute(
                overlay_tags.insert().values(
                    document_id=doc_id, tag=f"t-{origin}", origin=origin, created_at=now
                )
            )

    counts = purge_target("machine_tags")

    assert counts["overlay_tags"] == 4
    with get_engine().connect() as con:
        origins = {r[0] for r in con.execute(sa.select(overlay_tags.c.origin)).fetchall()}
    assert origins == {"manual", "learned", "source"}


def test_fetched_text_requeues_only_network_fetched_documents(db):
    """A Calibre book rests at ``available`` and is re-read from disk, not re-fetched."""
    fetched = _seed_fetched_doc("firefox", "F1")
    book = make_document("calibre", "C1", "A Book", "/books/a.epub", 0, fetch_status="available")
    insert_chunks(
        [
            {
                "document_id": book,
                "chunk_index": 0,
                "text": "book body text that is long enough to matter",
                "token_count": 8,
                "vector_id": "vec-book-0",
                "chunk_pass": "fulltext",
            }
        ]
    )

    purge_target("fetched_text")

    with get_engine().connect() as con:
        statuses = dict(con.execute(sa.select(documents.c.id, documents.c.fetch_status)).fetchall())
        assert con.execute(sa.select(sa.func.count()).select_from(chunks)).scalar() == 0
    assert statuses[fetched] == "pending"
    assert statuses[book] == "available"


# ── Reassembly (§10's open risk) ────────────────────────────────────────────


class TestReassembly:
    def test_round_trips_a_real_multi_chunk_document(self):
        from pka.ingestion.chunker import clean_text
        from pka.ingestion.enrich import reassemble_chunk_text

        text = _sentences(30)
        pieces = sentence_window_chunks(text, window=5, overlap=1, min_chars=1)
        assert len(pieces) > 3

        assert reassemble_chunk_text(pieces) == clean_text(text)

    def test_does_not_duplicate_overlapping_sentences(self):
        from pka.ingestion.enrich import reassemble_chunk_text

        out = reassemble_chunk_text(["One. Two. Three.", "Three. Four."])

        assert out == "One. Two. Three. Four."

    def test_appends_whole_when_nothing_overlaps(self):
        """A window dropped by min_chars leaves a gap, never a duplicated fragment."""
        from pka.ingestion.enrich import reassemble_chunk_text

        out = reassemble_chunk_text(["One. Two.", "Nine. Ten."])

        assert out == "One. Two. Nine. Ten."

    def test_ignores_empty_chunks(self):
        from pka.ingestion.enrich import reassemble_chunk_text

        assert reassemble_chunk_text(["", "   ", "One."]) == "One."


# ── API surface (§5.3) ──────────────────────────────────────────────────────


def test_purge_targets_endpoint_lists_the_registry(client):
    _seed_a_bit_of_everything()

    r = client.get("/ingestion/purge-targets")

    assert r.status_code == 200
    body = r.json()
    keys = {t["key"] for t in body["targets"]}
    assert keys == set(TARGETS)
    summaries = next(t for t in body["targets"] if t["key"] == "summaries")
    assert summaries["counts"]["documents"] == 1
    assert summaries["retrigger"]


def test_purge_endpoint_dry_run_deletes_nothing(client):
    _seed_a_bit_of_everything()

    r = client.post("/ingestion/purge/summaries?dry_run=true")

    assert r.status_code == 200
    assert r.json()["status"] == "counted"
    assert r.json()["counts"]["documents"] == 1
    with get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count())
            .select_from(documents)
            .where(documents.c.generated_summary.isnot(None))
        ).scalar()
    assert n == 1


def test_purge_endpoint_purges(client):
    _seed_a_bit_of_everything()

    r = client.post("/ingestion/purge/summaries")

    assert r.status_code == 200
    assert r.json()["status"] == "purged"
    with get_engine().connect() as con:
        n = con.execute(
            sa.select(sa.func.count())
            .select_from(documents)
            .where(documents.c.generated_summary.isnot(None))
        ).scalar()
    assert n == 0


def test_purge_endpoint_unknown_target(client):
    assert client.post("/ingestion/purge/bogus").status_code == 400


def test_purge_endpoint_blocked_while_a_sync_runs(client):
    from pka.ingestion import progress as sp

    _seed_a_bit_of_everything()
    sp.begin_job("firefox", "metadata", phase="loading")
    try:
        assert client.post("/ingestion/purge/summaries").status_code == 409
        # …but a dry run only reads, so it stays available.
        assert client.post("/ingestion/purge/summaries?dry_run=true").status_code == 200
        # A purge scoped to an idle source is not blocked by another's sync.
        assert client.post("/ingestion/purge/summaries?source=zotero").status_code == 200
    finally:
        sp.reset("firefox")


def test_enrich_endpoint_rejects_an_unknown_kind(client):
    assert client.post("/ingestion/enrich?kind=bogus").status_code == 400
