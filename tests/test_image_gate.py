"""Tests for the two-step image admission gate (text coverage + VLM category)."""
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image as PILImage

from pka.connectors.images import ImageFile
from pka.db.queries import get_rejected_paths, init_db


@pytest.fixture()
def sample_png(tmp_path) -> Path:
    # 800x600 = 480_000 px — a round area for coverage math.
    p = tmp_path / "photo.png"
    PILImage.new("RGB", (800, 600), color="white").save(p)
    return p


def _box(x0, y0, x1, y1):
    """EasyOCR-style 4-corner polygon."""
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _image_file(path: Path, size=(800, 600)) -> ImageFile:
    PILImage.new("RGB", size, color="white").save(path)
    return ImageFile(path, path.name, size[0], size[1], 1000, int(time.time()), {})


# ── EasyOCR text-coverage measurement ─────────────────────────────────────────

class TestTextCoverage:
    def test_sums_box_area_fraction(self, sample_png, monkeypatch):
        from pka.providers.easy_ocr import EasyOcrProvider
        reader = MagicMock()
        # 200x200 + 100x100 = 40_000 + 10_000 = 50_000 / 480_000 ≈ 0.104
        reader.readtext.return_value = [
            (_box(0, 0, 200, 200), "A", 0.9),
            (_box(300, 300, 400, 400), "B", 0.9),
        ]
        monkeypatch.setattr(EasyOcrProvider, "_reader", lambda self, langs: reader)
        cov = EasyOcrProvider().text_coverage(sample_png)
        assert cov == pytest.approx(50_000 / 480_000, rel=1e-3)

    def test_no_text_is_zero(self, sample_png, monkeypatch):
        from pka.providers.easy_ocr import EasyOcrProvider
        reader = MagicMock()
        reader.readtext.return_value = []
        monkeypatch.setattr(EasyOcrProvider, "_reader", lambda self, langs: reader)
        assert EasyOcrProvider().text_coverage(sample_png) == 0.0

    def test_clamped_to_one(self, sample_png, monkeypatch):
        from pka.providers.easy_ocr import EasyOcrProvider
        reader = MagicMock()
        # Box larger than the image (overlaps inflate) — must clamp to 1.0.
        reader.readtext.return_value = [(_box(0, 0, 2000, 2000), "X", 0.9)]
        monkeypatch.setattr(EasyOcrProvider, "_reader", lambda self, langs: reader)
        assert EasyOcrProvider().text_coverage(sample_png) == 1.0

    def test_failure_returns_zero(self, sample_png, monkeypatch):
        from pka.providers.easy_ocr import EasyOcrProvider

        def _boom(self, langs):
            raise RuntimeError("ocr fail")

        monkeypatch.setattr(EasyOcrProvider, "_reader", _boom)
        assert EasyOcrProvider().text_coverage(sample_png) == 0.0

    def test_exif_orientation_is_applied(self, tmp_path, monkeypatch):
        """Rotated phone photos (EXIF orientation=6) must be transposed before
        EasyOCR sees them — otherwise its loader crashes on an empty array and
        every such image is (wrongly) rejected for zero text coverage.

        A landscape 800x400 image tagged orientation=6 becomes portrait 400x800
        after transpose; coverage divides the box area by the *transposed* area.
        """
        from PIL import Image as PILImg

        from pka.providers.easy_ocr import EasyOcrProvider

        src = tmp_path / "rotated.jpg"
        im = PILImg.new("RGB", (800, 400), color="white")
        exif = im.getexif()
        exif[274] = 6  # 274 = Orientation, 6 = rotate 90° CW
        im.save(src, exif=exif)

        captured = {}

        class _FakeReader:
            def readtext(self, image, **kw):
                captured["shape"] = image.shape  # numpy array, not a path
                return [(_box(0, 0, 200, 200), "A", 0.9)]  # 40_000 px

        monkeypatch.setattr(EasyOcrProvider, "_reader", lambda self, langs: _FakeReader())
        cov = EasyOcrProvider().text_coverage(src)

        # Array handed to EasyOCR is transposed to portrait (H=800, W=400).
        assert captured["shape"][:2] == (800, 400)
        assert cov == pytest.approx(40_000 / (800 * 400), rel=1e-3)


