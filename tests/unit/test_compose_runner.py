"""Tests for nexus_deploy.compose_runner — Phase 2 Modul 2.2a (#505).

Eight round-tagged invariant tests (one per deploy.sh hardening round)
plus virtual-service expansion tests, exec'd-bash regression tests for
the parallel-deploy semantics, and CLI integration covering rc=0/1/2.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from nexus_deploy.compose_runner import (
    ComposeUpResult,
    expand_targets,
    parse_result,
    render_remote_script,
    run_compose_up,
)

# ---------------------------------------------------------------------------
# expand_targets — virtual-service resolution
# ---------------------------------------------------------------------------


def test_expand_targets_no_virtuals() -> None:
    """All-leaf input: parents empty, leaves preserve order."""
    parents, leaves = expand_targets(["jupyter", "marimo", "gitea"])
    assert parents == []
    assert leaves == ["jupyter", "marimo", "gitea"]


def test_round_5_parent_dedupe_two_virtual_children() -> None:
    """R5 — parent stack appears once even with two virtual children enabled."""
    parents, leaves = expand_targets(["seaweedfs-filer", "seaweedfs-manager"])
    assert parents == ["seaweedfs"]
    assert leaves == []


def test_round_5_parent_skipped_in_leaves_when_already_added() -> None:
    """R5 — explicit parent + child means parent is started once, not twice."""
    parents, leaves = expand_targets(["seaweedfs", "seaweedfs-filer"])
    assert parents == ["seaweedfs"]
    # Explicit "seaweedfs" must not appear in leaves
    assert "seaweedfs" not in leaves
    assert leaves == []


def test_round_6_deferred_services_skipped() -> None:
    """R6 — woodpecker is deferred (started later in deploy.sh)."""
    parents, leaves = expand_targets(["jupyter", "woodpecker", "gitea"])
    assert parents == []
    assert "woodpecker" not in leaves
    assert leaves == ["jupyter", "gitea"]


def test_expand_targets_preserves_source_order() -> None:
    """Source order is preserved (operators rely on it for log debug)."""
    parents, leaves = expand_targets(["c", "a", "seaweedfs-filer", "b", "seaweedfs-manager", "z"])
    assert parents == ["seaweedfs"]
    assert leaves == ["c", "a", "b", "z"]


def test_expand_targets_handles_duplicates_in_input() -> None:
    """Duplicate enabled entries don't duplicate compose-up calls."""
    _, leaves = expand_targets(["jupyter", "jupyter", "marimo"])
    assert leaves == ["jupyter", "marimo"]


# ---------------------------------------------------------------------------
# render_remote_script — locks the 8 hardening rounds in the rendered bash
# ---------------------------------------------------------------------------


def _render_default(**kwargs: Any) -> str:
    defaults: dict[str, Any] = {
        "parents": [],
        "leaves": ["jupyter", "marimo"],
    }
    defaults.update(kwargs)
    return render_remote_script(**defaults)


def test_round_1_set_euo_pipefail_first_executable_line() -> None:
    """R1 — `set -euo pipefail` is the first command."""
    script = _render_default()
    first_executable = next(
        line for line in script.splitlines() if line and not line.startswith("#")
    )
    assert first_executable == "set -euo pipefail"


def test_round_2_firewall_override_check_present() -> None:
    """R2 — per-stack firewall override applied when present on disk."""
    script = _render_default(leaves=["minio"])
    # The conditional check must appear
    assert "docker-compose.firewall.yml" in script
    # And the compose invocation form must use both files when present
    assert "-f docker-compose.yml -f docker-compose.firewall.yml" in script


def test_round_3_parallel_deploy_via_background_jobs() -> None:
    """R3 — leaf compose-up runs in background; PIDs collected for `wait`."""
    script = _render_default(leaves=["jupyter", "marimo"])
    # Background-jobs marker
    assert "&\n" in script
    # PID collection
    assert "PIDS+=($!)" in script
    # Wait loop
    assert 'wait "$pid"' in script


