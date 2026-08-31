"""
Project-wide settings.

All paths default to sensible per-user locations and can be overridden via the
``ALEXANDRIA_`` env prefix or a ``.env`` file in the working directory.

Credentials live apart from ordinary config in a ``.secrets`` file (same
``KEY=value`` format, keys additionally prefixed with ``SECRET_``) so ``.env``
can stay free of API keys and passwords. See ``SecretsFileSettingsSource``.
"""
import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource

log = logging.getLogger(__name__)

FORBIDDEN_PATH_PREFIXES = (Path("/etc"), Path("/usr"), Path("/var"), Path("/sys"))

# ── Secrets file ─────────────────────────────────────────────────────────────
# Default location (relative to cwd) and the key prefix that marks a line in it.
# Override the path with ALEXANDRIA_SECRETS_FILE; set it empty to disable.
SECRETS_FILE = ".secrets"
SECRET_KEY_PREFIX = "SECRET_"


def _secrets_file_path() -> Path | None:
    """Resolve the secrets file location, honouring ALEXANDRIA_SECRETS_FILE."""
    override = os.environ.get("ALEXANDRIA_SECRETS_FILE")
    raw = SECRETS_FILE if override is None else override
    if not raw:
        return None
    return Path(raw).expanduser()


def parse_secrets_file(path: Path) -> dict[str, str]:
    """Parse ``SECRET_ALEXANDRIA_FOO=bar`` lines into ``{"ALEXANDRIA_FOO": "bar"}``.

    Same shape as ``.env``: ``KEY=value`` per line, ``#`` comments, blank lines
    and an optional ``export`` prefix ignored, surrounding quotes stripped. Keys
    without the ``SECRET_`` prefix are ignored so the file can't be used to set
    arbitrary config behind ``.env``'s back.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read secrets file %s: %s", path, exc)
        return out

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            log.warning("Ignoring malformed line %d in %s", lineno, path)
            continue
        key = key.strip()
        if not key.startswith(SECRET_KEY_PREFIX):
            log.warning("Ignoring key without %s prefix on line %d in %s",
                        SECRET_KEY_PREFIX, lineno, path)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key[len(SECRET_KEY_PREFIX):]] = value
    return out


class SecretsFileSettingsSource(PydanticBaseSettingsSource):
    """Settings source backed by the ``.secrets`` file.

    Sits between the process environment and ``.env`` in precedence: a real env
    var still wins, but a secret overrides anything left in ``.env``. Keys are
    matched the same way as env vars — ``SECRET_`` stripped, then the
    ``ALEXANDRIA_`` prefix, then lowercased to a field name.
    """

    def __init__(self, settings_cls: type[BaseSettings]):
        super().__init__(settings_cls)
        self._values = self._load()

    def _load(self) -> dict[str, Any]:
        path = _secrets_file_path()
        if path is None or not path.is_file():
            return {}

        prefix = self.config.get("env_prefix", "")
        fields = self.settings_cls.model_fields
        values: dict[str, Any] = {}
        for key, value in parse_secrets_file(path).items():
            if prefix and not key.upper().startswith(prefix.upper()):
                log.warning("Secret %s%s does not start with %s — ignoring",
                            SECRET_KEY_PREFIX, key, prefix)
                continue
            name = key[len(prefix):].lower()
            if name not in fields:
                log.warning("Secret %s%s does not match any setting — ignoring",
                            SECRET_KEY_PREFIX, key)
                continue
            values[name] = value
        return values

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        if field_name in self._values:
            return self._values[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


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

    # ── YouTube (Data API v3 — a network source; see DESIGN.md §1.1, §2.1) ───
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
    # alongside local EasyOCR + CLIP. See pka/providers/.
    chat_provider: str = "ollama"  # ollama | ollama_cloud | openrouter | ovh | scaleway
    vision_provider: str = "ollama"  # ollama | ollama_cloud | openrouter | ovh | scaleway
    ocr_provider: str = "vlm"  # vlm (vision model transcribes) | easyocr

    # EasyOCR device. Off by default because the pinned torch is a CPU build
    # (pyproject: "torch>=2.2  # CPU build is fine"), so a fresh checkout must
    # not ask for a device it has no kernels for. Turning this on requires a
    # CUDA torch whose arch list actually covers the card — verify with
    # `python -c "import torch; print(torch.cuda.get_arch_list())"`.
    easyocr_gpu: bool = False

    # Longest edge EasyOCR downscales to before detection; 0 keeps EasyOCR's own
    # default (2560). Only matters for images larger than the cap. At 2560 a
    # camera-resolution photo costs ~2.6 GB of VRAM, enough to evict a resident
    # gate VLM from a 4 GB card, so set ~1600 when running `easyocr_gpu` on a
    # small GPU. Left at 0 by default so enabling the GPU never silently changes
    # measured coverage (and therefore gate decisions) for existing libraries.
    easyocr_canvas_size: int = 0
    image_embed_provider: str = "clip"  # clip

    # Master switch for the OCR pass. Set ALEXANDRIA_OCR_ENABLED=0 to skip text
    # extraction entirely (UI + CLI + sync), relying only on the VLM description.
    ocr_enabled: bool = True

    # Master switch for the CLIP image-embedding pass, off by default. CLIP buys
    # exactly one thing: *purely visual* matching, where the query words appear
    # nowhere in the image's inferred text. That is a narrow slice of real
    # queries, and it costs a ~600 MB model download plus an extra pass and a
    # second Chroma collection on every image. The inferred-text path (per-type
    # content + description + OCR, already embedded into the shared chunk
    # collection) answers the rest, so the visual index is opt-in: set
    # ALEXANDRIA_CLIP_ENABLED=1 to index and search it. With it off, both the
    # ingest pass and the text->image query path short-circuit (DESIGN.md §3.3).
    clip_enabled: bool = False

    # ── Image gate (two-step admission filter) ──────────────────────────────
    # Before an image runs the expensive describe/OCR/CLIP passes it must clear
    # two gates: (1) EasyOCR-measured text coverage ≥ the threshold, and (2) a
    # fast VLM classification into a non-"unknown" category of interest. Failing
    # either records the path in the ``image_rejections`` cache and skips it now
    # and on future runs. The gate classifier is deliberately distinct (and
    # smaller/faster, e.g. moondream) from the main describe pass; the label it
    # resolves is reused to pick that pass's per-type content prompt, so the main
    # ``vision_model`` never re-classifies when the gate is on.
    image_gate_enabled: bool = True
    image_gate_text_coverage_min: float = 0.05  # fraction of pixels covered by text
    image_gate_vision_provider: str = "ollama"  # ollama | ollama_cloud | openrouter | ovh | scaleway
    image_gate_vision_model: str = "moondream"  # small local classifier by default

    # ── Ollama (local chat / vision) ────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    chat_model: str = ""  # auto-detect first non-embedding, non-vision model when empty
    vision_model: str = "llava"  # swap via ALEXANDRIA_VISION_MODEL (e.g. "llava-phi3")
    # Model used by the "vlm" OCR provider to transcribe image text. Empty ⇒ reuse
    # ``vision_model`` so classification, description, and OCR share one model.
    vlm_ocr_model: str = ""

    # ── Ollama Cloud (hosted ollama.com — same native API, Bearer key) ──────
    # Set *_PROVIDER=ollama_cloud to route a capability here. Distinct from the
    # local-daemon route (``ollama signin`` + a ``:cloud`` model tag in
    # ``chat_model``), which needs no settings beyond the model name. The API
    # key is a credential — supply it the same way as the other keys below.
    ollama_cloud_base_url: str = "https://ollama.com"
    ollama_cloud_api_key: str = ""  # created at ollama.com/settings/keys
    ollama_cloud_chat_model: str = ""  # e.g. "gpt-oss:120b"
    ollama_cloud_vision_model: str = ""  # must be a vision model, e.g. "qwen3.5:397b"

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

    # ── Scaleway Generative APIs (OpenAI-compatible remote chat / vision) ───
    scaleway_api_key: str = ""
    scaleway_base_url: str = "https://api.scaleway.ai/v1"
    scaleway_chat_model: str = ""
    scaleway_vision_model: str = ""

    # ── Ingestion progress ──────────────────────────────────────────────────
    # The ``/ingestion/status`` and ``/ingestion/sync/progress`` endpoints are
    # polled ~2×/sec by the frontend while a sync runs. Deriving the "pending"
    # and "corpus size" numbers re-probes the live sources each call (Firefox
    # parse, Zotero DB copy, image-folder walk + per-file EXIF), so those probe
    # results are cached for this many seconds and invalidated at job
    # start/finish/purge. Set to 0 to disable caching (always recompute).
    ingestion_probe_cache_ttl_seconds: float = 30.0

    # ── Chunking ────────────────────────────────────────────────────────────
    chunk_sentences: int = 5  # sentence-window size
    chunk_overlap: int = 1  # sentences of overlap between windows
    min_chunk_chars: int = 80  # discard chunks shorter than this

    # ── Firefox fetch ───────────────────────────────────────────────────────
    fetch_timeout_seconds: float = 10.0  # max seconds to read response body
    fetch_connect_timeout_seconds: float = 5.0  # max seconds to establish connection
    fetch_concurrency: int = 8  # parallel URL fetches
    # Cap PDF extraction depth (None = all pages). Extraction is the slowest
    # step in a fetch run, and a bookmarked PDF book costs minutes at full
    # depth. Also applies to arXiv/bioRxiv PDFs, whose body text is appended
    # to title + abstract in build_preprint_text.
    fetch_pdf_max_pages: int | None = 3
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
    # Private token-bearing feed from https://www.reddit.com/prefs/feeds/ (RSS or
    # JSON form accepted; always fetched as .rss). This is the only credential
    # the connector takes — the OAuth API needs a "script" app clearance that
    # personal accounts no longer get. The URL *is* the credential — anyone
    # holding it can read the saved list — so it belongs in .secrets as
    # SECRET_ALEXANDRIA_REDDIT_FEED_URL, never in .env, and is redacted in logs.
    reddit_feed_url: str = ""
    # Plain and descriptive on purpose. The "<platform>:<app id>:<version>"
    # form Reddit's API rules ask for applies to OAuth API clients; the private
    # feed is not that, and announcing "python:" there advertises a script to a
    # WAF without buying anything. The loader still appends "(by /u/<user>)"
    # from the feed URL when this carries no attribution.
    reddit_user_agent: str = "Alexandria/0.2 (local research library; saved-post indexer)"
    reddit_saved_limit: int | None = None   # None = fetch all saved items
    # Pause between feed pages (seconds), plus up to this much random jitter.
    # Only a backfill makes many requests in a row; an incremental sync usually
    # stops after one page and never sleeps.
    reddit_feed_poll_interval_seconds: float = 1.0
    reddit_feed_poll_jitter_seconds: float = 0.5
    # A failed feed response is always written to data_dir/diagnostics (a block
    # page explains itself in HTML that does not fit in a log line). Set this to
    # open that file in a browser tab as well — handy while diagnosing, noisy
    # during a normal run, hence off.
    reddit_feed_open_failed_page: bool = False
    # Write every poll to data_dir/reddit/<timestamp>/ (raw Atom pages plus a
    # manifest) and merge its items into data_dir/reddit/saved.jsonl. On by
    # default, unlike the outbound flags: this is a local disk write, and Reddit
    # is the one source with no local original to re-read — the feed serves only
    # the newest slice, so an unarchived poll is gone once the token dies.
    reddit_archive_enabled: bool = True

    # ── Images ──────────────────────────────────────────────────────────────
    ocr_lang: str = "eng"  # OCR language(s), e.g. "eng+fra"; mapped to EasyOCR codes
    clip_model: str = "openai/clip-vit-base-patch32"  # HuggingFace hub id

    # ── Retrieval enrichment (DESIGN.md §3.2) ───────────────────────────────
    # What each document type contributes to the vector index. The per-type VLM
    # prompts (transcript for slide/notes/whiteboard, content summary for
    # poster, structured {title, authors, isbn} extraction for book_cover) are
    # always on and purely local, so they carry no flag. Everything below either
    # reaches the network or spends chat tokens, so it is off by default per §1.1.
    bookmark_summary_enabled: bool = False  # local LLM summary chunk for bookmarks/posts
    # Separate from the bookmark flag on purpose: a book is map-reduced over its
    # full text (many calls), a bookmark is usually one. Enabling the cheap case
    # must not silently enable the expensive one.
    book_summary_enabled: bool = False  # local LLM summary chunk for Calibre full text
    external_lookup_enabled: bool = False  # Open Library by ISBN or title+author
    cover_search_fallback: bool = False  # web search when the lookup misses
    search_provider: str = "google_books"  # see pka/ingestion/book_search.py registry
    # One credential per backend, never a shared slot: the backends are separate
    # vendors, and a single setting meant whichever key was configured got sent
    # to whichever backend ran first — a Brave key ending up in a googleapis.com
    # query string, where Google rejects it with a 400 and the rung silently
    # stops working.
    search_api_key: str = ""  # Brave credential — SECRET_ALEXANDRIA_SEARCH_API_KEY
    # Optional even when the Google Books rung is on: keyless use only costs a
    # lower per-IP quota, which is what keeps it the switch-on-and-try default.
    google_books_api_key: str = ""  # credential — SECRET_ALEXANDRIA_GOOGLE_BOOKS_API_KEY
    openlibrary_base_url: str = "https://openlibrary.org"
    summary_max_sentences: int = 4  # MiniLM truncates in the low hundreds of word-pieces

    @property
    def cover_search_active(self) -> bool:
        """Web search runs only when the identifier lookup it falls back from is on.

        §1.1 forbids implicit escalation: enabling one outbound path must not
        enable another. Call sites use this rather than ``cover_search_fallback``
        so the flag genuinely has no effect on its own.
        """
        return self.cover_search_fallback and self.external_lookup_enabled

    # ── Clustering ──────────────────────────────────────────────────────────
    cluster_space: str = "pca"  # pca | legacy_umap
    cluster_pca_components: int = 50
    cluster_label_workers: int = 4
    cluster_async_labelling: bool = False  # TF-IDF first, LLM relabel in background
    cluster_regenerate_temperature: float = 0.85  # higher on manual Regenerate label (Ollama)

    # ── Tag training ───────────────────────────────────────────────────────
    tag_training_llm_chat_timeout_seconds: float = 60.0  # per doc in LLM pseudo-label

    # ── Validators ──────────────────────────────────────────────────────────
    @field_validator(
        "dev",
        "fetch_wayback_fallback",
        "cluster_async_labelling",
        "ocr_enabled",
        "clip_enabled",
        "reddit_feed_open_failed_page",
        "reddit_archive_enabled",
        "image_gate_enabled",
        "bookmark_summary_enabled",
        "book_summary_enabled",
        "external_lookup_enabled",
        "cover_search_fallback",
        mode="before",
    )
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

    # ── Sources ─────────────────────────────────────────────────────────────
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert ``.secrets`` between the environment and ``.env``."""
        return (
            init_settings,
            env_settings,
            SecretsFileSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    class Config:
        env_file = ".env"
        env_prefix = "ALEXANDRIA_"


settings = Settings()