# ── Gate decision logic ───────────────────────────────────────────────────────

class TestGateImage:
    def _patch_coverage(self, monkeypatch, value):
        fake = MagicMock()
        fake.text_coverage.return_value = value
        monkeypatch.setattr("pka.ingestion.image_gate._get_easyocr", lambda: fake)
        return fake

    def test_low_coverage_rejects_without_calling_vlm(self, sample_png, monkeypatch):
        import pka.ingestion.image_gate as gate
        self._patch_coverage(monkeypatch, 0.01)
        classify = MagicMock()
        monkeypatch.setattr(gate, "classify_and_describe", classify)

        res = gate.gate_image(sample_png, coverage_min=0.05)
        assert not res.passed
        assert res.reason == gate.REASON_LOW_COVERAGE
        assert res.text_coverage == 0.01
        classify.assert_not_called()  # cheap gate first, VLM never runs

    def test_unknown_category_rejects(self, sample_png, monkeypatch):
        import pka.ingestion.image_gate as gate
        self._patch_coverage(monkeypatch, 0.5)
        monkeypatch.setattr(gate, "get_gate_vision_provider", lambda: MagicMock())
        monkeypatch.setattr(gate, "classify_and_describe", lambda *a, **k: ("unknown", ""))

        res = gate.gate_image(sample_png, coverage_min=0.05)
        assert not res.passed
        assert res.reason == gate.REASON_NOT_CATEGORY

    def test_passes_both_gates(self, sample_png, monkeypatch):
        import pka.ingestion.image_gate as gate
        self._patch_coverage(monkeypatch, 0.5)
        monkeypatch.setattr(gate, "get_gate_vision_provider", lambda: MagicMock())
        monkeypatch.setattr(
            gate, "classify_and_describe", lambda *a, **k: ("slide", "A slide.")
        )

        res = gate.gate_image(sample_png, coverage_min=0.05)
        assert res.passed
        assert res.reason is None
        assert res.image_type == "slide"

    def test_uses_gate_model_and_provider(self, sample_png, monkeypatch):
        """The gate classifies with its own provider + model, not the main one."""
        import pka.ingestion.image_gate as gate
        self._patch_coverage(monkeypatch, 0.5)
        sentinel = object()
        monkeypatch.setattr(gate, "get_gate_vision_provider", lambda: sentinel)
        captured = {}

        def _classify(path, model=None, provider=None, *, strict=False):
            captured["model"] = model
            captured["provider"] = provider
            captured["strict"] = strict
            return "poster", "desc"

        monkeypatch.setattr(gate, "classify_and_describe", _classify)
        gate.gate_image(sample_png, vision_model="moondream", coverage_min=0.05)
        assert captured["model"] == "moondream"
        assert captured["provider"] is sentinel
        assert captured["strict"] is True  # gate must classify in strict mode


# ── Pipeline integration ──────────────────────────────────────────────────────

