import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from PIL import Image as PILImage

from pka.config import settings as cfg
from pka.connectors.images import ImageFile
from pka.db.queries import get_engine, init_db
from pka.db.schema import chunks, documents, images, overlay_tags

FAKE_CLIP_DIM  = 512
FAKE_TEXT_DIM  = 8
FAKE_CLIP_VEC  = [0.01] * FAKE_CLIP_DIM
FAKE_TEXT_VEC  = [0.1]  * FAKE_TEXT_DIM


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


@pytest.fixture()
def sample_image(tmp_path) -> Path:
    p = tmp_path / "test_slide.png"
    PILImage.new("RGB", (1920, 1080), color="lightgrey").save(p)
    return p


@pytest.fixture()
def mock_vision(monkeypatch):
    from pka.ingestion.image_extractor import ImageContent

    monkeypatch.setattr(
        "pka.ingestion.image_pipeline.extract_image_content",
        lambda path, image_type=None, model="llava": ImageContent(
            image_type="slide",
            description="A slide about machine learning.",
            content="Title: gradient descent\nbullet: learning rate schedules",
        ),
    )


@pytest.fixture()
def mock_ocr(monkeypatch):
    monkeypatch.setattr(
        "pka.ingestion.image_pipeline.ocr_image",
        lambda path, lang="eng": "Introduction to Neural Networks",
    )


@pytest.fixture()
def mock_clip(monkeypatch):
    monkeypatch.setattr(
        "pka.ingestion.image_pipeline.clip_embed_image",
        lambda path: FAKE_CLIP_VEC,
    )
    monkeypatch.setattr(
        "pka.ingestion.image_extractor.clip_embed_text",
        lambda query: FAKE_CLIP_VEC,
    )


@pytest.fixture()
def mock_text_embed(monkeypatch):
    monkeypatch.setattr(
        "pka.ingestion.image_pipeline.image_search_text",
        lambda ocr, desc, content="": "description and ocr combined",
    )


@pytest.fixture()
def mock_chroma_clip(monkeypatch):
    col = MagicMock()
    monkeypatch.setattr(
        "pka.ingestion.image_pipeline._get_clip_collection",
        lambda: col,
    )
    return col


@pytest.fixture()
def all_mocks(mock_vision, mock_ocr, mock_clip, mock_text_embed,
              mock_chroma_clip, mock_chroma):
    """Combine all mocks for full pipeline tests."""
    return mock_chroma_clip


