"""Ollama-backed chat and vision providers (native ``/api/chat``).

One implementation covers two deployments, because Ollama Cloud speaks the same
native API as the local daemon — only the host and an ``Authorization`` header
differ:

* **local** (``ollama``) — zero-arg construction. Talks to ``ollama_base_url``,
  no auth, and resolves the chat model from ``cfg.chat_model`` or by probing
  ``/api/tags``.
* **cloud** (``ollama_cloud``) — constructed with ``remote=True`` plus a
  ``base_url``, ``api_key`` and explicit ``model``. Auto-detection is off, and a
  missing key or model is reported instead of guessed, so a local model name can
  never be sent to the hosted endpoint.
"""

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


class _OllamaEndpoint:
    """Shared host/auth resolution for the native Ollama API."""

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        label: str = "ollama",
        remote: bool = False,
    ):
        self._base_url = base_url
        self.api_key = api_key
        self.model = model
        self.label = label
        self.remote = remote

    @property
    def base_url(self) -> str:
        # Resolved per call rather than frozen at construction so the local
        # provider still follows a monkeypatched/updated ``ollama_base_url``.
        return (self._base_url or cfg.ollama_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


class OllamaChatProvider(_OllamaEndpoint):
    """Chat completions via Ollama, grammar-constrained to JSON."""

    def resolve_model(self, explicit: str | None = None) -> str:
        """Return the chat model, auto-detecting from Ollama when unset.

        Remote (cloud) instances never auto-detect: they return ``""`` when no
        model is configured so :meth:`chat_json` can report it.
        """
        global _cached_chat_model

        if explicit:
            return explicit
        if self.model:
            return self.model
        if self.remote:
            return ""
        if cfg.chat_model:
            return cfg.chat_model
        if _cached_chat_model:
            return _cached_chat_model

        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
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
        if not chosen:
            return {}, f"No chat model configured for provider '{self.label}'"
        if self.remote and not self.api_key:
            return {}, f"No API key configured for provider '{self.label}'"

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
                f"{self.base_url}/api/chat",
                json=payload,
                headers=self._headers(),
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body.get("message", {}).get("content", "")
            if not content.strip():
                return {}, f"Empty response from model {chosen}"
            return parse_llm_json(content), None
        except Exception as exc:
            log.warning("%s chat failed (model=%s): %s", self.label, chosen, exc)
            return {}, str(exc)


class OllamaVisionProvider(_OllamaEndpoint):
    """Vision completions via Ollama (image passed in the ``images`` array)."""

    def complete(
        self,
        prompt: str,
        image_b64: str,
        *,
        model: str | None = None,
        timeout: float = 120,
    ) -> str:
        # A model baked in at construction (cloud) wins over the plumbed-in
        # default, which carries a local Ollama model name. The local provider
        # leaves ``self.model`` empty, so per-call override → cfg as before.
        chosen = self.model or model or cfg.vision_model
        if not chosen:
            raise ValueError(f"No vision model configured for provider '{self.label}'")
        if self.remote and not self.api_key:
            raise ValueError(f"No API key configured for provider '{self.label}'")

        resp = httpx.post(
            f"{self.base_url}/api/chat",
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
            headers=self._headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
