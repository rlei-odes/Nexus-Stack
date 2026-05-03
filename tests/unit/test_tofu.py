"""Tests for nexus_deploy.tofu — Phase 3 Modul 3.2 (#505)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_deploy.tofu import TofuError, TofuRunner

# -- output_raw ---------------------------------------------------------


def test_output_raw_invokes_tofu_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = args[0]
        captured["cwd"] = kwargs.get("cwd")
        captured["check"] = kwargs.get("check")
        captured["capture_output"] = kwargs.get("capture_output")
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="1.2.3.4", stderr="")

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    runner = TofuRunner(Path("/some/tofu/dir"))
    result = runner.output_raw("server_ip")

    assert result == "1.2.3.4"
    assert captured["argv"] == ["tofu", "output", "-raw", "server_ip"]
    assert captured["cwd"] == Path("/some/tofu/dir")
    assert captured["check"] is True
    assert captured["capture_output"] is True


def test_output_raw_default_tofu_dir_is_stack() -> None:
    """No-arg constructor uses tofu/stack — matches deploy.sh's $TOFU_DIR."""
    runner = TofuRunner()
    assert runner.tofu_dir == Path("tofu/stack")


def test_output_raw_strips_trailing_newlines_to_match_dollar_paren(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tofu output -raw`` adds a trailing ``\\n``; deploy.sh's ``$(...)``
    command-substitution strips it. The Python wrapper must do the same
    or downstream f-strings get a stray ``\\n`` in the middle of URLs etc.
    POSIX ``$(...)`` strips ALL trailing newlines, not just one — match
    that with ``rstrip('\\n')``.
    """

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="1.2.3.4\n\n",  # tofu adds one + extra possible
            stderr="",
        )

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_raw("server_ip")
    assert result == "1.2.3.4"


def test_output_raw_preserves_internal_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newlines INSIDE the value (e.g. multi-line PEM) must NOT be stripped —
    only trailing ones. Defends against an over-eager rstrip()."""

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="line1\nline2\nline3\n",  # 2 internal + 1 trailing
            stderr="",
        )

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_raw("multiline_value")
    assert result == "line1\nline2\nline3"


def test_output_raw_returns_default_on_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0], stderr="no output X")

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_raw("missing_output", default="0")
    assert result == "0"


def test_output_raw_returns_default_on_tofu_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "tofu")

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_raw("anything", default="")
    assert result == ""


def test_output_raw_raises_when_no_default_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without default → TofuError. Distinguishes silent-fallback vs strict."""

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    with pytest.raises(TofuError, match="output -raw server_ip"):
        TofuRunner(Path("/some/dir")).output_raw("server_ip")


def test_output_raw_error_message_does_not_leak_subprocess_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TofuError text contains only name + tofu_dir, never the stderr output.

    `tofu` errors can include sensitive provider state (Cloudflare API
    tokens shown in plan diff failures, Hetzner cloud credentials in
    auth-error messages). The exception we raise on top must NOT
    re-emit subprocess.stderr, so we explicitly pin the format.
    """

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Simulate stderr containing a credential-looking token
        raise subprocess.CalledProcessError(
            returncode=1, cmd=args[0], stderr="provider token=eyJhb-secret-do-not-leak"
        )

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    with pytest.raises(TofuError) as excinfo:
        TofuRunner(Path("/dir")).output_raw("server_ip")
    assert "secret-do-not-leak" not in str(excinfo.value)


# -- output_json --------------------------------------------------------


def test_output_json_invokes_tofu_with_correct_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout='{"a": 1}', stderr="")

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    runner = TofuRunner(Path("/dir"))
    result = runner.output_json("secrets")

    assert result == {"a": 1}
    assert captured["argv"] == ["tofu", "output", "-json", "secrets"]


def test_output_json_parses_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tofu list outputs (e.g. enabled_services) parse to Python lists."""

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout='["jupyter", "marimo"]', stderr=""
        )

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_json("enabled_services")
    assert result == ["jupyter", "marimo"]


def test_output_json_returns_default_on_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_json("missing_output", default={})
    assert result == {}


def test_output_json_returns_default_on_tofu_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "tofu")

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_json("anything", default=None)
    assert result is None


def test_output_json_returns_default_on_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tofu succeeded but stdout isn't JSON → default kicks in if provided."""

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="not json at all", stderr=""
        )

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_json("anything", default={"fallback": True})
    assert result == {"fallback": True}


def test_output_json_raises_on_invalid_json_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    with pytest.raises(TofuError, match="returned non-JSON"):
        TofuRunner().output_json("enabled_services")


def test_output_json_raises_when_no_default_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    with pytest.raises(TofuError, match="output -json enabled_services"):
        TofuRunner(Path("/dir")).output_json("enabled_services")


def test_output_json_default_none_is_treated_as_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller passing default=None should NOT trigger raise — None is a valid default."""

    def fake_run(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_json("anything", default=None)
    # Distinguishes the _MISSING sentinel from None
    assert result is None


def test_output_json_default_empty_string_is_treated_as_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-string default is a valid silent-fallback (matches deploy.sh)."""

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "tofu")

    monkeypatch.setattr("nexus_deploy.tofu.subprocess.run", fake_run)
    result = TofuRunner().output_raw("anything", default="")
    assert result == ""


# -- end-to-end against a real tofu-stand-in ----------------------------


def test_output_json_actually_invokes_subprocess(tmp_path: Path) -> None:
    """End-to-end against a fake `tofu` on PATH — catches subprocess.run misuse.

    Mirrors test_remote.test_ssh_run_actually_invokes_subprocess: mocked
    tests prove call-shape but don't catch combinations that raise
    ValueError before subprocess is spawned.
    """
    fake_tofu = tmp_path / "tofu"
    payload = json.dumps({"server_ip": "1.2.3.4"})
    fake_tofu.write_text(f"#!/usr/bin/env bash\nprintf %s {payload!r}\n")
    fake_tofu.chmod(0o755)

    import os

    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp_path}:{old_path}"
    try:
        result = TofuRunner(tmp_path).output_json("server_ip")
    finally:
        os.environ["PATH"] = old_path
    assert result == {"server_ip": "1.2.3.4"}
