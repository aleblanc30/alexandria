import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from PIL import Image as PILImage

from pka.connectors.images import ImageFile
from pka.db.queries import get_engine, init_db
from pka.db.schema import image_tags, images

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
    monkeypatch.setattr(
        "pka.ingestion.image_pipeline.classify_and_describe",
        lambda path, model="llava": ("slide", "A slide about machine learning."),
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
        lambda ocr, desc: "description and ocr combined",
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
        from pka.connectors.images import ImageFile
        from pka.ingestion.image_pipeline import ingest_image
        img = ImageFile(sample_image, sample_image.name, 1920, 1080, 1000,
                        int(time.time()), {})
        ingest_image(img)
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(image_tags.c.tag)
                .join(images, images.c.id == image_tags.c.image_id)
                .where(images.c.path == str(sample_image))
            ).fetchone()
        assert row[0] == "slide"

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
        monkeypatch.setattr(
            "pka.ingestion.image_extractor.clip_embed_text",
            lambda q: None,
        )
        from pka.ingestion.image_pipeline import search_images_by_text
        assert search_images_by_text("query") == []


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
                sa.select(images.c.filename).where(images.c.path == str(img.path))
            ).fetchone()
        assert row is not None

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
