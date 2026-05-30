"""Unit tests for image extraction helpers (no real Ollama/CLIP/Tesseract)."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image as PILImage


@pytest.fixture()
def sample_png(tmp_path) -> Path:
    p = tmp_path / "photo.png"
    PILImage.new("RGB", (800, 600), color="red").save(p)
    return p


class TestParseLlmJson:
    def test_parses_plain_json(self):
        from pka.ingestion.image_extractor import _parse_llm_json
        raw = '{"image_type": "slide", "description": "A slide."}'
        assert _parse_llm_json(raw)["image_type"] == "slide"

    def test_strips_markdown_fences(self):
        from pka.ingestion.image_extractor import _parse_llm_json
        raw = '```json\n{"image_type": "poster", "description": "Poster."}\n```'
        assert _parse_llm_json(raw)["image_type"] == "poster"

    def test_extracts_embedded_object(self):
        from pka.ingestion.image_extractor import _parse_llm_json
        raw = 'Here is the result: {"image_type": "notes", "description": "Notes."}'
        assert _parse_llm_json(raw)["image_type"] == "notes"


class TestEncodeImage:
    def test_returns_base64_string(self, sample_png):
        from pka.ingestion.image_extractor import _encode_image
        b64 = _encode_image(sample_png)
        assert isinstance(b64, str)
        assert len(b64) > 20

    def test_downscales_large_images(self, tmp_path):
        from pka.ingestion.image_extractor import _encode_image
        p = tmp_path / "big.png"
        PILImage.new("RGB", (3000, 2000), color="blue").save(p)
        b64 = _encode_image(p, max_px=512)
        assert len(b64) > 0


class TestClassifyAndDescribe:
    def test_success(self, sample_png, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "message": {"content": '{"image_type": "slide", "description": "ML slide."}'},
        }
        monkeypatch.setattr("pka.ingestion.image_extractor.httpx.post", lambda *a, **kw: resp)

        from pka.ingestion.image_extractor import classify_and_describe
        itype, desc = classify_and_describe(sample_png)
        assert itype == "slide"
        assert "ML" in desc

    def test_invalid_type_becomes_unknown(self, sample_png, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "message": {"content": '{"image_type": "not_valid", "description": "x"}'},
        }
        monkeypatch.setattr("pka.ingestion.image_extractor.httpx.post", lambda *a, **kw: resp)

        from pka.ingestion.image_extractor import classify_and_describe
        itype, _ = classify_and_describe(sample_png)
        assert itype == "unknown"

    def test_failure_returns_unknown(self, sample_png, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.image_extractor.httpx.post",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("down")),
        )
        from pka.ingestion.image_extractor import classify_and_describe
        itype, desc = classify_and_describe(sample_png)
        assert itype == "unknown"
        assert desc == ""


class TestOcrImage:
    def test_success(self, sample_png, monkeypatch):
        monkeypatch.setattr(
            "pytesseract.image_to_string",
            lambda img, lang="eng": "  Hello OCR  ",
        )
        from pka.ingestion.image_extractor import ocr_image
        assert ocr_image(sample_png) == "Hello OCR"

    def test_failure_returns_empty(self, sample_png, monkeypatch):
        monkeypatch.setattr(
            "pytesseract.image_to_string",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ocr fail")),
        )
        from pka.ingestion.image_extractor import ocr_image
        assert ocr_image(sample_png) == ""


class TestClipEmbed:
    class _FakeTensor:
        def __init__(self, values):
            self._values = values

        def norm(self, **kw):
            return self

        def __truediv__(self, other):
            return self

        def squeeze(self):
            return self

        def tolist(self):
            return self._values

    def _mock_torch(self, monkeypatch):
        monkeypatch.setattr(
            "torch.no_grad",
            lambda: MagicMock(__enter__=MagicMock(return_value=None),
                              __exit__=MagicMock(return_value=False)),
        )

    def test_image_embedding(self, sample_png, monkeypatch):
        self._mock_torch(monkeypatch)
        fake_model = MagicMock()
        fake_model.get_image_features.return_value = self._FakeTensor([0.1, 0.2, 0.3])
        fake_processor = MagicMock(return_value={"pixel_values": "x"})

        monkeypatch.setattr(
            "pka.ingestion.image_extractor._load_clip",
            lambda: (fake_model, fake_processor),
        )

        from pka.ingestion.image_extractor import clip_embed_image
        assert clip_embed_image(sample_png) == [0.1, 0.2, 0.3]

    def test_text_embedding(self, monkeypatch):
        self._mock_torch(monkeypatch)
        fake_model = MagicMock()
        fake_model.get_text_features.return_value = self._FakeTensor([0.5, 0.6])
        fake_processor = MagicMock(return_value={"input_ids": "x"})

        monkeypatch.setattr(
            "pka.ingestion.image_extractor._load_clip",
            lambda: (fake_model, fake_processor),
        )

        from pka.ingestion.image_extractor import clip_embed_text
        assert clip_embed_text("query") == [0.5, 0.6]

    def test_clip_failure_returns_none(self, sample_png, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.image_extractor._load_clip",
            lambda: (_ for _ in ()).throw(RuntimeError("clip")),
        )
        from pka.ingestion.image_extractor import clip_embed_image
        assert clip_embed_image(sample_png) is None


class TestEmbedImageText:
    def test_combines_and_embeds(self, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.embedder.embed_one",
            lambda t: [0.1] * 8,
        )
        from pka.ingestion.image_extractor import embed_image_text
        vec = embed_image_text("OCR text", "Description here")
        assert vec == [0.1] * 8

    def test_empty_input_returns_none(self):
        from pka.ingestion.image_extractor import embed_image_text
        assert embed_image_text("", "") is None

    def test_embed_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.embedder.embed_one",
            lambda t: (_ for _ in ()).throw(RuntimeError("embed")),
        )
        from pka.ingestion.image_extractor import embed_image_text
        assert embed_image_text("text", "") is None
