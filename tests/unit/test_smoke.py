"""Smoke tests — proves the package imports and CI runs.

Cheapest signal for "the toolchain is actually wired up" — a
refactor that breaks importability or silently regresses the
no-args / --version CLI shape surfaces here before the heavier
per-module tests run.
"""

from __future__ import annotations

import sys

import pytest

import nexus_deploy
from nexus_deploy import __main__, cli, hello


def test_hello_returns_stable_string() -> None:
    """Smoke: package is importable, hello() returns a stable string."""
    assert hello() == "nexus_deploy ready"


def test_version_present() -> None:
    """Smoke: __version__ is defined at the package root."""
    assert nexus_deploy.__version__
    assert isinstance(nexus_deploy.__version__, str)


def test_main_no_args_prints_hello(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -m nexus_deploy` (no args) prints the hello() target."""
    monkeypatch.setattr(sys, "argv", ["nexus_deploy"])
    rc = __main__.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "nexus_deploy ready" in captured.out


def test_main_version_flag(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -m nexus_deploy --version` prints __version__."""
    monkeypatch.setattr(sys, "argv", ["nexus_deploy", "--version"])
    rc = __main__.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert nexus_deploy.__version__ in captured.out


def test_main_unknown_command_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown subcommand returns exit-code 2."""
    monkeypatch.setattr(sys, "argv", ["nexus_deploy", "bootstrap"])
    rc = __main__.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown command" in captured.err


def test_cli_main_delegates_to_main_module(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cli.main()` is the console-script entry; it delegates to `__main__.main`."""
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "hello"])
    rc = cli.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "nexus_deploy ready" in captured.out
