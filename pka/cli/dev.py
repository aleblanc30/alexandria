"""Run the backend and frontend dev servers together for local development.

Usage::

    alexandria dev
    alexandria dev --no-open

Starts ``uvicorn --reload`` (port 8420) and the Vite dev server (port 5173)
as subprocesses, streams both logs into the current terminal, opens the
browser at http://localhost:5173 once the frontend is ready, and shuts both
processes down on Ctrl+C.

If either server doesn't report ready within ``STARTUP_TIMEOUT_S``, both are
torn down and the command exits with an error — this is the common symptom
of another process already holding the port: uvicorn's ``--reload``
supervisor stays alive even when its worker fails to bind, so a bare
liveness check on the subprocess isn't enough to catch that.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"
BACKEND_PORT = 8420
FRONTEND_URL = "http://localhost:5173"
STARTUP_TIMEOUT_S = 15.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _stream_output(
    proc: subprocess.Popen,
    prefix: str,
    ready_marker: str,
    ready_event: threading.Event,
) -> None:
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip()
        print(f"[{prefix}] {line}", flush=True)
        # Vite's banner interleaves ANSI color codes inside words (e.g.
        # "\x1b[1mLocal\x1b[22m:"), so match against the color-stripped line.
        if ready_marker in _ANSI_RE.sub("", line) and not ready_event.is_set():
            ready_event.set()


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alexandria dev",
        description="Run backend (uvicorn --reload) + frontend (vite) together.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't open the browser automatically once the frontend is ready",
    )
    args = parser.parse_args(argv)

    # On Windows, shutil.which("npm") can resolve to the extensionless POSIX
    # shim instead of npm.cmd, which CreateProcess can't launch directly.
    npm_candidates = ["npm.cmd", "npm.exe", "npm"] if os.name == "nt" else ["npm"]
    npm = next((shutil.which(name) for name in npm_candidates if shutil.which(name)), None)
    if npm is None:
        print("alexandria dev: 'npm' not found on PATH", file=sys.stderr)
        return 1

    backend_env = {**os.environ, "ALEXANDRIA_DEV": "1"}
    backend: subprocess.Popen | None = None
    frontend: subprocess.Popen | None = None
    threads: list[threading.Thread] = []
    exit_code = 0

    try:
        backend = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "pka.api.main:app",
                "--reload",
                "--reload-dir",
                str(REPO_ROOT / "pka"),
                "--port",
                str(BACKEND_PORT),
            ],
            cwd=REPO_ROOT,
            env=backend_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        frontend = subprocess.Popen(
            [npm, "run", "dev"],
            cwd=FRONTEND_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        backend_ready = threading.Event()
        frontend_ready = threading.Event()

        threads = [
            threading.Thread(
                target=_stream_output,
                args=(backend, "api", "Uvicorn running on", backend_ready),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_output,
                args=(frontend, "web", "Local:", frontend_ready),
                daemon=True,
            ),
        ]
        for t in threads:
            t.start()

        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if backend_ready.is_set() and frontend_ready.is_set():
                break
            if backend.poll() is not None or frontend.poll() is not None:
                break
            time.sleep(0.2)

        # Open the browser only once BOTH servers are ready. The frontend (Vite)
        # reports ready before the backend finishes its init_db() startup; opening
        # on the frontend alone means the first wave of API calls races the backend
        # and the Vite proxy surfaces the unreachable target as 500 toasts.
        if backend_ready.is_set() and frontend_ready.is_set() and not args.no_open:
            webbrowser.open(FRONTEND_URL)

        if not (backend_ready.is_set() and frontend_ready.is_set()):
            which = []
            if not backend_ready.is_set():
                which.append(f"backend (port {BACKEND_PORT})")
            if not frontend_ready.is_set():
                which.append("frontend (port 5173)")
            print(
                f"\nalexandria dev: {' and '.join(which)} did not report ready "
                f"within {STARTUP_TIMEOUT_S:.0f}s — see the log above "
                f"(often another process already using the port). Shutting down.",
                file=sys.stderr,
                flush=True,
            )
            exit_code = 1
        else:
            while backend.poll() is None and frontend.poll() is None:
                try:
                    backend.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    continue
    except KeyboardInterrupt:
        pass
    finally:
        print("\nalexandria dev: shutting down…", flush=True)
        if frontend is not None:
            _kill(frontend)
        if backend is not None:
            _kill(backend)
        for t in threads:
            t.join(timeout=2)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
