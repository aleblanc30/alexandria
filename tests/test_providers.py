"""Provider registry dispatch + OpenAI-compatible backend behaviour (HTTP mocked)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pka.providers as providers
from pka.providers.clip import ClipImageEmbedder
from pka.providers.easy_ocr import EasyOcrProvider
from pka.providers.ollama import OllamaChatProvider, OllamaVisionProvider
from pka.providers.openai_compat import (
    OpenAICompatChatProvider,
    OpenAICompatVisionProvider,
)
from pka.providers.vlm_ocr import VlmOcrProvider

# ── Registry dispatch ─────────────────────────────────────────────────────────


class TestRegistryDispatch:
    def test_defaults_are_local_backends(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "chat_provider", "ollama")
        monkeypatch.setattr(providers.cfg, "vision_provider", "ollama")
        monkeypatch.setattr(providers.cfg, "ocr_provider", "easyocr")
        monkeypatch.setattr(providers.cfg, "image_embed_provider", "clip")
        providers.reset_providers()

        assert isinstance(providers.get_chat_provider(), OllamaChatProvider)
        assert isinstance(providers.get_vision_provider(), OllamaVisionProvider)
        assert isinstance(providers.get_ocr_provider(), EasyOcrProvider)
        assert isinstance(providers.get_image_embedder(), ClipImageEmbedder)

    def test_chat_provider_is_cached(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "chat_provider", "ollama")
        providers.reset_providers()
        assert providers.get_chat_provider() is providers.get_chat_provider()

    def test_openrouter_chat_uses_configured_endpoint(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "chat_provider", "openrouter")
        monkeypatch.setattr(providers.cfg, "openrouter_base_url", "https://openrouter.ai/api/v1")
        monkeypatch.setattr(providers.cfg, "openrouter_api_key", "sk-or-test")
        monkeypatch.setattr(providers.cfg, "openrouter_chat_model", "openai/gpt-4o-mini")
        providers.reset_providers()

        p = providers.get_chat_provider()
        assert isinstance(p, OpenAICompatChatProvider)
        assert p.base_url == "https://openrouter.ai/api/v1"
        assert p.model == "openai/gpt-4o-mini"
        assert p.label == "openrouter"

    def test_ovh_vision_uses_configured_endpoint(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "vision_provider", "ovh")
        monkeypatch.setattr(providers.cfg, "ovh_base_url", "https://ovh.example/v1")
        monkeypatch.setattr(providers.cfg, "ovh_api_key", "ovh-test")
        monkeypatch.setattr(providers.cfg, "ovh_vision_model", "vision-x")
        providers.reset_providers()

        p = providers.get_vision_provider()
        assert isinstance(p, OpenAICompatVisionProvider)
        assert p.base_url == "https://ovh.example/v1"
        assert p.model == "vision-x"

    def test_scaleway_chat_uses_configured_endpoint(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "chat_provider", "scaleway")
        monkeypatch.setattr(providers.cfg, "scaleway_base_url", "https://api.scaleway.ai/v1")
        monkeypatch.setattr(providers.cfg, "scaleway_api_key", "scw-test")
        monkeypatch.setattr(providers.cfg, "scaleway_chat_model", "llama-3.3-70b-instruct")
        providers.reset_providers()

        p = providers.get_chat_provider()
        assert isinstance(p, OpenAICompatChatProvider)
        assert p.base_url == "https://api.scaleway.ai/v1"
        assert p.model == "llama-3.3-70b-instruct"
        assert p.label == "scaleway"

    def test_ollama_cloud_chat_uses_hosted_endpoint(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "chat_provider", "ollama_cloud")
        monkeypatch.setattr(providers.cfg, "ollama_cloud_base_url", "https://ollama.com")
        monkeypatch.setattr(providers.cfg, "ollama_cloud_api_key", "oll-test")
        monkeypatch.setattr(providers.cfg, "ollama_cloud_chat_model", "gpt-oss:120b")
        providers.reset_providers()

        p = providers.get_chat_provider()
        assert isinstance(p, OllamaChatProvider)
        assert p.base_url == "https://ollama.com"
        assert p.resolve_model() == "gpt-oss:120b"
        assert p.remote is True

    def test_ollama_cloud_gate_vision_bakes_in_gate_model(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "image_gate_vision_provider", "ollama_cloud")
        monkeypatch.setattr(providers.cfg, "ollama_cloud_api_key", "oll-test")
        monkeypatch.setattr(providers.cfg, "image_gate_vision_model", "qwen3.5:397b")
        providers.reset_providers()

        p = providers.get_gate_vision_provider()
        assert isinstance(p, OllamaVisionProvider)
        assert p.model == "qwen3.5:397b"

    def test_ocr_provider_vlm_selects_vision_backend(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "ocr_provider", "vlm")
        providers.reset_providers()
        assert isinstance(providers.get_ocr_provider(), VlmOcrProvider)

    def test_unknown_provider_raises(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "chat_provider", "bogus")
        providers.reset_providers()
        with pytest.raises(ValueError, match="Unknown chat provider"):
            providers.get_chat_provider()

    def test_unknown_ocr_provider_raises(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "ocr_provider", "bogus")
        providers.reset_providers()
        with pytest.raises(ValueError, match="Unknown OCR provider"):
            providers.get_ocr_provider()


# ── VLM OCR provider ──────────────────────────────────────────────────────────


class TestVlmOcr:
    def _patch_vision(self, monkeypatch, content: str):
        """Stub the vision provider + image encoding so no HTTP/PIL is touched."""
        fake = MagicMock()
        fake.complete.return_value = content
        monkeypatch.setattr("pka.providers.vlm_ocr.get_vision_provider", lambda: fake)
        monkeypatch.setattr(
            "pka.ingestion.image_extractor._encode_image", lambda p, max_px=1024: "QUJD"
        )
        return fake

    def test_parses_transcribed_text(self, monkeypatch):
        fake = self._patch_vision(monkeypatch, '{"text": "Hello\\nWorld"}')
        out = VlmOcrProvider().ocr(Path("slide.png"))
        assert out == "Hello\nWorld"
        # Uses the vision model, not a local OCR engine.
        assert fake.complete.called

    def test_salvages_text_from_invalid_json(self, monkeypatch):
        # Unescaped inner quotes break strict JSON; the field is still recovered.
        self._patch_vision(monkeypatch, '{"text": "He said "hi" loudly"}')
        assert VlmOcrProvider().ocr(Path("slide.png")) == 'He said "hi" loudly'

    def test_returns_empty_on_failure(self, monkeypatch):
        fake = MagicMock()
        fake.complete.side_effect = RuntimeError("boom")
        monkeypatch.setattr("pka.providers.vlm_ocr.get_vision_provider", lambda: fake)
        monkeypatch.setattr(
            "pka.ingestion.image_extractor._encode_image", lambda p, max_px=1024: "QUJD"
        )
        assert VlmOcrProvider().ocr(Path("slide.png")) == ""


# ── EasyOCR provider ──────────────────────────────────────────────────────────


class TestEasyOcr:
    def test_lang_codes_are_mapped(self):
        from pka.providers.easy_ocr import _to_easyocr_langs

        assert _to_easyocr_langs("eng") == ["en"]
        assert _to_easyocr_langs("eng+fra") == ["en", "fr"]
        assert _to_easyocr_langs("en") == ["en"]  # native code passes through
        assert _to_easyocr_langs("") == ["en"]  # empty ⇒ default

    def test_ocr_joins_recognised_lines(self, monkeypatch):
        reader = MagicMock()
        reader.readtext.return_value = ["Line one", "  Line two  "]
        monkeypatch.setattr(EasyOcrProvider, "_reader", lambda self, langs: reader)
        # ocr() decodes the file (EXIF-orienting it) before OCR; stub that out so
        # the test exercises only the line-joining, not image loading.
        monkeypatch.setattr(
            "pka.providers.easy_ocr._oriented_rgb_array", lambda path: object()
        )
        assert EasyOcrProvider().ocr(Path("slide.png")) == "Line one\nLine two"

    def test_ocr_returns_empty_on_failure(self, monkeypatch):
        def _boom(self, langs):
            raise RuntimeError("model load failed")

        monkeypatch.setattr(EasyOcrProvider, "_reader", _boom)
        assert EasyOcrProvider().ocr(Path("slide.png")) == ""

    # ── Missing-install detection (must surface, never silently degrade) ───────

    def test_ensure_available_raises_when_wheel_missing(self, monkeypatch):
        import importlib.util

        from pka.providers.easy_ocr import EasyOcrUnavailable, ensure_easyocr_available

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        with pytest.raises(EasyOcrUnavailable):
            ensure_easyocr_available()

    def test_ensure_available_passes_when_wheel_present(self, monkeypatch):
        import importlib.util

        from pka.providers.easy_ocr import ensure_easyocr_available

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        ensure_easyocr_available()  # no raise

    def test_text_coverage_raises_when_unavailable(self, tmp_path, monkeypatch):
        """A missing install must raise, not return 0.0 (which the gate reads as
        'reject')."""
        from PIL import Image as PILImg

        from pka.providers.easy_ocr import EasyOcrProvider, EasyOcrUnavailable

        img = tmp_path / "x.png"
        PILImg.new("RGB", (40, 40), "white").save(img)

        def _unavailable(self, langs):
            from pka.providers.easy_ocr import EasyOcrUnavailable as _U
            raise _U("no easyocr")

        monkeypatch.setattr(EasyOcrProvider, "_reader", _unavailable)
        with pytest.raises(EasyOcrUnavailable):
            EasyOcrProvider().text_coverage(img)

    def test_ocr_raises_when_unavailable(self, tmp_path, monkeypatch):
        from PIL import Image as PILImg

        from pka.providers.easy_ocr import EasyOcrProvider, EasyOcrUnavailable

        img = tmp_path / "x.png"
        PILImg.new("RGB", (40, 40), "white").save(img)

        def _unavailable(self, langs):
            raise EasyOcrUnavailable("no easyocr")

        monkeypatch.setattr(EasyOcrProvider, "_reader", _unavailable)
        with pytest.raises(EasyOcrUnavailable):
            EasyOcrProvider().ocr(img)

    # ── EXIF orientation handling ─────────────────────────────────────────────

    def test_oriented_rgb_array_applies_exif_transpose(self, tmp_path):
        """orientation=6 (rotate 90° CW) landscape → portrait array."""
        from PIL import Image as PILImg

        from pka.providers.easy_ocr import _oriented_rgb_array

        p = tmp_path / "rot.jpg"
        im = PILImg.new("RGB", (120, 60), "white")
        exif = im.getexif()
        exif[274] = 6
        im.save(p, exif=exif)

        arr = _oriented_rgb_array(p)
        assert arr.shape[:2] == (120, 60)  # H, W swapped from the stored 60x120


# ── OpenAI-compatible chat ────────────────────────────────────────────────────


class TestOpenAICompatChat:
    def _resp(self, content='{"label": "AI"}'):
        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        return FakeResp()

    def test_payload_and_headers(self, monkeypatch):
        captured = {}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers)
            return self._resp()

        monkeypatch.setattr("pka.providers.openai_compat.httpx.post", _post)
        p = OpenAICompatChatProvider(
            base_url="https://host/v1", api_key="key-123", model="m1", label="openrouter"
        )
        data, err = p.chat_json("prompt", temperature=0.2)

        assert err is None
        assert data["label"] == "AI"
        assert captured["url"] == "https://host/v1/chat/completions"
        assert captured["json"]["model"] == "m1"
        assert captured["json"]["response_format"] == {"type": "json_object"}
        assert captured["json"]["temperature"] == 0.2
        assert captured["headers"]["Authorization"] == "Bearer key-123"

    def test_missing_model_errors_without_http(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("must not call HTTP without a model")

        monkeypatch.setattr("pka.providers.openai_compat.httpx.post", _boom)
        p = OpenAICompatChatProvider(base_url="https://host/v1", api_key="k", model="")
        data, err = p.chat_json("prompt")
        assert data == {}
        assert "No chat model" in err

    def test_missing_key_errors_without_http(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("must not call HTTP without a key")

        monkeypatch.setattr("pka.providers.openai_compat.httpx.post", _boom)
        p = OpenAICompatChatProvider(base_url="https://host/v1", api_key="", model="m1")
        data, err = p.chat_json("prompt")
        assert data == {}
        assert "No API key" in err

    def test_http_failure_returns_error(self, monkeypatch):
        monkeypatch.setattr(
            "pka.providers.openai_compat.httpx.post",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        p = OpenAICompatChatProvider(base_url="https://host/v1", api_key="k", model="m1")
        data, err = p.chat_json("prompt")
        assert data == {}
        assert "boom" in err


# ── OpenAI-compatible vision ──────────────────────────────────────────────────


class TestOpenAICompatVision:
    def test_sends_data_uri_and_returns_content(self, monkeypatch):
        captured = {}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(json=json, headers=headers)

            class FakeResp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": '{"image_type": "slide"}'}}]}

            return FakeResp()

        monkeypatch.setattr("pka.providers.openai_compat.httpx.post", _post)
        p = OpenAICompatVisionProvider(
            base_url="https://host/v1", api_key="k", model="vision-x", label="ovh"
        )
        content = p.complete("describe", "QUJD", model="ignored-ollama-name")

        assert content == '{"image_type": "slide"}'
        parts = captured["json"]["messages"][0]["content"]
        img_part = next(p for p in parts if p["type"] == "image_url")
        assert img_part["image_url"]["url"] == "data:image/jpeg;base64,QUJD"
        assert captured["json"]["model"] == "vision-x"  # configured model wins

    def test_missing_model_raises(self):
        p = OpenAICompatVisionProvider(base_url="https://host/v1", api_key="k", model="")
        with pytest.raises(ValueError, match="No vision model"):
            p.complete("describe", "QUJD")


# ── Ollama Cloud (native API against ollama.com) ──────────────────────────────


def _cloud_chat(**kw):
    return OllamaChatProvider(
        base_url="https://ollama.com", api_key="oll-key", label="ollama_cloud",
        remote=True, **kw
    )


class TestOllamaCloudChat:
    def test_sends_bearer_key_to_hosted_api_chat(self, monkeypatch):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": '{"label": "AI"}'}}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers)
            return FakeResp()

        monkeypatch.setattr("pka.providers.ollama.httpx.post", _post)
        data, err = _cloud_chat(model="gpt-oss:120b").chat_json("prompt")

        assert err is None
        assert data["label"] == "AI"
        # Cloud speaks the *native* Ollama API, not /v1/chat/completions.
        assert captured["url"] == "https://ollama.com/api/chat"
        assert captured["json"]["model"] == "gpt-oss:120b"
        assert captured["json"]["format"] == "json"
        assert captured["headers"]["Authorization"] == "Bearer oll-key"

    def test_never_falls_back_to_local_chat_model(self, monkeypatch):
        """A local model name must not be sent to the hosted endpoint."""
        monkeypatch.setattr("pka.providers.ollama.cfg.chat_model", "llava")

        def _boom(*a, **k):
            raise AssertionError("must not call HTTP without a cloud model")

        monkeypatch.setattr("pka.providers.ollama.httpx.post", _boom)
        monkeypatch.setattr("pka.providers.ollama.httpx.get", _boom)
        p = _cloud_chat(model="")
        assert p.resolve_model() == ""
        data, err = p.chat_json("prompt")
        assert data == {}
        assert "No chat model" in err

    def test_missing_key_errors_without_http(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("must not call HTTP without a key")

        monkeypatch.setattr("pka.providers.ollama.httpx.post", _boom)
        p = OllamaChatProvider(
            base_url="https://ollama.com", api_key="", model="gpt-oss:120b",
            label="ollama_cloud", remote=True,
        )
        data, err = p.chat_json("prompt")
        assert data == {}
        assert "No API key" in err


class TestOllamaCloudVision:
    def test_configured_model_wins_over_plumbed_default(self, monkeypatch):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": '{"image_type": "slide"}'}}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers)
            return FakeResp()

        monkeypatch.setattr("pka.providers.ollama.httpx.post", _post)
        p = OllamaVisionProvider(
            base_url="https://ollama.com", api_key="oll-key", model="qwen3.5:397b",
            label="ollama_cloud", remote=True,
        )
        content = p.complete("describe", "QUJD", model="moondream")

        assert content == '{"image_type": "slide"}'
        assert captured["json"]["model"] == "qwen3.5:397b"  # not the local "moondream"
        assert captured["json"]["messages"][0]["images"] == ["QUJD"]
        assert captured["headers"]["Authorization"] == "Bearer oll-key"

    def test_local_provider_still_honours_per_call_model(self, monkeypatch):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": "{}"}}

        def _post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json)
            return FakeResp()

        monkeypatch.setattr("pka.providers.ollama.httpx.post", _post)
        OllamaVisionProvider().complete("describe", "QUJD", model="moondream")

        assert captured["json"]["model"] == "moondream"
        assert captured["url"].endswith("/api/chat")