class TestIngestImage:
    def test_returns_ok_status(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img)
        assert result["status"] == "ok"

    def test_image_type_stored(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(images.c.image_type)
                .where(images.c.path == str(sample_image))
            ).fetchone()
        assert row[0] == "slide"

    def test_ocr_text_stored(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(images.c.ocr_text)
                .where(images.c.path == str(sample_image))
            ).fetchone()
        assert "Neural Networks" in (row[0] or "")

    def test_description_stored(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(images.c.description)
                .where(images.c.path == str(sample_image))
            ).fetchone()
        assert "machine learning" in (row[0] or "").lower()

    def test_auto_tag_from_type(self, sample_image, all_mocks):
        """image_type becomes an inferred overlay tag on the unified document."""
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(overlay_tags.c.tag, overlay_tags.c.origin)
                .join(images, images.c.document_id == overlay_tags.c.document_id)
                .where(images.c.path == str(sample_image))
            ).fetchone()
        assert row[0] == "slide"
        assert row[1] == "inferred"

    def test_creates_document_row(self, sample_image, all_mocks):
        """An image is a first-class document: source=image, linked + searchable."""
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            image_row = con.execute(
                sa.select(images.c.id, images.c.document_id)
                .where(images.c.path == str(sample_image))
            ).fetchone()
            doc = con.execute(
                sa.select(documents.c.source, documents.c.title,
                          documents.c.url_or_path, documents.c.card_summary)
                .where(documents.c.id == image_row[1])
            ).fetchone()
            n_chunks = con.execute(
                sa.select(sa.func.count()).select_from(chunks)
                .where(chunks.c.document_id == image_row[1])
            ).scalar()
        assert image_row[1] is not None                     # images.document_id set
        assert doc[0] == "image"
        assert doc[1] == sample_image.name                  # title = filename
        assert doc[2] == str(sample_image)                  # url_or_path = image path
        assert "machine learning" in (doc[3] or "").lower()  # card_summary = description
        assert n_chunks >= 1                                 # searchable text embedded

    def test_clip_vector_id_stored(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(images.c.clip_vector_id)
                .where(images.c.path == str(sample_image))
            ).fetchone()
        assert row[0] is not None

    def test_clip_upsert_called(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        assert all_mocks.upsert.called

    def test_dry_run_writes_nothing(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img, dry_run=True)
        assert result["status"] == "dry_run"
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar()
        assert count == 0

    def test_skip_vision_leaves_unknown_type(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img, skip_vision=True)
        assert result["image_type"] == "unknown"

    def test_skip_ocr_stores_none(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img, skip_ocr=True)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(images.c.ocr_text)
                .where(images.c.path == str(sample_image))
            ).fetchone()
        assert row[0] is None

    def test_content_text_reaches_the_index(self, sample_image, mock_vision, mock_ocr,
                                            mock_clip, mock_chroma_clip, mock_chroma):
        """The per-type extraction (not just the description) is what gets embedded."""
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            text = con.execute(
                sa.select(chunks.c.text)
                .join(images, images.c.document_id == chunks.c.document_id)
                .where(images.c.path == str(sample_image))
            ).scalar()
        assert "learning rate schedules" in text          # transcript content
        assert "Neural Networks" in text                  # OCR still included

    def test_book_fields_exposed_and_cached(self, sample_image, all_mocks, monkeypatch):
        """Cover extraction is returned by ingest_image and cached in books_json."""
        import json

        from pka.connectors.images import ImageFile
        from pka.ingestion.image_extractor import ImageContent
        from pka.ingestion.image_pipeline import ingest_image

        extracted = [
            {"title": "Godel, Escher, Bach", "authors": ["Douglas Hofstadter"],
             "isbn": "9780465026562"},
            {"title": "The Society of Mind", "authors": ["Marvin Minsky"], "isbn": None},
        ]
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.extract_image_content",
            lambda path, image_type=None, model="llava": ImageContent(
                image_type="multiple_book_covers",
                description="Two paperbacks on a desk.",
                content="Godel, Escher, Bach — Douglas Hofstadter",
                books=extracted,
            ),
        )
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img)
        assert result["books"] == extracted
        with get_engine().connect() as con:
            stored = con.execute(
                sa.select(images.c.books_json)
                .where(images.c.path == str(sample_image))
            ).scalar()
        assert json.loads(stored) == extracted

    def test_upsert_on_reindex(self, sample_image, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        ingest_image(img)  # second call — should upsert, not duplicate
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(images)
                .where(images.c.path == str(sample_image))
            ).scalar()
        assert count == 1


class TestIngestImages:
    def _make_image_file(self, path: Path) -> ImageFile:
        PILImage.new("RGB", (100, 100)).save(path)
        return ImageFile(path, path.name, 100, 100, 500, int(time.time()), {})

    def test_processes_all_images(self, tmp_path, all_mocks):
        from pka.ingestion.image_pipeline import ingest_images
        imgs = [self._make_image_file(tmp_path / f"img{i}.jpg") for i in range(3)]
        stats = ingest_images(imgs)
        assert stats["processed"] == 3

    def test_skip_existing_skips_already_indexed(self, tmp_path, all_mocks):
        from pka.ingestion.image_pipeline import ingest_images
        imgs = [self._make_image_file(tmp_path / "single.jpg")]
        ingest_images(imgs)
        stats = ingest_images(imgs, skip_existing=True)
        assert stats["skipped"] == 1
        assert stats["processed"] == 0

    def test_by_type_counts(self, tmp_path, all_mocks):
        from pka.ingestion.image_pipeline import ingest_images
        imgs = [self._make_image_file(tmp_path / f"s{i}.jpg") for i in range(2)]
        stats = ingest_images(imgs)
        assert "slide" in stats["by_type"]
        assert stats["by_type"]["slide"] == 2


