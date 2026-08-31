"""Swappable model backends behind per-capability accessors.

Every LLM / vision / OCR / image-embedding call in Alexandria goes through one
of the ``get_*_provider`` accessors here. Which concrete backend they return is
driven by config (``chat_provider``, ``vision_provider``, ``ocr_provider``,
``image_embed_provider``), so callers never import a backend directly.

Instances are cached for the process lifetime; tests reset them via
:func:`reset_providers` (wired into ``conftest.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from pka.config import settings as cfg
from pka.providers.base import ChatProvider, ImageEmbedder, OcrProvider, VisionProvider

log = logging.getLogger(__name__)

_chat: ChatProvider | None = None
_vision: VisionProvider | None = None
_gate_vision: VisionProvider | None = None
_ocr: OcrProvider | None = None
_embedder: ImageEmbedder | None = None


def reset_providers() -> None:
    """Drop cached provider instances — used by the test suite."""
    global _chat, _vision, _gate_vision, _ocr, _embedder
    _chat = _vision = _gate_vision = _ocr = _embedder = None


# ── Remote (OpenAI-compatible) config resolution ─────────────────────────────


def _openai_compat_config(name: str) -> dict[str, str]:
    """Return ``{base_url, api_key, chat_model, vision_model}`` for a remote provider."""
    if name == "openrouter":
        return {
            "base_url": cfg.openrouter_base_url,
            "api_key": cfg.openrouter_api_key,
            "chat_model": cfg.openrouter_chat_model,
            "vision_model": cfg.openrouter_vision_model,
        }
    if name == "ovh":
        return {
            "base_url": cfg.ovh_base_url,
            "api_key": cfg.ovh_api_key,
            "chat_model": cfg.ovh_chat_model,
            "vision_model": cfg.ovh_vision_model,
        }
    if name == "scaleway":
        return {
            "base_url": cfg.scaleway_base_url,
            "api_key": cfg.scaleway_api_key,
            "chat_model": cfg.scaleway_chat_model,
            "vision_model": cfg.scaleway_vision_model,
        }
    raise ValueError(f"Unknown OpenAI-compatible provider: {name!r}")


def _ollama_cloud_kwargs(model: str) -> dict[str, Any]:
    """Constructor kwargs for a hosted (ollama.com) Ollama provider."""
    return {
        "base_url": cfg.ollama_cloud_base_url,
        "api_key": cfg.ollama_cloud_api_key,
        "model": model,
        "label": "ollama_cloud",
        "remote": True,
    }


# ── Builders ─────────────────────────────────────────────────────────────────


def _build_chat(name: str) -> ChatProvider:
    if name == "ollama":
        from pka.providers.ollama import OllamaChatProvider

        return OllamaChatProvider()
    if name == "ollama_cloud":
        from pka.providers.ollama import OllamaChatProvider

        return OllamaChatProvider(**_ollama_cloud_kwargs(cfg.ollama_cloud_chat_model))
    if name in ("openrouter", "ovh", "scaleway"):
        from pka.providers.openai_compat import OpenAICompatChatProvider

        conf = _openai_compat_config(name)
        return OpenAICompatChatProvider(
            base_url=conf["base_url"],
            api_key=conf["api_key"],
            model=conf["chat_model"],
            label=name,
        )
    raise ValueError(
        f"Unknown chat provider: {name!r} (expected ollama|ollama_cloud|openrouter|ovh|scaleway)"
    )


def _build_vision(name: str) -> VisionProvider:
    if name == "ollama":
        from pka.providers.ollama import OllamaVisionProvider

        return OllamaVisionProvider()
    if name == "ollama_cloud":
        from pka.providers.ollama import OllamaVisionProvider

        return OllamaVisionProvider(**_ollama_cloud_kwargs(cfg.ollama_cloud_vision_model))
    if name in ("openrouter", "ovh", "scaleway"):
        from pka.providers.openai_compat import OpenAICompatVisionProvider

        conf = _openai_compat_config(name)
        return OpenAICompatVisionProvider(
            base_url=conf["base_url"],
            api_key=conf["api_key"],
            model=conf["vision_model"],
            label=name,
        )
    raise ValueError(
        f"Unknown vision provider: {name!r} (expected ollama|ollama_cloud|openrouter|ovh|scaleway)"
    )


def _build_gate_vision(name: str) -> VisionProvider:
    """Vision provider for the admission gate's fast classifier.

    Distinct from :func:`_build_vision`: for remote backends the gate model is
    baked in at construction, because a constructed ``self.model`` wins over a
    per-call override. Local Ollama takes the model per call, so its provider is
    plain and the gate passes ``image_gate_vision_model`` at call time.
    """
    if name == "ollama":
        from pka.providers.ollama import OllamaVisionProvider

        return OllamaVisionProvider()
    if name == "ollama_cloud":
        from pka.providers.ollama import OllamaVisionProvider

        return OllamaVisionProvider(**_ollama_cloud_kwargs(cfg.image_gate_vision_model))
    if name in ("openrouter", "ovh", "scaleway"):
        from pka.providers.openai_compat import OpenAICompatVisionProvider

        conf = _openai_compat_config(name)
        return OpenAICompatVisionProvider(
            base_url=conf["base_url"],
            api_key=conf["api_key"],
            model=cfg.image_gate_vision_model,
            label=f"{name}-gate",
        )
    raise ValueError(
        f"Unknown gate vision provider: {name!r} "
        "(expected ollama|ollama_cloud|openrouter|ovh|scaleway)"
    )


def _build_ocr(name: str) -> OcrProvider:
    if name == "easyocr":
        from pka.providers.easy_ocr import EasyOcrProvider

        return EasyOcrProvider()
    if name == "vlm":
        from pka.providers.vlm_ocr import VlmOcrProvider

        return VlmOcrProvider()
    raise ValueError(f"Unknown OCR provider: {name!r} (expected vlm|easyocr)")


def _build_embedder(name: str) -> ImageEmbedder:
    if name == "clip":
        from pka.providers.clip import ClipImageEmbedder

        return ClipImageEmbedder()
    raise ValueError(f"Unknown image-embed provider: {name!r} (expected clip)")


# ── Accessors ────────────────────────────────────────────────────────────────


def get_chat_provider() -> ChatProvider:
    global _chat
    if _chat is None:
        _chat = _build_chat(cfg.chat_provider)
    return _chat


def get_vision_provider() -> VisionProvider:
    global _vision
    if _vision is None:
        _vision = _build_vision(cfg.vision_provider)
    return _vision


def get_gate_vision_provider() -> VisionProvider:
    global _gate_vision
    if _gate_vision is None:
        _gate_vision = _build_gate_vision(cfg.image_gate_vision_provider)
    return _gate_vision


def get_ocr_provider() -> OcrProvider:
    global _ocr
    if _ocr is None:
        _ocr = _build_ocr(cfg.ocr_provider)
    return _ocr


def get_image_embedder() -> ImageEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = _build_embedder(cfg.image_embed_provider)
    return _embedder


__all__ = [
    "ChatProvider",
    "VisionProvider",
    "OcrProvider",
    "ImageEmbedder",
    "get_chat_provider",
    "get_vision_provider",
    "get_gate_vision_provider",
    "get_ocr_provider",
    "get_image_embedder",
    "reset_providers",
]
