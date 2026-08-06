"""Capability interfaces for swappable model backends.

Each interface is a small Protocol describing one capability (chat, vision,
OCR, image embedding). Concrete implementations live alongside in
``pka/providers/``; :mod:`pka.providers` picks one per capability from config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatProvider(Protocol):
    """Text-in, JSON-out chat completion."""

    def resolve_model(self, explicit: str | None = None) -> str:
        """Return the chat model to use (honouring an explicit override)."""
        ...

    def chat_json(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout: float = 90,
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Send *prompt*, parse a JSON object from the reply.

        Returns ``(parsed_dict, error_message)``; *error_message* is set on
        failure and the dict is empty.
        """
        ...


@runtime_checkable
class VisionProvider(Protocol):
    """Image + prompt in, raw text out (parsing is the caller's job)."""

    def complete(
        self,
        prompt: str,
        image_b64: str,
        *,
        model: str | None = None,
        timeout: float = 120,
    ) -> str:
        """Return the model's raw text reply for *prompt* about *image_b64*.

        *image_b64* is a base64-encoded JPEG (no data-URI prefix). Raises on
        transport/HTTP failure so the caller can fall back.
        """
        ...


@runtime_checkable
class OcrProvider(Protocol):
    """Image file in, extracted text out."""

    def ocr(self, path: Path, lang: str = "eng") -> str:
        """Return recognised text, or ``""`` on failure."""
        ...


@runtime_checkable
class ImageEmbedder(Protocol):
    """Cross-modal image/text embeddings (shared vector space)."""

    def embed_image(self, path: Path) -> list[float] | None:
        """Return a normalised image embedding, or ``None`` on failure."""
        ...

    def embed_text(self, query: str) -> list[float] | None:
        """Return a normalised text embedding in the same space, or ``None``."""
        ...
