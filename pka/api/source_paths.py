"""Per-source path configuration: read/update the folder or database path each
ingestion connector reads from, persist it to ``.env``, and open a native OS
folder/file picker so the user doesn't have to type an absolute path by hand.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pka.config import reject_system_path, settings

log = logging.getLogger(__name__)

PathKind = Literal["file", "dir"]

# Rewritten in place by tests so a run never touches the repo's real .env.
ENV_FILE_PATH = Path(".env")


@dataclass(frozen=True)
class SourcePathSpec:
    field: str   # attribute name on pka.config.Settings
    kind: PathKind
    label: str   # native picker dialog title


SOURCE_PATH_SPECS: dict[str, SourcePathSpec] = {
    "firefox": SourcePathSpec("firefox_db", "dir", "Select Firefox profile folder"),
    "zotero": SourcePathSpec("zotero_db", "file", "Select zotero.sqlite"),
    "calibre": SourcePathSpec("book_archive", "dir", "Select Calibre library folder"),
    "image": SourcePathSpec("images_dir", "dir", "Select image folder"),
    # YouTube has no data folder; the configurable "path" is its OAuth client
    # secret JSON (used to authorise read-only Data API access).
    "youtube": SourcePathSpec(
        "youtube_client_secret", "file", "Select YouTube OAuth client secret (JSON)"
    ),
}


def require_spec(source: str) -> SourcePathSpec:
    spec = SOURCE_PATH_SPECS.get(source)
    if spec is None:
        raise ValueError(f"Unknown source: {source}")
    return spec


def get_source_path(source: str) -> dict:
    spec = require_spec(source)
    path: Path = getattr(settings, spec.field)
    exists = path.is_dir() if spec.kind == "dir" else path.is_file()
    return {"source": source, "path": str(path), "kind": spec.kind, "exists": exists}


def _persist_env_var(key: str, value: str) -> None:
    """Rewrite (or append) ``KEY='value'`` in ``.env``, preserving every other line."""
    lines = (
        ENV_FILE_PATH.read_text(encoding="utf-8").splitlines()
        if ENV_FILE_PATH.exists() else []
    )
    new_line = f"{key}='{value}'"
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    ENV_FILE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_source_path(source: str, new_path: str) -> dict:
    spec = require_spec(source)
    path = reject_system_path(Path(new_path))
    setattr(settings, spec.field, path)
    _persist_env_var(f"ALEXANDRIA_{spec.field.upper()}", str(path))
    return get_source_path(source)


PICKER_TIMEOUT_SECONDS = 600  # generous — the clock is mostly "user thinking"

# Runs as a standalone script (see open_native_picker) rather than importing
# tkinter in-process. A Tk root created on a FastAPI threadpool worker thread
# competes with the event loop / reload watcher for the GIL and can take tens
# of seconds just to *appear* on Windows; a fresh subprocess gets its own GIL
# and a real main thread, which is what Tk expects, and starts instantly since
# it doesn't carry this process's already-imported ML/ASGI stack.
_PICKER_SCRIPT = """\
import sys
import tkinter as tk
from tkinter import filedialog

kind, title, initial = sys.argv[1], sys.argv[2], sys.argv[3] or None
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
try:
    if kind == "dir":
        chosen = filedialog.askdirectory(title=title, initialdir=initial, parent=root)
    else:
        chosen = filedialog.askopenfilename(title=title, initialdir=initial, parent=root)
finally:
    root.destroy()
print(chosen)
"""


def open_native_picker(source: str) -> str | None:
    """Open a native OS folder/file picker on the machine running the API.

    Alexandria is local-first: the API and the browser tab run on the same
    machine, so a server-side native dialog is the natural way to browse the
    filesystem (a plain ``<input type="file">`` can't return a real absolute
    path). Runs in a standalone subprocess (see ``_PICKER_SCRIPT``) so Tk gets
    a real main thread instead of a threadpool worker. Raises ``RuntimeError``
    when no display/Tk is available or the dialog fails to open — callers
    should fall back to manual path entry in that case.
    """
    spec = require_spec(source)
    current: Path = getattr(settings, spec.field)
    initial_dir = str(current if spec.kind == "dir" else current.parent) if current.exists() else ""

    t0 = time.monotonic()
    log.info("Opening native picker for %s (subprocess launch)…", source)
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PICKER_SCRIPT, spec.kind, spec.label, initial_dir],
            capture_output=True, text=True, timeout=PICKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("Native picker for %s timed out after %.1fs", source, time.monotonic() - t0)
        raise RuntimeError("Native file picker timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"Native file picker unavailable: {exc}") from exc

    log.info("Native picker for %s returned after %.1fs (rc=%s)", source, time.monotonic() - t0, result.returncode)

    if result.returncode != 0:
        reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise RuntimeError(f"Native file picker unavailable ({reason})")

    chosen = result.stdout.strip()
    return chosen or None
