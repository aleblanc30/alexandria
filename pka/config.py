"""
Project-wide settings.

All paths default to sensible per-user locations and can be overridden via the
``PKA_`` env prefix or a ``.env`` file in the working directory.
"""
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Source paths ────────────────────────────────────────────────────────
    zotero_db: Path = Path.home() / "Zotero" / "zotero.sqlite"
    firefox_db: Path = Path.home() / ".mozilla/firefox"   # profile auto-detected
    book_archive: Path = Path.home() / "Documents/books"
    images_dir: Path = Path.home() / "Pictures" / "research"

    # ── Output paths ────────────────────────────────────────────────────────
    data_dir: Path = Path("data")

    @property
    def archive_db(self) -> Path:
        return self.data_dir / "archive.db"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def zotero_db_copy(self) -> Path:
        return self.data_dir / "zotero_copy.sqlite"

    # ── Ollama ──────────────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    chat_model: str = ""   # auto-detect first non-embedding model when empty
    vision_model: str = "llava"

    # ── Chunking ────────────────────────────────────────────────────────────
    chunk_sentences: int = 5      # sentence-window size
    chunk_overlap: int = 1        # sentences of overlap between windows
    min_chunk_chars: int = 80     # discard chunks shorter than this

    # ── Firefox fetch ───────────────────────────────────────────────────────
    fetch_timeout_seconds: float = 10.0       # max seconds to read response body
    fetch_connect_timeout_seconds: float = 5.0  # max seconds to establish connection
    fetch_concurrency: int = 8                # parallel URL fetches

    # ── Images ──────────────────────────────────────────────────────────────
    ocr_lang: str = "eng"                                  # passed to pytesseract
    clip_model: str = "openai/clip-vit-base-patch32"       # HuggingFace hub id

    # ── Validators ──────────────────────────────────────────────────────────
    @field_validator("zotero_db", "firefox_db", "book_archive", "images_dir")
    @classmethod
    def _expand_and_check(cls, v: Path) -> Path:
        """Expand ``~`` and reject obvious system roots."""
        if v is None:
            return v
        v = Path(v).expanduser().resolve()
        forbidden = (Path("/etc"), Path("/usr"), Path("/var"), Path("/sys"))
        for prefix in forbidden:
            try:
                v.relative_to(prefix)
            except ValueError:
                continue
            raise ValueError(f"Refusing to index system path: {v}")
        return v

    class Config:
        env_file = ".env"
        env_prefix = "PKA_"


settings = Settings()