def test_round_4_docker_ps_verification_via_bash_exec() -> None:
    """R4 — `docker ps` verification logic, executed via bash.

    Modul-2.0 lesson: dispatch logic that LOOKS right but behaves
    wrong (e.g. grep matching substrings instead of exact names) is
    a real risk. We extract the verification snippet and run it
    against four scenarios. This is the test that would have caught
    a substring-match bug in production where 'foo' matched 'foo-bar'.
    """
    cases = [
        # (docker_ps_output, target_name, expected_match)
        ("foo\nbar\nbaz\n", "foo", True),
        ("foo-bar\nfoo-baz\n", "foo", False),  # substring → must NOT match
        ("\n", "foo", False),
        ("foo\n", "FOO", False),  # case-sensitive
    ]
    for ps_output, name, expected in cases:
        snippet = f"""
set -euo pipefail
echo {shlex_quote(ps_output)} | grep -q "^{name}$" && echo match || echo nomatch
"""
        out = subprocess.run(
            ["bash", "-c", snippet], capture_output=True, text=True, check=False
        ).stdout.strip()
        assert (out == "match") is expected, (
            f"ps={ps_output!r} name={name!r} expected={expected} got={out!r}"
        )


def shlex_quote(s: str) -> str:
    """Local helper because we need bash quoting in the test snippet."""
    import shlex as _shlex

    return _shlex.quote(s)


def test_round_7_global_env_sourced_with_set_a() -> None:
    """R7 — global env (image-version pins) sourced via `set -a; source ...`."""
    script = _render_default()
    assert 'if [ -f "$GLOBAL_ENV" ]; then' in script
    assert "set -a" in script
    assert 'source "$GLOBAL_ENV"' in script
    assert "set +a" in script


def test_round_8_result_line_emitted() -> None:
    """R8 — RESULT line at end with started+failed counts."""
    script = _render_default()
    last_lines = [line for line in script.splitlines() if line.strip()][-3:]
    assert any('echo "RESULT started=' in line for line in last_lines)


def test_render_dify_storage_prep_only_when_flagged() -> None:
    """Dify chown block only present when dify_storage_prep=True."""
    without = _render_default(dify_storage_prep=False)
    assert "/mnt/nexus-data/dify/storage" not in without
    assert "chown -R 1001:1001" not in without

    with_dify = _render_default(dify_storage_prep=True)
    assert "/mnt/nexus-data/dify/storage" in with_dify
    assert "chown -R 1001:1001 /mnt/nexus-data/dify/storage" in with_dify


def test_render_handles_empty_lists() -> None:
    """Empty parents + leaves still produces a parseable RESULT (started=0 failed=0)."""
    script = render_remote_script(parents=[], leaves=[])
    # Bash arrays with zero elements don't enter the `for` loop
    assert "PARENTS=()" in script
    assert "LEAVES=()" in script
    assert 'echo "RESULT started=$STARTED failed=$FAILED"' in script


def test_render_special_chars_in_service_name_quoted() -> None:
    """shlex.quote protects against future stack names with special chars.

    Realistically deploy.sh's STACK_PARENTS map is hardcoded to
    safe ASCII names, but defence in depth: a future contributor
    adding 'foo bar' would not break the rendered bash.
    """
    script = render_remote_script(parents=["foo-bar"], leaves=["a;b"])
    assert "'a;b'" in script  # shlex-quoted form


# ---------------------------------------------------------------------------
# parse_result
# ---------------------------------------------------------------------------


def test_parse_result_happy() -> None:
    out = "RESULT started=8 failed=0"
    assert parse_result(out) == ComposeUpResult(started=8, failed=0)


def test_parse_result_with_warnings_above() -> None:
    out = (
        "  ✓ jupyter started and running\n"
        "  ✗ marimo compose up failed (rc=1)\n"
        "RESULT started=1 failed=1"
    )
    assert parse_result(out) == ComposeUpResult(started=1, failed=1)


