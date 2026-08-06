"""
Project-wide settings.

All paths default to sensible per-user locations and can be overridden via the
``ALEXANDRIA_`` env prefix or a ``.env`` file in the working directory.
"""
import json
import os
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode

FORBIDDEN_PATH_PREFIXES = (Path("/etc"), Path("/usr"), Path("/var"), Path("/sys"))


def _parse_path_list(value: object) -> list[Path]:
    """Normalize a config value into a de-duplicated list of validated dirs.

    Accepts a list/tuple, a JSON array string (``'["/a","/b"]'`` — how the app
    persists it to ``.env``), an OS-path-separated string, or a bare single
    path (legacy ``ALEXANDRIA_IMAGES_DIR``). Each entry is expanded and checked
    against the system-path denylist; order is preserved and duplicates dropped.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            try:
                value = json.loads(s)
            except json.JSONDecodeError:
                value = [s]
        else:
            value = [part for part in s.split(os.pathsep) if part]
    if not isinstance(value, (list, tuple)):
        value = [value]

    out: list[Path] = []
    seen: set[Path] = set()
    for item in value:
        p = reject_system_path(Path(item))
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def reject_system_path(v: Path) -> Path:
    """Expand ``~`` and reject obvious system roots."""
    v = Path(v).expanduser().resolve()
    for prefix in FORBIDDEN_PATH_PREFIXES:
        try:
            v.relative_to(prefix)
        except ValueError:
            continue
        raise ValueError(f"Refusing to index system path: {v}")
    return v


class Settings(BaseSettings):
    # ── Source paths ────────────────────────────────────────────────────────
    zotero_db: Path = Path.home() / "Zotero" / "zotero.sqlite"
    firefox_db: Path = Path.home() / ".mozilla/firefox"  # profile auto-detected
    book_archive: Path = Path.home() / "Documents/books"
    # One or more image folders. Env: ``ALEXANDRIA_IMAGE_DIRS`` as a JSON array
    # (``'["/a","/b"]'``); the legacy singular ``ALEXANDRIA_IMAGES_DIR`` is still
    # honoured as a fallback. ``NoDecode`` defers parsing to ``_parse_image_dirs``
    # so both JSON and a bare path work.
    image_dirs: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path.home() / "Pictures" / "research"],
        validation_alias=AliasChoices("ALEXANDRIA_IMAGE_DIRS", "ALEXANDRIA_IMAGES_DIR"),
    )

    # ── YouTube (Data API v3 — the one sanctioned cloud connector) ───────────
    # OAuth *desktop app* client secret JSON downloaded from Google Cloud Console.
    # The connector is inert until this file exists (or a cached token is present).
    youtube_client_secret: Path = (
        Path.home() / ".config" / "alexandria" / "youtube_client_secret.json"
    )

    # ── Output paths ────────────────────────────────────────────────────────
    data_dir: Path = Path("data")
    dev: bool = (
        False  # ALEXANDRIA_DEV=1 — dev API, Firefox places snapshot, Zotero library snapshot
    )
    # Max docs synced/ingested per source when dev=True. Edit via
    # ALEXANDRIA_DEV_INGESTION_LIMIT_<SOURCE> env vars (e.g. ALEXANDRIA_DEV_INGESTION_LIMIT_ZOTERO).
    dev_ingestion_limit_firefox: int = 10
    dev_ingestion_limit_zotero: int = 10
    dev_ingestion_limit_calibre: int = 10
    dev_ingestion_limit_image: int = 10
    dev_ingestion_limit_youtube: int = 10
    dev_ingestion_limit_reddit: int = 10

    @property
    def archive_db(self) -> Path:
        return self.data_dir / "archive.db"

    @property
    def youtube_token_path(self) -> Path:
        """Cached OAuth refresh token (lives under data/, git-ignored)."""
        return self.data_dir / "youtube_token.json"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def zotero_db_copy(self) -> Path:
        return self.data_dir / "zotero_copy.sqlite"

    @property
    def firefox_places_copy(self) -> Path:
        return self.data_dir / "firefox_places_copy.sqlite"

    # ── Providers (per-capability backend selection) ────────────────────────
    # Each capability picks its own backend, so e.g. OpenRouter chat can run
    # alongside local Tesseract OCR + CLIP. See pka/providers/.
    chat_provider: str = "ollama"  # ollama | openrouter | ovh
    vision_provider: str = "ollama"  # ollama | openrouter | ovh
    ocr_provider: str = "tesseract"  # tesseract
    image_embed_provider: str = "clip"  # clip

    # ── Ollama (local chat / vision) ────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    chat_model: str = ""  # auto-detect first non-embedding model when empty
    vision_model: str = "llava-phi3"  # swap via ALEXANDRIA_VISION_MODEL (e.g. "llava")

    # ── OpenRouter (OpenAI-compatible remote chat / vision) ─────────────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_chat_model: str = ""  # e.g. "openai/gpt-4o-mini"
    openrouter_vision_model: str = ""  # e.g. "openai/gpt-4o-mini"

    # ── OVH AI Endpoints (OpenAI-compatible remote chat / vision) ───────────
    ovh_api_key: str = ""
    ovh_base_url: str = ""  # region endpoint, e.g. https://…/v1
    ovh_chat_model: str = ""
    ovh_vision_model: str = ""

    # ── Chunking ────────────────────────────────────────────────────────────
    chunk_sentences: int = 5  # sentence-window size
    chunk_overlap: int = 1  # sentences of overlap between windows
    min_chunk_chars: int = 80  # discard chunks shorter than this

    # ── Firefox fetch ───────────────────────────────────────────────────────
    fetch_timeout_seconds: float = 10.0  # max seconds to read response body
    fetch_connect_timeout_seconds: float = 5.0  # max seconds to establish connection
    fetch_concurrency: int = 8  # parallel URL fetches
    fetch_pdf_max_pages: int | None = None  # cap PDF pages (None = all)
    fetch_pdf_max_bytes: int = 50_000_000  # reject larger PDF downloads
    fetch_pdf_timeout_seconds: float = 120.0  # read timeout for .pdf bookmark URLs
    fetch_pdf_budget_extra_seconds: float = 30.0  # extraction slack on top of PDF timeouts
    fetch_wayback_fallback: bool = True  # on HTTP 404, query archive.org for a snapshot
    fetch_wayback_extra_budget_seconds: float = 15.0  # extra time for availability + snapshot
    fetch_wikipedia_retry_delay_seconds: float = 2.0  # pause between Wikipedia API retries
    fetch_wikipedia_max_retries: int = 2  # retries after the first Wikipedia API attempt
    fetch_user_agent: str = (
        "Alexandria/0.2 (local research library; +https://www.mediawiki.org/wiki/API:Etiquette)"
    )

    # ── Reddit (saved posts) ────────────────────────────────────────────────
    # OAuth "script" app credentials — set via ALEXANDRIA_REDDIT_* env / .env only.
    # Never commit these. Create an app at https://www.reddit.com/prefs/apps
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_username: str = ""
    reddit_password: str = ""
    reddit_refresh_token: str = ""   # alternative to username/password auth
    reddit_user_agent: str = "Alexandria/0.2 (local research library; saved-post indexer)"
    reddit_saved_limit: int | None = None   # None = fetch all saved items

    # ── Images ──────────────────────────────────────────────────────────────
    ocr_lang: str = "eng"  # passed to pytesseract
    clip_model: str = "openai/clip-vit-base-patch32"  # HuggingFace hub id

    # ── Clustering ──────────────────────────────────────────────────────────
    cluster_space: str = "pca"  # pca | legacy_umap
    cluster_pca_components: int = 50
    cluster_label_workers: int = 4
    cluster_async_labelling: bool = False  # TF-IDF first, LLM relabel in background
    cluster_regenerate_temperature: float = 0.85  # higher on manual Regenerate label (Ollama)

    # ── Tag training ───────────────────────────────────────────────────────
    tag_training_llm_chat_timeout_seconds: float = 60.0  # per doc in LLM pseudo-label

    # ── Validators ──────────────────────────────────────────────────────────
    @field_validator("dev", "fetch_wayback_fallback", "cluster_async_labelling", mode="before")
    @classmethod
    def _parse_bool(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    @field_validator("zotero_db", "firefox_db", "book_archive", "youtube_client_secret")
    @classmethod
    def _expand_and_check(cls, v: Path) -> Path:
        if v is None:
            return v
        return reject_system_path(v)

    @field_validator("image_dirs", mode="before")
    @classmethod
    def _parse_image_dirs(cls, v: object) -> list[Path]:
        return _parse_path_list(v)

    class Config:
        env_file = ".env"
        env_prefix = "ALEXANDRIA_"


settings = Settings()