class TestGateInPipeline:
    @pytest.fixture(autouse=True)
    def _enable_gate(self, monkeypatch):
        from pka.config import settings
        monkeypatch.setattr(settings, "image_gate_enabled", True)
        init_db()

    def _reject_all(self, monkeypatch):
        import pka.ingestion.image_gate as gate
        fake = MagicMock()
        fake.text_coverage.return_value = 0.0
        monkeypatch.setattr(gate, "_get_easyocr", lambda: fake)

    def test_rejected_image_recorded_and_cached(self, sample_png, monkeypatch):
        import pka.ingestion.image_gate as gate
        from pka.ingestion.image_pipeline import ingest_image
        self._reject_all(monkeypatch)

        img = _image_file(sample_png)
        res = ingest_image(img)
        assert res["status"] == "rejected"
        assert res["reason"] == gate.REASON_LOW_COVERAGE
        assert str(sample_png) in get_rejected_paths()

    def test_dry_run_does_not_cache(self, sample_png, monkeypatch):
        from pka.ingestion.image_pipeline import ingest_image
        self._reject_all(monkeypatch)

        img = _image_file(sample_png)
        res = ingest_image(img, dry_run=True)
        assert res["status"] == "rejected"
        assert get_rejected_paths() == set()

    def test_skip_gate_bypasses(self, sample_png, monkeypatch):
        """--skip-gate short-circuits the gate; EasyOCR must not even be touched."""
        import pka.ingestion.image_gate as gate
        from pka.ingestion.image_pipeline import ingest_image

        def _boom():
            raise AssertionError("gate should be skipped")

        monkeypatch.setattr(gate, "_get_easyocr", _boom)
        # Skip the expensive passes so we only exercise the gate bypass.
        img = _image_file(sample_png)
        res = ingest_image(img, skip_gate=True, skip_vision=True, skip_ocr=True, skip_clip=True)
        assert res["status"] != "rejected"

    def test_ingest_images_counts_rejections(self, tmp_path, monkeypatch):
        from pka.ingestion.image_pipeline import ingest_images
        self._reject_all(monkeypatch)

        imgs = [_image_file(tmp_path / f"i{i}.png") for i in range(2)]
        stats = ingest_images(imgs)
        assert stats["rejected"] == 2
        assert stats["processed"] == 0
        assert stats["skipped"] == 0
        assert stats["by_reason"]["low_text_coverage"] == 2

    def test_missing_easyocr_surfaces_and_does_not_cache(self, tmp_path, monkeypatch):
        """A broken EasyOCR install must fail images (loudly), never reject-and-cache
        them — otherwise clearing the cache can't help until the install is fixed."""
        import pka.providers.easy_ocr as eo
        from pka.ingestion.image_pipeline import ingest_images

        def _raise():
            raise eo.EasyOcrUnavailable("easyocr missing")

        monkeypatch.setattr(eo, "ensure_easyocr_available", _raise)

        imgs = [_image_file(tmp_path / f"n{i}.png") for i in range(2)]
        stats = ingest_images(imgs)
        assert stats["failed"] == 2
        assert stats["rejected"] == 0
        assert get_rejected_paths() == set()  # nothing cached → a retry can succeed

    def test_vlm_backend_outage_surfaces_and_does_not_cache(self, sample_png, monkeypatch):
        """Coverage passes but the gate VLM backend is down: the image must fail
        (retryable), not be rejected-and-cached as 'not a category of interest'."""
        import pka.ingestion.image_gate as gate
        from pka.ingestion.image_pipeline import ingest_images

        cov = MagicMock()
        cov.text_coverage.return_value = 0.5  # clear step 1
        monkeypatch.setattr(gate, "_get_easyocr", lambda: cov)
        down = MagicMock()
        down.complete.side_effect = RuntimeError("ollama down")
        monkeypatch.setattr(gate, "get_gate_vision_provider", lambda: down)

        stats = ingest_images([_image_file(sample_png)])
        assert stats["failed"] == 1
        assert stats["rejected"] == 0
        assert get_rejected_paths() == set()

    def test_ingest_images_skips_cached_rejects(self, tmp_path, monkeypatch):
        from pka.ingestion.image_pipeline import ingest_images
        self._reject_all(monkeypatch)

        imgs = [_image_file(tmp_path / "one.png")]
        ingest_images(imgs)                       # first run rejects + caches
        stats = ingest_images(imgs)               # second run skips via cache
        assert stats["rejected"] == 0
        assert stats["skipped"] == 1

    def test_reject_drops_registered_rows(self, tmp_path, monkeypatch):
        """A rejected image leaves no documents/images row behind."""
        import sqlalchemy as sa

        from pka.db.queries import get_engine
        from pka.db.schema import documents, images
        from pka.ingestion.image_pipeline import ingest_image, register_images
        self._reject_all(monkeypatch)

        img = _image_file(tmp_path / "drop.png")
        register_images([img])                    # metadata pass creates the rows
        with get_engine().connect() as con:
            assert con.execute(
                sa.select(sa.func.count()).select_from(images)
                .where(images.c.path == str(img.path))
            ).scalar() == 1

        res = ingest_image(img)
        assert res["status"] == "rejected"
        with get_engine().connect() as con:
            assert con.execute(
                sa.select(sa.func.count()).select_from(images)
                .where(images.c.path == str(img.path))
            ).scalar() == 0
            assert con.execute(
                sa.select(sa.func.count()).select_from(documents)
                .where(documents.c.url_or_path == str(img.path))
            ).scalar() == 0

    def test_register_ignores_cached_rejects(self, tmp_path, monkeypatch):
        """Reruns of metadata sync do not re-register a previously rejected path."""
        import sqlalchemy as sa

        from pka.db.queries import get_engine, record_image_rejection
        from pka.db.schema import images
        from pka.ingestion.image_pipeline import register_images

        img = _image_file(tmp_path / "known_bad.png")
        record_image_rejection(str(img.path), "low_text_coverage")

        stats = register_images([img])
        assert stats["skipped"] == 1
        assert stats["processed"] == 0
        with get_engine().connect() as con:
            assert con.execute(
                sa.select(sa.func.count()).select_from(images)
                .where(images.c.path == str(img.path))
            ).scalar() == 0