class TestSearchImagesByText:
    def test_returns_hits(self, monkeypatch):
        monkeypatch.setattr(cfg, "clip_enabled", True)
        monkeypatch.setattr(
            "pka.ingestion.image_extractor.clip_embed_text",
            lambda q: [0.1, 0.2, 0.3],
        )
        mock_col = MagicMock()
        mock_col.query.return_value = {
            "ids": [["vid-1"]],
            "metadatas": [[{"filename": "a.png", "path": "/a.png", "image_type": "slide"}]],
            "distances": [[0.12]],
        }
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline._get_clip_collection",
            lambda: mock_col,
        )
        from pka.ingestion.image_pipeline import search_images_by_text
        hits = search_images_by_text("neural networks", n=5)
        assert len(hits) == 1
        assert hits[0]["vector_id"] == "vid-1"
        assert hits[0]["filename"] == "a.png"

    def test_returns_empty_when_clip_fails(self, monkeypatch):
        monkeypatch.setattr(cfg, "clip_enabled", True)
        monkeypatch.setattr(
            "pka.ingestion.image_extractor.clip_embed_text",
            lambda q: None,
        )
        from pka.ingestion.image_pipeline import search_images_by_text
        assert search_images_by_text("query") == []

    def test_returns_empty_when_clip_disabled(self, monkeypatch):
        """Off by default: no model load, no query — the call short-circuits."""
        called = {"n": 0}

        def _embed(q):
            called["n"] += 1
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr("pka.ingestion.image_extractor.clip_embed_text", _embed)
        monkeypatch.setattr(
            "pka.ingestion.image_pipeline._get_clip_collection",
            lambda: pytest.fail("CLIP collection opened with clip_enabled off"),
        )
        from pka.ingestion.image_pipeline import search_images_by_text
        assert search_images_by_text("query") == []
        assert called["n"] == 0


class TestSearchImagesByInferredText:
    """The non-CLIP path: query text vs the text inferred from the picture."""

    def _hit(self, vid, doc_id, distance, text="", pass_=None):
        meta = {"document_id": doc_id, "source": "image", "title": "a.png"}
        if pass_:
            meta["pass"] = pass_
        return {"vector_id": vid, "text": text, "distance": distance, "metadata": meta}

    def test_returns_best_chunk_per_document(self, monkeypatch):
        hits = [
            self._hit("v1", 7, 0.40, "transcript"),
            self._hit("v2", 7, 0.10, "book synopsis", pass_="external_synopsis"),
            self._hit("v3", 9, 0.25, "poster summary"),
        ]
        monkeypatch.setattr("pka.storage.vector_store.query", lambda *a, **kw: hits)
        from pka.ingestion.image_pipeline import search_images_by_inferred_text
        out = search_images_by_inferred_text("neural networks", n=5)
        assert [h["document_id"] for h in out] == [7, 9]
        assert out[0]["vector_id"] == "v2"          # nearer of the two doc-7 chunks
        assert out[0]["pass"] == "external_synopsis"
        assert out[1]["pass"] is None

    def test_restricted_to_image_source(self, monkeypatch):
        seen: dict = {}

        def _query(text, n_results=10, where=None):
            seen["where"] = where
            seen["n_results"] = n_results
            return []

        monkeypatch.setattr("pka.storage.vector_store.query", _query)
        from pka.ingestion.image_pipeline import search_images_by_inferred_text
        search_images_by_inferred_text("q", n=5)
        assert seen["where"] == {"source": "image"}
        assert seen["n_results"] >= 5  # over-fetch: chunks collapse per document

    def test_works_with_clip_disabled(self, monkeypatch):
        """The whole point: images stay searchable without the visual index."""
        monkeypatch.setattr(cfg, "clip_enabled", False)
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda *a, **kw: [self._hit("v1", 3, 0.2, "gradient descent")],
        )
        from pka.ingestion.image_pipeline import search_images_by_inferred_text
        assert len(search_images_by_inferred_text("gradient descent")) == 1

    def test_returns_empty_when_store_unavailable(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("chroma down")

        monkeypatch.setattr("pka.storage.vector_store.query", _boom)
        from pka.ingestion.image_pipeline import search_images_by_inferred_text
        assert search_images_by_inferred_text("query") == []

    def test_skips_hits_without_document_id(self, monkeypatch):
        bad = {"vector_id": "v0", "text": "", "distance": 0.1, "metadata": {}}
        monkeypatch.setattr(
            "pka.storage.vector_store.query",
            lambda *a, **kw: [bad, self._hit("v1", 4, 0.5)],
        )
        from pka.ingestion.image_pipeline import search_images_by_inferred_text
        out = search_images_by_inferred_text("query")
        assert [h["document_id"] for h in out] == [4]


class TestRegisterImages:
    def _make_image_file(self, path: Path) -> ImageFile:
        PILImage.new("RGB", (50, 50)).save(path)
        return ImageFile(path, path.name, 50, 50, 200, int(time.time()), {})

    def test_registers_new_image(self, tmp_path):
        from pka.ingestion.image_pipeline import register_images
        img = self._make_image_file(tmp_path / "new.jpg")
        stats = register_images([img])
        assert stats["processed"] == 1
        assert stats["skipped"] == 0
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(images.c.filename, images.c.document_id)
                .where(images.c.path == str(img.path))
            ).fetchone()
            doc_source = con.execute(
                sa.select(documents.c.source).where(documents.c.id == row[1])
            ).scalar()
        assert row is not None
        assert row[1] is not None          # linked to a documents row
        assert doc_source == "image"       # registered as an image document

    def test_skips_existing_path(self, tmp_path):
        from pka.ingestion.image_pipeline import register_images
        img = self._make_image_file(tmp_path / "dup.jpg")
        register_images([img])
        stats = register_images([img])
        assert stats["skipped"] == 1
        assert stats["processed"] == 0

    def test_dry_run_counts_without_db_row(self, tmp_path):
        from pka.ingestion.image_pipeline import register_images
        img = self._make_image_file(tmp_path / "dry.jpg")
        stats = register_images([img], dry_run=True)
        assert stats["processed"] == 1
        with get_engine().connect() as con:
            count = con.execute(
                sa.select(sa.func.count()).select_from(images)
            ).scalar()
        assert count == 0

    def test_stops_on_cancel(self, tmp_path):
        from pka.ingestion import sync_progress as sp
        from pka.ingestion.image_pipeline import register_images
        sp.begin("image")
        sp.set_phase("image", "ingesting", 5)
        sp.request_cancel("image")
        imgs = [self._make_image_file(tmp_path / f"c{i}.jpg") for i in range(3)]
        stats = register_images(imgs, progress_key="image")
        assert stats.get("stopped") == "cancel"
        assert stats["processed"] == 0

    def test_failure_ticks_progress(self, tmp_path, monkeypatch):
        from pka.ingestion import sync_progress as sp
        from pka.ingestion.image_pipeline import register_images

        class _BrokenEngine:
            def begin(self):
                raise RuntimeError("db error")

        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.get_engine",
            lambda: _BrokenEngine(),
        )
        sp.begin("image")
        sp.set_phase("image", "ingesting", 1)
        img = self._make_image_file(tmp_path / "fail.jpg")
        stats = register_images([img], progress_key="image")
        assert stats["failed"] == 1


