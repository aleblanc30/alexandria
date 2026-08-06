"""Ollama-backed chat and vision providers (local ``/api/chat``)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_EMBED_MARKERS = ("embed", "nomic-embed", "mxbai-embed", "snowflake-arctic-embed")
_cached_chat_model: str | None = None


def _is_chat_model(name: str) -> bool:
    lower = name.lower()
    return not any(marker in lower for marker in _EMBED_MARKERS)


class OllamaChatProvider:
    """Chat completions via Ollama, grammar-constrained to JSON."""

    def resolve_model(self, explicit: str | None = None) -> str:
        """Return the chat model, auto-detecting from Ollama when unset."""
        global _cached_chat_model

        if explicit:
            return explicit
        if cfg.chat_model:
            return cfg.chat_model
        if _cached_chat_model:
            return _cached_chat_model

        try:
            resp = httpx.get(f"{cfg.ollama_base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            for entry in resp.json().get("models", []):
                name = entry.get("name", "")
                if name and _is_chat_model(name):
                    _cached_chat_model = name
                    log.info("Auto-selected Ollama chat model: %s", name)
                    return name
        except Exception as exc:
            log.warning("Could not list Ollama models: %s", exc)

        return cfg.chat_model or "llama3"

    def chat_json(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout: float = 90,
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        from pka.json_utils import parse_llm_json

        chosen = self.resolve_model(model)
        payload: dict[str, Any] = {
            "model": chosen,
            "messages": [{"role": "user", "content": prompt}],
            # Grammar-constrain the reply to valid JSON so quotes/newlines in the
            # generated content don't produce an unparseable response. Every caller
            # of chat_json prompts for and parses JSON.
            "format": "json",
            "stream": False,
        }
        if temperature is not None:
            payload["options"] = {"temperature": temperature}
        try:
            resp = httpx.post(
                f"{cfg.ollama_base_url}/api/chat",
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body.get("message", {}).get("content", "")
            if not content.strip():
                return {}, f"Empty response from model {chosen}"
            return parse_llm_json(content), None
        except Exception as exc:
            log.warning("Ollama chat failed (model=%s): %s", chosen, exc)
            return {}, str(exc)


class OllamaVisionProvider:
    """Vision completions via Ollama (image passed in the ``images`` array)."""

    def complete(
        self,
        prompt: str,
        image_b64: str,
        *,
        model: str | None = None,
        timeout: float = 120,
    ) -> str:
        chosen = model or cfg.vision_model
        resp = httpx.post(
            f"{cfg.ollama_base_url}/api/chat",
            json={
                "model": chosen,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [image_b64],
                    }
                ],
                # Grammar-constrain the output to valid JSON so descriptions
                # containing quotes don't produce unparseable responses.
                "format": "json",
                "stream": False,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
