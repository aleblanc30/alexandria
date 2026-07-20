"""Tests for the unified ``alexandria`` CLI dispatcher."""
import importlib

import pytest

from pka import cli


def test_help_exits_zero(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "usage: alexandria" in out
    for command in cli.COMMANDS:
        assert command in out


def test_unknown_command_exits_nonzero(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_all_commands_resolve_to_modules_with_main():
    for command, (module_name, _) in cli.COMMANDS.items():
        module = importlib.import_module(f"pka.cli.{module_name}")
        assert callable(module.main), f"{command} has no main()"


@pytest.mark.parametrize("command", ["zotero", "firefox", "calibre", "clustering"])
def test_dispatch_passes_remaining_args(monkeypatch, command):
    module_name = cli.COMMANDS[command][0]
    module = importlib.import_module(f"pka.cli.{module_name}")
    seen: dict = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(module, "main", fake_main)
    assert cli.main([command, "--dry-run"]) == 0
    assert seen["argv"] == ["--dry-run"]


def test_init_runs_init_db(monkeypatch, tmp_path):
    called = {"n": 0}
    init_db_module = importlib.import_module("pka.cli.init_db")
    monkeypatch.setattr(init_db_module, "init_db", lambda: called.__setitem__("n", called["n"] + 1))
    assert cli.main(["init"]) == 0
    assert called["n"] == 1
