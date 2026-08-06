"""Compatibility shim: chat helpers now delegate to the configured provider.

Historically these lived here and posted directly to Ollama. The backend is now
selectable (``ALEXANDRIA_CHAT_PROVIDER``); the concrete logic lives in
``pka/providers/``. These wrappers keep ``from pka.ollama_chat import chat_json``
working for existing call sites (clustering, tag-training).
"""

from __future__ import annotations

from typing import Any

from pka.providers import get_chat_provider


def resolve_chat_model(explicit: str | None = None) -> str:
    """Return the chat model to use (delegates to the active chat provider)."""
    return get_chat_provider().resolve_model(explicit)


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
    return get_chat_provider().chat_json(
        prompt, model=model, timeout=timeout, temperature=temperature
    )
