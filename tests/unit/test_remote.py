"""Tests for nexus_deploy._remote — Phase 1 SSH/rsync primitives (#505)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_deploy import _remote


def test_ssh_run_invokes_ssh_with_host_and_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ssh_run('cmd')`` calls ``ssh nexus 'cmd'`` via subprocess."""
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.subprocess.run", fake_run)
    result = _remote.ssh_run("echo hello")
    assert result.returncode == 0
    assert captured["args"][0] == ["ssh", "nexus", "echo hello"]
    assert captured["kwargs"]["check"] is True
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_ssh_run_custom_host(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.subprocess.run", fake_run)
    _remote.ssh_run("uptime", host="dev-host")
    assert captured["args"][0] == ["ssh", "dev-host", "uptime"]


def test_ssh_run_no_check_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """``check=False`` is forwarded so non-zero exits don't raise."""
    captured: dict[str, Any] = {}

    def fake_run(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["check"] = kwargs.get("check")
        return subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("nexus_deploy._remote.subprocess.run", fake_run)
    result = _remote.ssh_run("false", check=False)
    assert captured["check"] is False
    assert result.returncode == 1


def test_rsync_to_remote_appends_trailing_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path('/foo')`` becomes ``/foo/`` to match deploy.sh's contents-only rsync."""
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.subprocess.run", fake_run)
    _remote.rsync_to_remote(tmp_path, "nexus:/dst/")
    cmd = captured["args"]
    assert cmd[0] == "rsync"
    assert "-aq" in cmd
    # Source has trailing slash → rsync uploads dir contents, not the dir itself
    assert cmd[-2] == f"{tmp_path}/"
    assert cmd[-1] == "nexus:/dst/"


def test_rsync_to_remote_preserves_existing_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing a string path that already ends in ``/`` is left alone."""
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.subprocess.run", fake_run)
    _remote.rsync_to_remote(Path("/some/path/"), "nexus:/dst/")
    # Path('/some/path/') normalises to '/some/path' in str(); we
    # always re-append /, so the result is the same.
    assert captured["args"][-2].endswith("/")


def test_rsync_to_remote_delete_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.subprocess.run", fake_run)
    _remote.rsync_to_remote(Path("/src"), "nexus:/dst/", delete=True)
    assert "--delete" in captured["args"]


def test_rsync_to_remote_no_delete_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.subprocess.run", fake_run)
    _remote.rsync_to_remote(Path("/src"), "nexus:/dst/")
    assert "--delete" not in captured["args"]
