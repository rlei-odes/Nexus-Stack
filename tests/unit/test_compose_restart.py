"""Tests for nexus_deploy.compose_restart.

Covers:
- Empty input short-circuit (no ssh round-trip, RESULT 0/0)
- Single + multiple service rendered scripts (snapshot)
- RESULT-line parser: success, partial, missing
- run_restart RC paths: happy / partial / no-result-fallback
- DI seam: script_runner accepts a fake CompletedProcess
- Host parameter plumbed through to _remote.ssh_run_script (R2 #2 pattern)
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus_deploy.compose_restart import (
    RestartResult,
    parse_result,
    render_remote_script,
    run_restart,
)


def _fake_completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh", "nexus", "bash", "-s"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


# ---------------------------------------------------------------------------
# render_remote_script
# ---------------------------------------------------------------------------


def test_render_remote_script_empty_short_circuits_to_result_line() -> None:
    """Empty service list produces a one-line script that emits RESULT
    0/0 directly — saves an ssh round-trip when no git-integrated
    services need restart."""
    script = render_remote_script([])
    assert script.strip() == "echo 'RESULT restarted=0 failed=0'"


def test_render_remote_script_single_service(snapshot: Any) -> None:
    """Snapshot: single-service rendered loop. Verifies STACKS_DIR
    constant + RESULT line shape + per-service status echoes."""
    script = render_remote_script(["jupyter"])
    assert script == snapshot


def test_render_remote_script_multi_service(snapshot: Any) -> None:
    """Snapshot: multi-service rendered loop. Verifies de-duped
    iteration + counters."""
    script = render_remote_script(["jupyter", "marimo", "code-server"])
    assert script == snapshot


def test_render_remote_script_includes_missing_dir_check() -> None:
    """A typo in the input list should surface as a counted failure,
    not a silent skip. The rendered script must include the
    'stack directory missing' branch."""
    script = render_remote_script(["nonexistent"])
    assert "stack directory missing on server" in script


def test_render_remote_script_quotes_service_names() -> None:
    """Defensive: service names are placed in single quotes in the
    for-loop list. Prevents a tab character or special shell metachar
    in a contributor-added service from breaking the script."""
    script = render_remote_script(["foo"])
    assert "for SVC in 'foo'" in script


# ---------------------------------------------------------------------------
# parse_result
# ---------------------------------------------------------------------------


def test_parse_result_happy_path() -> None:
    assert parse_result("RESULT restarted=3 failed=0") == RestartResult(restarted=3, failed=0)


def test_parse_result_partial() -> None:
    assert parse_result(
        "  ✓ Restarted jupyter\n  ✗ Restart marimo: failed\nRESULT restarted=1 failed=1\n"
    ) == RestartResult(restarted=1, failed=1)


def test_parse_result_returns_none_when_missing() -> None:
    """Defensive: no RESULT line in stdout → None. Caller falls back
    to RestartResult(0, len(services))."""
    assert parse_result("garbage output\n") is None
    assert parse_result("") is None


def test_parse_result_returns_none_on_malformed_line() -> None:
    """Wire-format strict — an unfamiliar shape (extra fields, missing
    counter) returns None. Mirrors the compose_runner regex's strict
    ^...$ anchoring."""
    assert parse_result("RESULT started=1 failed=0") is None  # wrong key
    assert parse_result("RESULT restarted=foo failed=0") is None  # non-int


# ---------------------------------------------------------------------------
# run_restart — DI + RC paths
# ---------------------------------------------------------------------------


def test_run_restart_empty_short_circuits_no_ssh() -> None:
    """Empty input must not invoke the runner at all (saves a round-trip)."""
    runner = MagicMock()
    result = run_restart([], script_runner=runner)
    assert result == RestartResult(restarted=0, failed=0)
    runner.assert_not_called()


def test_run_restart_happy_path() -> None:
    runner = MagicMock(return_value=_fake_completed("RESULT restarted=2 failed=0\n"))
    result = run_restart(["jupyter", "marimo"], script_runner=runner)
    assert result.restarted == 2
    assert result.failed == 0
    assert result.is_success
    runner.assert_called_once()
    # Script body is the rendered script (verified by snapshot tests).
    script_arg = runner.call_args[0][0]
    assert "RESULT restarted=" in script_arg


def test_run_restart_partial_failure() -> None:
    runner = MagicMock(return_value=_fake_completed("RESULT restarted=1 failed=2\n"))
    result = run_restart(["a", "b", "c"], script_runner=runner)
    assert result == RestartResult(restarted=1, failed=2)
    assert not result.is_success


def test_run_restart_missing_result_falls_back_to_all_failed() -> None:
    """No RESULT line in stdout → defensive count: every requested
    restart counted as failed. Same fall-through as compose_runner."""
    runner = MagicMock(return_value=_fake_completed("ssh broke before the final echo"))
    result = run_restart(["a", "b", "c"], script_runner=runner)
    assert result == RestartResult(restarted=0, failed=3)


def test_run_restart_default_runner_uses_remote_ssh_run_script_with_host() -> None:
    """When script_runner is None, run_restart must call
    _remote.ssh_run_script with the provided host (R2 #2 + R4 #1
    pattern)."""
    fake_completed = _fake_completed("RESULT restarted=1 failed=0\n")
    with patch(
        "nexus_deploy.compose_restart._remote.ssh_run_script",
        return_value=fake_completed,
    ) as mock_run:
        result = run_restart(["jupyter"], host="custom-host")
    assert result.is_success
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["host"] == "custom-host"


def test_run_restart_defaults_host_to_nexus() -> None:
    fake_completed = _fake_completed("RESULT restarted=1 failed=0\n")
    with patch(
        "nexus_deploy.compose_restart._remote.ssh_run_script",
        return_value=fake_completed,
    ) as mock_run:
        run_restart(["jupyter"])
    assert mock_run.call_args.kwargs["host"] == "nexus"


def test_run_restart_forwards_stderr_lines_for_diagnostics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Per-service ✓/✗ lines from the remote script must reach the
    operator via local stderr — same pattern as compose_runner /
    secret_sync."""
    runner = MagicMock(
        return_value=_fake_completed(
            "  ✓ Restarted jupyter\n  ✗ Restart marimo: failed\nRESULT restarted=1 failed=1\n"
        ),
    )
    run_restart(["jupyter", "marimo"], script_runner=runner)
    captured = capsys.readouterr()
    assert "✓ Restarted jupyter" in captured.err
    assert "✗ Restart marimo" in captured.err
    # The RESULT line itself must be filtered out (it's not operator-facing).
    assert "RESULT restarted=" not in captured.err


# ---------------------------------------------------------------------------
# RestartResult — frozen contract
# ---------------------------------------------------------------------------


def test_restart_result_is_success_iff_zero_failures() -> None:
    assert RestartResult(restarted=5, failed=0).is_success is True
    assert RestartResult(restarted=5, failed=1).is_success is False
    assert RestartResult(restarted=0, failed=0).is_success is True
    assert RestartResult(restarted=0, failed=3).is_success is False


def test_restart_result_frozen() -> None:
    from dataclasses import FrozenInstanceError

    result = RestartResult(restarted=1, failed=0)
    with pytest.raises(FrozenInstanceError):
        result.restarted = 99  # type: ignore[misc]