class TestGateLabelThreadedToContentPass:
    """The gate's label picks the main pass's per-type prompt — no third call."""

    @pytest.fixture(autouse=True)
    def _enable_gate(self, monkeypatch):
        from pka.config import settings
        monkeypatch.setattr(settings, "image_gate_enabled", True)
        init_db()

    def test_main_pass_reuses_gate_label_without_reclassifying(
        self, tmp_path, monkeypatch, mock_chroma
    ):
        import pka.ingestion.image_extractor as extractor
        import pka.ingestion.image_gate as gate
        from pka.ingestion.image_pipeline import ingest_image

        cov = MagicMock()
        cov.text_coverage.return_value = 0.5
        monkeypatch.setattr(gate, "_get_easyocr", lambda: cov)
        monkeypatch.setattr(gate, "get_gate_vision_provider", lambda: MagicMock())
        monkeypatch.setattr(
            gate, "classify_and_describe",
            lambda *a, **k: ("whiteboard", "gate model's throwaway description"),
        )

        main = MagicMock()
        main.complete.return_value = (
            '{"transcript": "attention is all you need -> retrieval",'
            ' "description": "A whiteboard covered in marker."}'
        )
        monkeypatch.setattr(extractor, "get_vision_provider", lambda: main)
        monkeypatch.setattr("pka.ingestion.image_pipeline.ocr_image", lambda p, lang="eng": "")
        monkeypatch.setattr("pka.ingestion.image_pipeline.clip_embed_image", lambda p: None)

        res = ingest_image(_image_file(tmp_path / "board.png"))

        assert res["status"] == "ok"
        assert res["image_type"] == "whiteboard"        # gate label, not re-derived
        assert main.complete.call_count == 1            # one main call, no reclassify
        prompt = main.complete.call_args[0][0]
        assert '"transcript"' in prompt                 # transcript prompt for whiteboard
        assert '"image_type"' not in prompt             # not the classify prompt

    def test_skip_gate_falls_back_to_two_calls(self, tmp_path, monkeypatch, mock_chroma):
        import pka.ingestion.image_extractor as extractor
        import pka.ingestion.image_gate as gate
        from pka.ingestion.image_pipeline import ingest_image

        def _boom():
            raise AssertionError("gate should be skipped")

        monkeypatch.setattr(gate, "_get_easyocr", _boom)
        main = MagicMock()
        main.complete.side_effect = [
            '{"image_type": "poster", "description": "A conference poster."}',
            '{"content": "a study of retrieval quality", "description": "A poster."}',
        ]
        monkeypatch.setattr(extractor, "get_vision_provider", lambda: main)
        monkeypatch.setattr("pka.ingestion.image_pipeline.ocr_image", lambda p, lang="eng": "")
        monkeypatch.setattr("pka.ingestion.image_pipeline.clip_embed_image", lambda p: None)

        res = ingest_image(_image_file(tmp_path / "poster.png"), skip_gate=True)

        assert res["image_type"] == "poster"
        assert main.complete.call_count == 2            # classify, then content prompt
        assert '"image_type"' in main.complete.call_args_list[0][0][0]
        assert '"content"' in main.complete.call_args_list[1][0][0]


