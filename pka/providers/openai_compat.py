"""OpenAI-compatible chat/vision providers (OpenRouter, OVH AI Endpoints).

Both services expose the standard ``POST {base_url}/chat/completions`` API with
a ``Bearer`` key, so a single implementation covers them; only the base URL,
key, and model names differ (injected at construction from config).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


class OpenAICompatChatProvider:
    """Chat completions against an OpenAI-compatible endpoint (JSON mode)."""

    def __init__(self, *, base_url: str, api_key: str, model: str, label: str = "openai-compat"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.label = label

    def resolve_model(self, explicit: str | None = None) -> str:
        return explicit or self.model

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
        if not self.api_key:
            return {}, f"No API key configured for provider '{self.label}'"

        payload: dict[str, Any] = {
            "model": chosen,
            "messages": [{"role": "user", "content": prompt}],
            # Ask for a JSON object so quotes/newlines in the content don't
            # break parsing — the OpenAI-compatible equivalent of Ollama's
            # ``format: "json"``.
            "response_format": {"type": "json_object"},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=_auth_headers(self.api_key),
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            if not content or not content.strip():
                return {}, f"Empty response from model {chosen}"
            return parse_llm_json(content), None
        except Exception as exc:
            log.warning("%s chat failed (model=%s): %s", self.label, chosen, exc)
            return {}, str(exc)


class OpenAICompatVisionProvider:
    """Vision completions against an OpenAI-compatible endpoint (data-URI image)."""

    def __init__(self, *, base_url: str, api_key: str, model: str, label: str = "openai-compat"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.label = label

    def complete(
        self,
        prompt: str,
        image_b64: str,
        *,
        model: str | None = None,
        timeout: float = 120,
    ) -> str:
        # The configured vision model wins over the plumbed default (which is an
        # Ollama-specific name); fall back to the passed value only when unset.
        chosen = self.model or model
        if not chosen:
            raise ValueError(f"No vision model configured for provider '{self.label}'")
        if not self.api_key:
            raise ValueError(f"No API key configured for provider '{self.label}'")

        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": chosen,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                            },
                        ],
                    }
                ],
                "response_format": {"type": "json_object"},
            },
            headers=_auth_headers(self.api_key),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
