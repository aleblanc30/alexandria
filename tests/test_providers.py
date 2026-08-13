"""Provider registry dispatch + OpenAI-compatible backend behaviour (HTTP mocked)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pka.providers as providers
from pka.providers.clip import ClipImageEmbedder
from pka.providers.ollama import OllamaChatProvider, OllamaVisionProvider
from pka.providers.openai_compat import (
    OpenAICompatChatProvider,
    OpenAICompatVisionProvider,
)
from pka.providers.tesseract import TesseractOcrProvider
from pka.providers.vlm_ocr import VlmOcrProvider

# ── Registry dispatch ─────────────────────────────────────────────────────────


class TestRegistryDispatch:
    def test_defaults_are_local_backends(self, monkeypatch):
        monkeypatch.setattr(providers.cfg, "chat_provider", "ollama")
        monkeypatch.setattr(providers.cfg, "vision_provider", "ollama")
        monkeypatch.setattr(providers.cfg, "ocr_provider", "tesseract")
        monkeypatch.setattr(providers.cfg, "image_embed_provider", "clip")
        providers.reset_providers()

        assert isinstance(providers.get_chat_provider(), OllamaChatProvider)
        assert isinstance(providers.get_vision_provider(), OllamaVisionProvider)
        assert isinstance(providers.get_ocr_provider(), TesseractOcrProvider)
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
        # Uses the vision model, not Tesseract.
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