class TestClassifyStrict:
    """The gate's step-2 classifier must distinguish a backend outage (raise)
    from a genuine 'unknown' verdict (return, so the gate legitimately rejects)."""

    def test_strict_raises_on_backend_error(self, sample_png):
        from pka.ingestion.image_extractor import VisionUnavailable, classify_and_describe

        prov = MagicMock()
        prov.complete.side_effect = RuntimeError("backend down")
        with pytest.raises(VisionUnavailable):
            classify_and_describe(sample_png, provider=prov, strict=True)

    def test_nonstrict_degrades_on_backend_error(self, sample_png):
        from pka.ingestion.image_extractor import classify_and_describe

        prov = MagicMock()
        prov.complete.side_effect = RuntimeError("backend down")
        assert classify_and_describe(sample_png, provider=prov) == ("unknown", "")

    def test_strict_returns_genuine_unknown_without_raising(self, sample_png):
        """A successful call that classifies 'unknown' is a real result, not an
        outage — it must return so the gate can reject it, not raise."""
        from pka.ingestion.image_extractor import classify_and_describe

        prov = MagicMock()
        prov.complete.return_value = '{"image_type": "unknown", "description": "a wall"}'
        assert classify_and_describe(sample_png, provider=prov, strict=True) == (
            "unknown", "a wall",
        )


class TestVisionEncodingOrientation:
    def test_encode_image_applies_exif_transpose(self, tmp_path):
        """The VLM encoder must upright EXIF-rotated photos too, so the gate's
        classifier and the describe pass don't see them sideways."""
        import base64
        import io

        from PIL import Image as PILImg

        from pka.ingestion.image_extractor import _encode_image

        p = tmp_path / "rot.jpg"
        im = PILImg.new("RGB", (120, 60), "white")
        exif = im.getexif()
        exif[274] = 6  # rotate 90° CW → decoded image should be 60x120
        im.save(p, exif=exif)

        decoded = PILImg.open(io.BytesIO(base64.b64decode(_encode_image(p))))
        assert decoded.size == (60, 120)  # transposed from the stored 120x60


class TestBrowseDefersPendingImages:
    @pytest.fixture(autouse=True)
    def _db(self):
        init_db()

    def test_pending_image_hidden_until_indexed(self, tmp_path):
        import sqlalchemy as sa

        from pka.db.queries import get_engine, list_documents
        from pka.db.schema import images
        from pka.ingestion.image_pipeline import register_images

        img = _image_file(tmp_path / "pending.png")
        register_images([img])  # indexed_at IS NULL → still ingesting

        _total, items = list_documents(sources=["image"])
        assert all(i["url_or_path"] != str(img.path) for i in items)

        # Once the embed pass sets indexed_at, it appears in browse.
        with get_engine().begin() as con:
            con.execute(
                sa.update(images)
                .where(images.c.path == str(img.path))
                .values(indexed_at=1)
            )
        _total, items = list_documents(sources=["image"])
        assert any(i["url_or_path"] == str(img.path) for i in items)

    async def test_list_images_hides_pending(self, tmp_path):
        import sqlalchemy as sa

        from pka.api.routers.images import list_images
        from pka.db.queries import get_engine
        from pka.db.schema import images
        from pka.ingestion.image_pipeline import register_images

        img = _image_file(tmp_path / "gallery.png")
        register_images([img])  # indexed_at IS NULL

        eng = get_engine()
        # Called directly (not via FastAPI), so pass the Query-defaulted args.
        assert await list_images(image_type=None, limit=20, offset=0, engine=eng) == []

        with eng.begin() as con:
            con.execute(
                sa.update(images)
                .where(images.c.path == str(img.path))
                .values(indexed_at=1)
            )
        out = await list_images(image_type=None, limit=20, offset=0, engine=eng)
        assert len(out) == 1
