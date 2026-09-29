"""The post-embedding hook registry, and the wiring that fills it at start-up."""

import logging
import os
import subprocess
import sys

import pytest

from pka import bootstrap, hooks


@pytest.fixture
def registry(monkeypatch):
    """An empty registry for the test, restored afterwards."""
    monkeypatch.setattr(hooks, "_document_embedded", [])


class TestRegistry:
    def test_listeners_run_in_registration_order(self, registry):
        calls: list[tuple[str, int]] = []
        hooks.on_document_embedded(lambda d: calls.append(("first", d)))
        hooks.on_document_embedded(lambda d: calls.append(("second", d)))

        hooks.document_embedded(7)

        assert calls == [("first", 7), ("second", 7)]

    def test_registering_twice_runs_once(self, registry):
        calls: list[int] = []
        hooks.on_document_embedded(calls.append)
        hooks.on_document_embedded(calls.append)

        hooks.document_embedded(3)

        assert calls == [3]

    def test_a_failing_listener_is_logged_and_the_rest_still_run(self, registry, caplog):
        calls: list[int] = []

        def boom(_doc_id: int) -> None:
            raise RuntimeError("listener broke")

        hooks.on_document_embedded(boom)
        hooks.on_document_embedded(calls.append)

        with caplog.at_level(logging.ERROR, logger="pka.hooks"):
            hooks.document_embedded(5)

        assert calls == [5]
        assert "failed for document 5" in caplog.text

    def test_no_listeners_is_a_no_op(self, registry):
        hooks.document_embedded(1)


class TestInstallHooks:
    def test_registers_learned_tag_scoring(self, registry):
        bootstrap.install_hooks()
        assert hooks.document_embedded_listeners() == (bootstrap._apply_learned_tags,)

    def test_is_idempotent(self, registry):
        bootstrap.install_hooks()
        bootstrap.install_hooks()
        assert len(hooks.document_embedded_listeners()) == 1

    def test_the_listener_scores_the_document(self, monkeypatch):
        scored: list[int] = []
        monkeypatch.setattr(
            "pka.tag_training.scoring.apply_learned_tags_for_document", scored.append
        )
        bootstrap._apply_learned_tags(42)
        assert scored == [42]


# Import an entry point in a fresh interpreter and report whether the
# learned-tag listener is registered. A subprocess, because this suite's
# conftest installs the hooks itself and would hide an entry point that forgot.
_PROBE = """
import importlib, sys
importlib.import_module(sys.argv[1])
from pka import bootstrap, hooks
print(bootstrap._apply_learned_tags in hooks.document_embedded_listeners())
"""


@pytest.mark.parametrize(
    "entry_point",
    [
        "pka.api.main",  # the server, and the sync jobs it runs in-process
        "pka.cli",  # `alexandria <command>`
        "pka.cli.firefox",  # what scripts/run_firefox.py imports directly
    ],
)
def test_entry_point_registers_learned_tag_scoring(entry_point, tmp_path):
    env = {
        **os.environ,
        "ALEXANDRIA_DATA_DIR": str(tmp_path / "data"),
        "ALEXANDRIA_SECRETS_FILE": "",
    }
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, entry_point],
        cwd=tmp_path,  # no repo .env
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert out.stdout.strip().splitlines()[-1] == "True", out.stderr
