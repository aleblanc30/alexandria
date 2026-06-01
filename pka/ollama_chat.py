"""Shared Ollama chat helpers — model resolution and JSON chat calls."""
from __future__ import annotations

import logging
from typing import Any

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_EMBED_MARKERS = ("embed", "nomic-embed", "mxbai-embed", "snowflake-arctic-embed")
_cached_chat_model: str | None = None


def _is_chat_model(name: str) -> bool:
    lower = name.lower()
    return not any(marker in lower for marker in _EMBED_MARKERS)


def resolve_chat_model(explicit: str | None = None) -> str:
    """Return the chat model to use, auto-detecting from Ollama when unset."""
    global _cached_chat_model

    if explicit:
        return explicit
    if cfg.chat_model:
        return cfg.chat_model
    if _cached_chat_model:
        return _cached_chat_model

    try:
        import httpx
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
    prompt: str,
    model: str | None = None,
    timeout: float = 90,
    *,
    temperature: float | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Send a chat prompt and parse a JSON object from the reply.

    Returns ``(parsed_dict, error_message)``. *error_message* is set on failure.
    """
    from pka.json_utils import parse_llm_json

    chosen = resolve_chat_model(model)
    payload: dict[str, Any] = {
        "model": chosen,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if temperature is not None:
        payload["options"] = {"temperature": temperature}
    try:
        import httpx
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
