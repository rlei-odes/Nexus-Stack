"""Phase 0 smoke tests — proves the package imports and CI runs.

Replaced in Phase 1 with real module tests. Until then these
exist to keep the test runner non-empty and confirm the toolchain
(ruff + mypy + pytest + coverage gate) is wired up.
"""

from __future__ import annotations

import sys

import pytest

import nexus_deploy
from nexus_deploy import __main__, cli, hello


def test_hello_returns_phase_marker() -> None:
    """Smoke: package is importable, hello() returns a stable string."""
    assert hello() == "nexus_deploy phase-0 ready"


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
    assert "phase-0 ready" in captured.out


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
    """Unknown subcommand returns exit-code 2 (Phase 0 = no commands yet)."""
    monkeypatch.setattr(sys, "argv", ["nexus_deploy", "bootstrap"])
    rc = __main__.main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "no commands implemented yet" in captured.err


def test_cli_main_delegates_to_main_module(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cli.main()` is the console-script entry; it delegates to `__main__.main`."""
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "hello"])
    rc = cli.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "phase-0 ready" in captured.out