def test_parse_result_no_match() -> None:
    assert parse_result("garbage output") is None


def test_compose_up_result_is_success() -> None:
    assert ComposeUpResult(started=5, failed=0).is_success is True
    assert ComposeUpResult(started=0, failed=0).is_success is True
    assert ComposeUpResult(started=5, failed=2).is_success is False


# ---------------------------------------------------------------------------
# run_compose_up — orchestration
# ---------------------------------------------------------------------------


def _ok_runner(stdout: str) -> Any:
    def runner(_script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr="")

    return runner


def test_run_compose_up_happy_path() -> None:
    out = "RESULT started=2 failed=0"
    result = run_compose_up(["jupyter", "marimo"], script_runner=_ok_runner(out))
    assert result == ComposeUpResult(started=2, failed=0)


def test_run_compose_up_no_result_attributes_failure_to_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No RESULT line → failed=count of (parents+leaves)."""
    result = run_compose_up(
        ["jupyter", "marimo", "seaweedfs-filer"],
        script_runner=_ok_runner("garbage output"),
    )
    # parents=[seaweedfs], leaves=[jupyter, marimo] → 3 services
    assert result.failed == 3
    assert result.started == 0


def test_run_compose_up_forwards_remote_warnings_to_local_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Modul-1.2 Round-4 lesson: remote ✓/✗ lines reach local stderr."""
    out = (
        "  ✓ jupyter started and running\n"
        "  ✗ marimo compose up failed (rc=1)\n"
        "RESULT started=1 failed=1"
    )
    run_compose_up(["jupyter", "marimo"], script_runner=_ok_runner(out))
    captured = capsys.readouterr()
    assert "jupyter started and running" in captured.err
    assert "marimo compose up failed" in captured.err
    # RESULT line is wire-format, must NOT pollute stderr
    assert "RESULT started=" not in captured.err


def test_run_compose_up_dify_default_when_dify_in_enabled() -> None:
    """dify_storage_prep defaults to True iff 'dify' is in enabled."""
    captured_script: dict[str, str] = {}

    def capture(script: str) -> subprocess.CompletedProcess[str]:
        captured_script["script"] = script
        return subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="RESULT started=1 failed=0", stderr=""
        )

    run_compose_up(["jupyter", "dify"], script_runner=capture)
    assert "/mnt/nexus-data/dify/storage" in captured_script["script"]


def test_run_compose_up_dify_omitted_when_dify_not_in_enabled() -> None:
    captured_script: dict[str, str] = {}

    def capture(script: str) -> subprocess.CompletedProcess[str]:
        captured_script["script"] = script
        return subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="RESULT started=1 failed=0", stderr=""
        )

    run_compose_up(["jupyter"], script_runner=capture)
    assert "/mnt/nexus-data/dify/storage" not in captured_script["script"]


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _run_cli(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-m", "nexus_deploy", "compose", *args],
        capture_output=True,
        text=True,
        env=full_env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_compose_missing_subcommand_returns_2() -> None:
    rc, _, err = _run_cli([])
    assert rc == 2
    assert "only 'up' subcommand" in err


def test_cli_compose_up_missing_enabled_returns_2() -> None:
    rc, _, err = _run_cli(["up"])
    assert rc == 2
    assert "--enabled" in err


def test_cli_compose_up_empty_enabled_returns_zero() -> None:
    """Empty list = nothing to do = success."""
    rc, out, _ = _run_cli(["up", "--enabled", ""])
    assert rc == 0
    assert "nothing to do" in out


def test_cli_compose_up_unknown_arg_returns_2() -> None:
    rc, _, err = _run_cli(["up", "--enabled", "jupyter", "--bogus"])
    assert rc == 2
    assert "unknown arg" in err


def test_cli_compose_up_subcommand_typo_returns_2() -> None:
    """`compose down`, `compose restart`, etc. all rejected."""
    rc, _, err = _run_cli(["down", "--enabled", "x"])
    assert rc == 2
    assert "only 'up'" in err
