"""Read-only configuration report: what ``pka.config.settings`` currently holds,
grouped for display, with secrets redacted and per-capability provider
resolution for the diagnostic table at the top of ``/settings``.

Pure functions, no FastAPI import — mirrors how ``source_paths.py`` keeps the
logic out of the router.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pka.config import Settings
from pka.config import settings as cfg

# ── Secrets ──────────────────────────────────────────────────────────────────
# The single definition of what must never be serialised. A field matching
# either rule reports presence only (``is_set``), never its value.
SECRET_FIELD_SUFFIXES = ("_api_key",)
SECRET_FIELDS = {"reddit_feed_url"}


def is_secret_field(name: str) -> bool:
    return name in SECRET_FIELDS or name.endswith(SECRET_FIELD_SUFFIXES)


# ── Tiers ────────────────────────────────────────────────────────────────────
# install_time: displayed read-only, forever (never editable from this panel).
# operational: read now, editable in phase 2 (providers/models/URLs, the
#   §1.1 outbound flags, ocr_enabled/clip_enabled/image_gate_enabled).
# tuning: everything else — read-only, grouped and collapsed (DESIGN.md
#   discussion of why the tuning tier gets no form applies here).

INSTALL_TIME_FIELDS = (
    "data_dir",
    "dev",
    "dev_ingestion_limit_firefox",
    "dev_ingestion_limit_zotero",
    "dev_ingestion_limit_calibre",
    "dev_ingestion_limit_image",
    "dev_ingestion_limit_youtube",
    "dev_ingestion_limit_reddit",
    # Source paths are managed by the per-source picker on /ingestion/:source,
    # not by this panel — displayed here read-only, never a second control.
    "zotero_db",
    "firefox_db",
    "book_archive",
    "image_dirs",
    "youtube_client_secret",
)

OPERATIONAL_FIELDS = (
    "chat_provider",
    "vision_provider",
    "ocr_provider",
    "image_embed_provider",
    "image_gate_vision_provider",
    "ollama_base_url",
    "chat_model",
    "vision_model",
    "vlm_ocr_model",
    "image_gate_vision_model",
    "ollama_cloud_base_url",
    "ollama_cloud_api_key",
    "ollama_cloud_chat_model",
    "ollama_cloud_vision_model",
    "openrouter_api_key",
    "openrouter_base_url",
    "openrouter_chat_model",
    "openrouter_vision_model",
    "ovh_api_key",
    "ovh_base_url",
    "ovh_chat_model",
    "ovh_vision_model",
    "scaleway_api_key",
    "scaleway_base_url",
    "scaleway_chat_model",
    "scaleway_vision_model",
    "ocr_enabled",
    "clip_enabled",
    "image_gate_enabled",
    "bookmark_summary_enabled",
    "book_summary_enabled",
    "external_lookup_enabled",
    "cover_search_fallback",
    "fetch_wayback_fallback",
    "search_provider",
    "search_api_key",
    "google_books_api_key",
    "staan_api_key",
    "openlibrary_base_url",
    "reddit_feed_url",
)


def _tier_for(name: str) -> str:
    if name in INSTALL_TIME_FIELDS:
        return "install_time"
    if name in OPERATIONAL_FIELDS:
        return "operational"
    return "tuning"


# ── Groups ───────────────────────────────────────────────────────────────────
# Display order/section layout. A field absent from every group here is a bug —
# tests/test_settings_view.py asserts every Settings field appears exactly once.

GROUPS: dict[str, tuple[str, ...]] = {
    "Providers": (
        "chat_provider",
        "vision_provider",
        "ocr_provider",
        "image_embed_provider",
        "image_gate_vision_provider",
        "ollama_base_url",
        "chat_model",
        "vision_model",
        "vlm_ocr_model",
        "image_gate_vision_model",
        "ollama_cloud_base_url",
        "ollama_cloud_api_key",
        "ollama_cloud_chat_model",
        "ollama_cloud_vision_model",
        "openrouter_api_key",
        "openrouter_base_url",
        "openrouter_chat_model",
        "openrouter_vision_model",
        "ovh_api_key",
        "ovh_base_url",
        "ovh_chat_model",
        "ovh_vision_model",
        "scaleway_api_key",
        "scaleway_base_url",
        "scaleway_chat_model",
        "scaleway_vision_model",
        "easyocr_gpu",
        "easyocr_canvas_size",
    ),
    "Outbound": (
        "ocr_enabled",
        "clip_enabled",
        "image_gate_enabled",
        "bookmark_summary_enabled",
        "book_summary_enabled",
        "external_lookup_enabled",
        "cover_search_fallback",
        "fetch_wayback_fallback",
        "search_provider",
        "search_api_key",
        "google_books_api_key",
        "staan_api_key",
        "openlibrary_base_url",
        "summary_max_sentences",
        "reddit_feed_url",
    ),
    "Images": (
        "image_gate_text_coverage_min",
        "ocr_lang",
        "clip_model",
    ),
    "Fetch": (
        "fetch_timeout_seconds",
        "fetch_connect_timeout_seconds",
        "fetch_concurrency",
        "fetch_pdf_max_pages",
        "fetch_pdf_max_bytes",
        "fetch_pdf_timeout_seconds",
        "fetch_pdf_budget_extra_seconds",
        "search_url_cards",
        "fetch_wayback_extra_budget_seconds",
        "fetch_wikipedia_retry_delay_seconds",
        "fetch_wikipedia_max_retries",
        "fetch_user_agent",
        "reddit_user_agent",
        "reddit_saved_limit",
        "reddit_feed_poll_interval_seconds",
        "reddit_feed_poll_jitter_seconds",
        "reddit_feed_open_failed_page",
        "reddit_archive_enabled",
        "ingestion_probe_cache_ttl_seconds",
    ),
    "Chunking": (
        "chunk_sentences",
        "chunk_overlap",
        "min_chunk_chars",
    ),
    "Clustering": (
        "cluster_space",
        "cluster_pca_components",
        "cluster_label_workers",
        "cluster_async_labelling",
        "cluster_regenerate_temperature",
        "tag_training_llm_chat_timeout_seconds",
    ),
    "Storage": (
        "data_dir",
        "zotero_db",
        "firefox_db",
        "book_archive",
        "image_dirs",
        "youtube_client_secret",
    ),
    "Dev": (
        "dev",
        "dev_ingestion_limit_firefox",
        "dev_ingestion_limit_zotero",
        "dev_ingestion_limit_calibre",
        "dev_ingestion_limit_image",
        "dev_ingestion_limit_youtube",
        "dev_ingestion_limit_reddit",
    ),
}


def _serialize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


def _build_field(name: str) -> dict:
    field_info = Settings.model_fields[name]
    default = field_info.get_default(call_default_factory=True)
    current = getattr(cfg, name)
    secret = is_secret_field(name)
    return {
        "name": name,
        "value": None if secret else _serialize(current),
        "is_secret": secret,
        "is_set": bool(current) if secret else None,
        "is_default": current == default,
        "tier": _tier_for(name),
    }


def build_settings_report() -> dict:
    """Return every ``Settings`` field, grouped for display, plus capability status."""
    groups = []
    seen: set[str] = set()
    for group_name, field_names in GROUPS.items():
        groups.append({"name": group_name, "fields": [_build_field(name) for name in field_names]})
        seen.update(field_names)

    missing = set(Settings.model_fields) - seen
    if missing:
        raise RuntimeError(f"Settings fields missing from GROUPS: {sorted(missing)}")

    return {"groups": groups, "capabilities": build_capability_report()}


# ── Capability resolution ────────────────────────────────────────────────────
# Mirrors _build_* in pka/providers/__init__.py, but never calls the get_*_provider()
# accessors — those populate the module cache as a side effect and would pin a
# provider built only for a page view.

CAPABILITIES = ("chat", "vision", "gate_vision", "ocr", "image_embed")

_REMOTE_PROVIDERS = ("ollama_cloud", "openrouter", "ovh", "scaleway")


def _base_url_for(provider_name: str) -> str:
    return {
        "ollama": cfg.ollama_base_url,
        "ollama_cloud": cfg.ollama_cloud_base_url,
        "openrouter": cfg.openrouter_base_url,
        "ovh": cfg.ovh_base_url,
        "scaleway": cfg.scaleway_base_url,
    }.get(provider_name, "")


def _api_key_for(provider_name: str) -> str:
    return {
        "ollama_cloud": cfg.ollama_cloud_api_key,
        "openrouter": cfg.openrouter_api_key,
        "ovh": cfg.ovh_api_key,
        "scaleway": cfg.scaleway_api_key,
    }.get(provider_name, "")


def _credential_present(provider_name: str) -> bool:
    if provider_name in _REMOTE_PROVIDERS:
        return bool(_api_key_for(provider_name))
    return True  # local ollama needs no credential


def _vision_model_for(provider_name: str) -> str:
    return {
        "ollama_cloud": cfg.ollama_cloud_vision_model,
        "openrouter": cfg.openrouter_vision_model,
        "ovh": cfg.ovh_vision_model,
        "scaleway": cfg.scaleway_vision_model,
    }.get(provider_name, cfg.vision_model)


def _resolve_chat_model() -> str:
    # Reuses OllamaChatProvider.resolve_model, which already probes
    # GET {base_url}/api/tags for the local backend when chat_model is unset —
    # this is the "local Ollama is probed on mount" case DESIGN.md §1.1 allows,
    # since ollama_base_url defaults to localhost.
    from pka.providers import _build_chat

    return _build_chat(cfg.chat_provider).resolve_model()


def _capability_status(capability: str, provider_name: str, model: str) -> dict:
    return {
        "capability": capability,
        "provider": provider_name,
        "model": model,
        "base_url": _base_url_for(provider_name),
        "credential_present": _credential_present(provider_name),
    }


def _ocr_capability_status() -> dict:
    if cfg.ocr_provider == "easyocr":
        return {
            "capability": "ocr",
            "provider": "easyocr",
            "model": "",
            "base_url": "",
            "credential_present": True,
        }
    # "vlm" OCR reuses the vision backend to transcribe image text.
    model = cfg.vlm_ocr_model or _vision_model_for(cfg.vision_provider)
    return _capability_status("ocr", cfg.vision_provider, model)


def build_capability_report() -> list[dict]:
    return [
        _capability_status("chat", cfg.chat_provider, _resolve_chat_model()),
        _capability_status("vision", cfg.vision_provider, _vision_model_for(cfg.vision_provider)),
        _capability_status(
            "gate_vision", cfg.image_gate_vision_provider, cfg.image_gate_vision_model
        ),
        _ocr_capability_status(),
        _capability_status(
            "image_embed",
            cfg.image_embed_provider,
            cfg.clip_model if cfg.image_embed_provider == "clip" else "",
        ),
    ]


# ── Reachability probe (on demand only — never called from build_*_report) ──

_PROBE_TIMEOUT_SECONDS = 5


def _provider_for_capability(capability: str) -> str | None:
    if capability == "chat":
        return cfg.chat_provider
    if capability == "vision":
        return cfg.vision_provider
    if capability == "gate_vision":
        return cfg.image_gate_vision_provider
    if capability == "ocr":
        return cfg.vision_provider if cfg.ocr_provider == "vlm" else None
    if capability == "image_embed":
        return None
    raise ValueError(f"Unknown capability: {capability!r}")


def probe_provider(capability: str) -> dict:
    """Check whether the backend behind ``capability`` is reachable right now.

    Never raises — an unreachable backend is the answer, not an error, so any
    exception from the HTTP call is caught and returned as ``detail``.
    """
    import httpx

    provider_name = _provider_for_capability(capability)
    if provider_name is None:
        return {"reachable": True, "detail": "Local, no network required"}

    base_url = _base_url_for(provider_name)
    if not base_url:
        return {"reachable": False, "detail": "No base URL configured"}

    try:
        if provider_name in ("ollama", "ollama_cloud"):
            headers = (
                {"Authorization": f"Bearer {cfg.ollama_cloud_api_key}"}
                if provider_name == "ollama_cloud" and cfg.ollama_cloud_api_key
                else {}
            )
            resp = httpx.get(
                f"{base_url.rstrip('/')}/api/tags", headers=headers, timeout=_PROBE_TIMEOUT_SECONDS
            )
        else:
            headers = {"Authorization": f"Bearer {_api_key_for(provider_name)}"}
            resp = httpx.get(
                f"{base_url.rstrip('/')}/models", headers=headers, timeout=_PROBE_TIMEOUT_SECONDS
            )
        resp.raise_for_status()
        return {"reachable": True, "detail": "ok"}
    except Exception as exc:
        return {"reachable": False, "detail": str(exc)}