class TestClipCollectionCache:
    def test_clip_collection_cached(self, isolated_settings):
        import pka.ingestion.image_pipeline as ip
        ip.reset_clip_collection()
        col_a = ip._get_clip_collection()
        col_b = ip._get_clip_collection()
        assert col_a is col_b
        ip.reset_clip_collection()


class TestIngestImagesStop:
    def test_stops_on_cancel(self, tmp_path, all_mocks):
        from pka.connectors.images import ImageFile
        from pka.ingestion import sync_progress as sp
        from pka.ingestion.image_pipeline import ingest_images

        sp.begin("image")
        sp.set_phase("image", "ingesting", 3)
        sp.request_cancel("image")
        imgs = [
            ImageFile(tmp_path / f"i{i}.jpg", f"i{i}.jpg", 10, 10, 100, int(time.time()), {})
            for i in range(3)
        ]
        for p in imgs:
            PILImage.new("RGB", (10, 10)).save(p.path)
        stats = ingest_images(imgs, progress_key="image")
        assert stats.get("stopped") == "cancel"
        assert stats["processed"] == 0


class TestBookSynopsisCascade:
    """Cover/shelf synopsis lookup (DESIGN.md §3.2), default-off."""

    @pytest.fixture()
    def book_vision(self, monkeypatch):
        from pka.ingestion.image_extractor import ImageContent

        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.extract_image_content",
            lambda path, image_type=None, model="llava": ImageContent(
                image_type="multiple_book_covers",
                description="Two books on a table.",
                content="Dune by Frank Herbert; Neuromancer by William Gibson",
                books=[
                    {"title": "Dune", "authors": ["Frank Herbert"], "isbn": None},
                    {"title": "Neuromancer", "authors": ["William Gibson"], "isbn": None},
                ],
            ),
        )

    @pytest.fixture()
    def fake_lookup(self, monkeypatch):
        from pka.ingestion.openlibrary import BookSynopsis

        def _lookup(title="", authors=None, isbn=None):
            return BookSynopsis(
                title=title,
                description=f"{title} is a novel. It concerns its own themes.",
                authors=authors or [],
                work_key=f"/works/{title}",
                resolved_by="search",
            )

        monkeypatch.setattr("pka.ingestion.openlibrary.lookup_book", _lookup)

    def _synopsis_meta(self, mock_chroma):
        """Synopsis chunk metadata out of the in-memory Chroma store."""
        store, _col = mock_chroma
        return [
            item["meta"] for item in store.values()
            if item["meta"].get("pass") == "external_synopsis"
        ]

    def test_one_chunk_per_visible_book(
        self, sample_image, book_vision, fake_lookup, mock_ocr, mock_clip,
        mock_chroma_clip, mock_chroma,
    ):
        from pka.ingestion.image_pipeline import ingest_image

        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img, skip_gate=True)

        assert result["synopsis_chunks"] == 2
        metas = self._synopsis_meta(mock_chroma)
        assert {m["book_title"] for m in metas} == {"Dune", "Neuromancer"}
        assert all(m["modality"] == "image" for m in metas)
        assert all(m["resolved_by"] == "search" for m in metas)

    def test_card_summary_and_description_stay_the_photo_caption(
        self, sample_image, book_vision, fake_lookup, mock_ocr, mock_clip,
        mock_chroma_clip, mock_chroma,
    ):
        """Enrichment must not rewrite what the browse card says about the image."""
        from pka.ingestion.image_pipeline import ingest_image

        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img, skip_gate=True)

        with get_engine().connect() as con:
            row = con.execute(sa.select(images.c.description)).fetchone()
            card = con.execute(sa.select(documents.c.card_summary)).fetchone()
        assert row[0] == "Two books on a table."
        assert card[0] == "Two books on a table."

    def test_synopsis_chunks_do_not_collide_with_the_main_chunk(
        self, sample_image, book_vision, fake_lookup, mock_ocr, mock_clip,
        mock_chroma_clip, mock_chroma,
    ):
        from pka.ingestion.image_pipeline import ingest_image

        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img, skip_gate=True)

        with get_engine().connect() as con:
            idxs = [r[0] for r in con.execute(sa.select(chunks.c.chunk_index)).fetchall()]
        assert len(idxs) == len(set(idxs)), f"duplicate chunk_index: {idxs}"

    def test_disabled_lookup_adds_nothing(
        self, sample_image, book_vision, mock_ocr, mock_clip,
        mock_chroma_clip, mock_chroma,
    ):
        """conftest pins external_lookup_enabled off; the real lookup must no-op."""
        from pka.ingestion.image_pipeline import ingest_image

        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img, skip_gate=True)
        assert result["synopsis_chunks"] == 0
        assert self._synopsis_meta(mock_chroma) == []

    def test_lookup_failure_does_not_break_ingestion(
        self, sample_image, book_vision, monkeypatch, mock_ocr, mock_clip,
        mock_chroma_clip, mock_chroma,
    ):
        def _boom(title="", authors=None, isbn=None):
            raise RuntimeError("openlibrary down")

        monkeypatch.setattr("pka.ingestion.openlibrary.lookup_book", _boom)
        from pka.ingestion.image_pipeline import ingest_image

        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img, skip_gate=True)
        assert result["status"] == "ok"
        assert result["synopsis_chunks"] == 0

    def test_non_book_image_never_looks_anything_up(
        self, sample_image, mock_vision, mock_ocr, mock_clip,
        mock_chroma_clip, mock_chroma, monkeypatch,
    ):
        calls = []
        monkeypatch.setattr(
            "pka.ingestion.openlibrary.lookup_book",
            lambda **kw: calls.append(kw),
        )
        from pka.ingestion.image_pipeline import ingest_image

        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        result = ingest_image(img, skip_gate=True)
        assert result["synopsis_chunks"] == 0
        assert calls == []

    def test_entry_without_title_or_isbn_is_skipped(
        self, sample_image, monkeypatch, fake_lookup, mock_ocr, mock_clip,
        mock_chroma_clip, mock_chroma,
    ):
        from pka.ingestion.image_extractor import ImageContent

        monkeypatch.setattr(
            "pka.ingestion.image_pipeline.extract_image_content",
            lambda path, image_type=None, model="llava": ImageContent(
                image_type="bookshelf",
                description="A shelf.",
                content="spines",
                books=[{"title": "", "authors": [], "isbn": None},
                       {"title": "Dune", "authors": [], "isbn": None}],
            ),
        )
        from pka.ingestion.image_pipeline import ingest_image

        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        assert ingest_image(img, skip_gate=True)["synopsis_chunks"] == 1
