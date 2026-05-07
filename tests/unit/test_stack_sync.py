"""Tests for nexus_deploy.stack_sync.

Round-tagged invariants on the rendered cleanup bash, exec'd-bash
regression tests for the disabled-stack matching semantics, rsync
dependency-injection tests covering all three RsyncResult statuses,
end-to-end run_stack_sync, and CLI rc=0/1/2 contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from nexus_deploy.stack_sync import (
    CleanupResult,
    RsyncResult,
    StackSyncResult,
    _is_safe_name,
    cleanup_disabled_stacks,
    parse_cleanup_result,
    render_cleanup_script,
    rsync_enabled_stacks,
    run_stack_sync,
)

# ---------------------------------------------------------------------------
# _is_safe_name — path-safety regex (R5 invariant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("jupyter", True),
        ("seaweedfs-filer", True),
        ("foo.bar", True),
        ("FOO_BAR", True),
        ("v1.2.3", True),
        ("", False),
        ("foo bar", False),  # space
        ("foo;bar", False),  # shell meta
        ("foo$bar", False),  # variable expansion attempt
        ("foo`bar`", False),  # command substitution attempt
        ("foo'bar", False),  # quote
        ("foo\nbar", False),  # newline
        ("../bar", False),  # path traversal attempt
        ("foo/bar", False),  # slash
        ("-rf", True),  # leading dash IS allowed; the bash uses `-- "$name"` to terminate options
        # Round-1 PR #523: bare "." and ".." pass the regex (dots are
        # in the char class) but ARE path-traversal segments. Explicit
        # rejection in _is_safe_name covers them.
        (".", False),
        ("..", False),
    ],
)
def test_is_safe_name_regex(name: str, expected: bool) -> None:
    """Path-safety regex covers shell meta, whitespace, quotes, newlines.

    Explicit ``.`` / ``..`` rejection is layered ON TOP of the regex —
    the regex's char class includes ``.`` so ``.`` and ``..`` match
    but are reserved path-traversal segments.
    """
    assert _is_safe_name(name) is expected


def test_is_safe_name_rejects_dot_and_dotdot_explicitly() -> None:
    """Round-1 PR #523 — `_is_safe_name(".")` and `_is_safe_name("..")`
    must return False even though the character-class regex matches
    them. Pinned independently of the parameterized test so a future
    refactor of the regex doesn't accidentally re-allow them."""
    assert _is_safe_name(".") is False
    assert _is_safe_name("..") is False


# ---------------------------------------------------------------------------
# render_cleanup_script — locks invariants in the rendered bash
# ---------------------------------------------------------------------------


def test_render_starts_with_set_euo_pipefail() -> None:
    """R1 — `set -euo pipefail` is the first executable line."""
    script = render_cleanup_script(["jupyter"])
    first_executable = next(
        line for line in script.splitlines() if line.strip() and not line.startswith("#")
    )
    assert first_executable == "set -euo pipefail"


def test_render_uses_grep_qfx_for_line_exact_fixed_string_match() -> None:
    """`grep -qFx --` for line-exact + fixed-string + option-terminator.

    -F prevents regex-meta false-positives, -x requires whole-line
    match (so 'jupyter' doesn't match 'jupyter-old'), -- prevents
    a name starting with '-' from being parsed as a flag.
    """
    script = render_cleanup_script(["jupyter"])
    assert "grep -qFx --" in script


def test_render_emits_result_line_format() -> None:
    """RESULT wire-format must match what parse_cleanup_result expects."""
    script = render_cleanup_script(["jupyter"])
    assert 'echo "RESULT stopped=$STOPPED removed=$REMOVED failed=$FAILED"' in script


def test_render_quotes_enabled_list_safely() -> None:
    """Enabled names land inside a shlex.quote'd string literal — no
    name (even if it had unsafe chars) can break out of the quote."""
    script = render_cleanup_script(["jupyter", "marimo"])
    # The list comes through as a single quoted argument, with names
    # joined by literal newlines that printf '%s\n' re-emits one per
    # line. This is the same shape every other rendered-bash module
    # uses.
    assert "ENABLED_LIST=" in script
    # Both names appear somewhere in the script (the exact
    # interpolation form is shlex's choice).
    assert "jupyter" in script
    assert "marimo" in script


def test_render_uses_docker_compose_yml_check_before_compose_down() -> None:
    """Compose-down only fires when docker-compose.yml is present —
    matches the canonical layout's branch (we don't try to `compose down` a
    folder that's just leftover assets)."""
    script = render_cleanup_script([])
    assert 'if [ -f "${stack_dir}docker-compose.yml" ]' in script
    assert "docker compose down" in script


def test_render_empty_enabled_removes_everything() -> None:
    """Empty enabled = every folder on the server is "not in enabled"
    = every folder gets removed. Pinned because the CLI explicitly
    documents this contract (a deploy with zero enabled services
    tears down every stack)."""
    script = render_cleanup_script([])
    # The rendered script still has the for loop; the ENABLED_LIST is empty.
    # printf '%s\n' "" produces a single empty line, and grep -qFx -- "<name>"
    # against just an empty line never matches a real name. Net: every
    # folder falls into the disabled branch.
    assert 'for stack_dir in "$STACKS_DIR"/*/' in script


# ---------------------------------------------------------------------------
# Exec'd-bash semantic tests — pin the actual matching/exit behaviour
# Modul-2.0 lesson: static-text tests don't catch dispatch bugs.
# ---------------------------------------------------------------------------


def _bash_can_be_invoked() -> bool:
    return shutil.which("bash") is not None


@pytest.mark.skipif(not _bash_can_be_invoked(), reason="bash not on PATH")
def test_exec_cleanup_skips_enabled_removes_disabled() -> None:
    """End-to-end: build a synthetic stacks dir, render, exec, verify
    the disabled folder is gone and the enabled one stays."""
    with tempfile.TemporaryDirectory() as tmp:
        stacks = Path(tmp)
        (stacks / "jupyter").mkdir()
        (stacks / "marimo").mkdir()
        (stacks / "old-stack").mkdir()
        # Don't drop docker-compose.yml so `compose down` doesn't
        # actually try to run docker — we just want to test the
        # rm -rf branch.

        script = render_cleanup_script(["jupyter", "marimo"], stacks_dir=str(stacks))
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "RESULT stopped=0 removed=1 failed=0" in proc.stdout
        assert (stacks / "jupyter").exists()
        assert (stacks / "marimo").exists()
        assert not (stacks / "old-stack").exists()


@pytest.mark.skipif(not _bash_can_be_invoked(), reason="bash not on PATH")
def test_exec_cleanup_line_exact_match_no_substring_collision() -> None:
    """Pin -x: 'jupyter' enabled must NOT shield 'jupyter-old' folder.

    grep without -x would match 'jupyter' as a substring of
    'jupyter-old' and incorrectly preserve the folder. -x requires
    whole-line equality, which is what we want.
    """
    with tempfile.TemporaryDirectory() as tmp:
        stacks = Path(tmp)
        (stacks / "jupyter").mkdir()
        (stacks / "jupyter-old").mkdir()

        script = render_cleanup_script(["jupyter"], stacks_dir=str(stacks))
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert (stacks / "jupyter").exists()
        # jupyter-old must be removed despite 'jupyter' being a substring
        assert not (stacks / "jupyter-old").exists()


@pytest.mark.skipif(not _bash_can_be_invoked(), reason="bash not on PATH")
def test_exec_cleanup_rm_runs_even_when_compose_down_fails() -> None:
    """Round-1 PR #523: a stuck container shouldn't block folder removal.

    The historical contract was ``docker compose down 2>/dev/null || true``
    followed by an unconditional ``rm -rf``. A previous version of this module
    inserted a ``continue`` after the down-failure branch which
    diverged from that contract and left orphan folders behind. This
    test pins the post-fix behaviour: when ``docker compose down``
    fails, the folder is STILL removed and BOTH counters move
    (failed=1 because the down failed, removed=1 because the rm
    succeeded).
    """
    with tempfile.TemporaryDirectory() as tmp_root:
        # fakebin must live OUTSIDE the stacks dir — otherwise the
        # cleanup loop iterates fakebin/ as a "disabled stack" and
        # rm -rf's it before reaching stuck-stack/, leaving the
        # subsequent docker invocation to fall through to the real
        # docker on PATH.
        fake_bin = Path(tmp_root) / "fakebin"
        fake_bin.mkdir()
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            '#!/bin/bash\necho "Error: stuck container" >&2\nexit 1\n',
        )
        fake_docker.chmod(0o755)

        stacks = Path(tmp_root) / "stacks"
        stacks.mkdir()
        bad = stacks / "stuck-stack"
        bad.mkdir()
        (bad / "docker-compose.yml").write_text("services: {}\n")

        script = render_cleanup_script([], stacks_dir=str(stacks))
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
        )
        assert proc.returncode == 0, proc.stderr
        # Down failed (compose.yml present, fake docker exits 1) but
        # rm -rf still succeeded → failed=1, removed=1.
        assert "RESULT stopped=0 removed=1 failed=1" in proc.stdout
        # Folder must be gone
        assert not bad.exists()
        # The captured stderr from the failed compose down must
        # surface in the script's stderr (not be silently swallowed).
        assert "Error: stuck container" in proc.stderr


@pytest.mark.skipif(not _bash_can_be_invoked(), reason="bash not on PATH")
def test_exec_cleanup_empty_stacks_dir_emits_zero_counts() -> None:
    """No folders → no actions, RESULT stopped=0 removed=0 failed=0."""
    with tempfile.TemporaryDirectory() as tmp:
        script = render_cleanup_script(["jupyter"], stacks_dir=tmp)
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "RESULT stopped=0 removed=0 failed=0" in proc.stdout


@pytest.mark.skipif(not _bash_can_be_invoked(), reason="bash not on PATH")
def test_exec_cleanup_handles_dotted_and_dashed_names() -> None:
    """Names with dots/dashes must round-trip through grep -F intact."""
    with tempfile.TemporaryDirectory() as tmp:
        stacks = Path(tmp)
        (stacks / "seaweedfs-filer").mkdir()
        (stacks / "v1.2").mkdir()
        (stacks / "old.stack").mkdir()

        script = render_cleanup_script(["seaweedfs-filer", "v1.2"], stacks_dir=str(stacks))
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert (stacks / "seaweedfs-filer").exists()
        assert (stacks / "v1.2").exists()
        assert not (stacks / "old.stack").exists()


# ---------------------------------------------------------------------------
# parse_cleanup_result
# ---------------------------------------------------------------------------


def test_parse_result_happy() -> None:
    out = "  Removing foo...\nRESULT stopped=2 removed=3 failed=0\n"
    parsed = parse_cleanup_result(out)
    assert parsed == CleanupResult(stopped=2, removed=3, failed=0)


def test_parse_result_missing_returns_none() -> None:
    """No RESULT line → None (caller treats as transport failure)."""
    assert parse_cleanup_result("garbage output, no RESULT here") is None


def test_parse_result_picks_first_match_only() -> None:
    """Multiple RESULT lines are unexpected; picking the first is fine."""
    out = "RESULT stopped=1 removed=1 failed=0\nRESULT stopped=99 removed=99 failed=99"
    parsed = parse_cleanup_result(out)
    assert parsed is not None
    assert parsed.stopped == 1


def test_cleanup_result_is_success_property() -> None:
    assert CleanupResult(stopped=1, removed=2, failed=0).is_success is True
    assert CleanupResult(stopped=0, removed=0, failed=1).is_success is False


# ---------------------------------------------------------------------------
# rsync_enabled_stacks — DI-based unit tests
# ---------------------------------------------------------------------------


def _ok_rsync(_local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")


def test_rsync_enabled_synced_status_for_existing_local_dirs(tmp_path: Path) -> None:
    (tmp_path / "jupyter").mkdir()
    (tmp_path / "marimo").mkdir()

    results = rsync_enabled_stacks(
        tmp_path,
        ["jupyter", "marimo"],
        rsync_runner=_ok_rsync,
    )
    assert all(r.status == "synced" for r in results)
    assert [r.service for r in results] == ["jupyter", "marimo"]


def test_rsync_enabled_missing_local_status(tmp_path: Path) -> None:
    """Local dir doesn't exist → status='missing-local', NOT failed."""
    results = rsync_enabled_stacks(
        tmp_path,
        ["nonexistent"],
        rsync_runner=_ok_rsync,
    )
    assert results[0].status == "missing-local"


def test_rsync_enabled_failed_status_on_rsync_rc_nonzero(tmp_path: Path) -> None:
    """rsync exits non-zero → status='failed' with rc in detail."""
    (tmp_path / "jupyter").mkdir()

    def fail_rsync(_local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(23, ["rsync"])

    results = rsync_enabled_stacks(
        tmp_path,
        ["jupyter"],
        rsync_runner=fail_rsync,
    )
    assert results[0].status == "failed"
    assert "rc=23" in results[0].detail


def test_rsync_failed_captures_stderr_excerpt(tmp_path: Path) -> None:
    """Round-2 PR #523: the captured rsync stderr must reach the
    RsyncResult so the CLI can surface it in the deploy log. Without
    this, operators get bare `rc=23` with no actionable signal."""
    (tmp_path / "jupyter").mkdir()

    def fail_rsync(_local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        exc = subprocess.CalledProcessError(23, ["rsync"])
        exc.stderr = "rsync: connection unexpectedly closed\nrsync error: code 23\n"
        exc.stdout = ""
        raise exc

    results = rsync_enabled_stacks(tmp_path, ["jupyter"], rsync_runner=fail_rsync)
    assert results[0].status == "failed"
    assert "connection unexpectedly closed" in results[0].stderr_excerpt
    assert "rsync error: code 23" in results[0].stderr_excerpt


def test_rsync_failed_truncates_pathological_stderr(tmp_path: Path) -> None:
    """Bound stderr_excerpt to ≤2000 chars so a pathological repeat-
    line situation can't flood the deploy log. The TAIL is kept (the
    actually-relevant final error line, not the file-by-file noise)."""
    (tmp_path / "jupyter").mkdir()
    huge = "rsync: file: " + ("x" * 100) + "\n"
    payload = huge * 100  # ~10KB
    payload += "rsync: connection broken at end\n"  # tail signal

    def fail_rsync(_local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        exc = subprocess.CalledProcessError(12, ["rsync"])
        exc.stderr = payload
        exc.stdout = ""
        raise exc

    results = rsync_enabled_stacks(tmp_path, ["jupyter"], rsync_runner=fail_rsync)
    assert len(results[0].stderr_excerpt) <= 2000
    # The tail must be preserved — that's where the actionable line lives
    assert "connection broken at end" in results[0].stderr_excerpt


def test_rsync_failed_handles_none_stderr_stdout(tmp_path: Path) -> None:
    """A test stub raising bare CalledProcessError leaves exc.stderr
    and exc.stdout as None (the default). The fallback to empty
    string keeps stderr_excerpt empty, no AttributeError."""
    (tmp_path / "jupyter").mkdir()

    def fail_rsync(_local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        # Note: stderr/stdout left as None (default)
        raise subprocess.CalledProcessError(1, ["rsync"])

    results = rsync_enabled_stacks(tmp_path, ["jupyter"], rsync_runner=fail_rsync)
    assert results[0].status == "failed"
    assert results[0].stderr_excerpt == ""


def test_rsync_enabled_failed_on_unsafe_name(tmp_path: Path) -> None:
    """Unsafe name → status='failed' BEFORE attempting rsync.

    Path-safety is the first gate; rsync_runner must not be called.
    """
    called = {"count": 0}

    def counting_rsync(local: Path, remote: str) -> subprocess.CompletedProcess[str]:
        called["count"] += 1
        return _ok_rsync(local, remote)

    results = rsync_enabled_stacks(
        tmp_path,
        ["foo;rm -rf /"],
        rsync_runner=counting_rsync,
    )
    assert results[0].status == "failed"
    assert "unsafe name" in results[0].detail
    assert called["count"] == 0


def test_rsync_enabled_continues_after_per_service_failure(tmp_path: Path) -> None:
    """One service's rsync failure does NOT abort the loop — strictly
    more forgiving than the caller's `set -e` semantics."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()

    call_log: list[str] = []

    def runner(local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        call_log.append(local.name)
        if local.name == "b":
            raise subprocess.CalledProcessError(1, ["rsync"])
        return _ok_rsync(local, _remote)

    results = rsync_enabled_stacks(tmp_path, ["a", "b", "c"], rsync_runner=runner)
    assert call_log == ["a", "b", "c"]
    assert [r.status for r in results] == ["synced", "failed", "synced"]


def test_rsync_enabled_targets_correct_remote_path(tmp_path: Path) -> None:
    """remote spec is `<host>:<stacks_dir>/<svc>/`."""
    (tmp_path / "jupyter").mkdir()
    captured: dict[str, str] = {}

    def capture(local: Path, remote: str) -> subprocess.CompletedProcess[str]:
        captured["remote"] = remote
        captured["local"] = str(local)
        return _ok_rsync(local, remote)

    rsync_enabled_stacks(
        tmp_path,
        ["jupyter"],
        rsync_runner=capture,
        remote_stacks_dir="/opt/foo",
        host="bar",
    )
    assert captured["remote"] == "bar:/opt/foo/jupyter/"
    assert captured["local"].endswith("jupyter")


# ---------------------------------------------------------------------------
# cleanup_disabled_stacks — DI + unsafe-name filter contract
# ---------------------------------------------------------------------------


def test_cleanup_filters_unsafe_names_from_enabled_set() -> None:
    """Unsafe names are dropped before rendering — they would NOT
    have been rsynced to the server, so the cleanup loop correctly
    treats their folder names as disabled."""
    captured: dict[str, str] = {}

    def runner(script: str) -> subprocess.CompletedProcess[str]:
        captured["script"] = script
        return subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="RESULT stopped=0 removed=0 failed=0", stderr=""
        )

    cleanup_disabled_stacks(
        ["jupyter", "evil; rm -rf /", "marimo"],
        script_runner=runner,
    )
    assert "evil" not in captured["script"]
    # safe names still made it
    assert "jupyter" in captured["script"]
    assert "marimo" in captured["script"]


def test_cleanup_returns_none_on_unparseable_result() -> None:
    """No RESULT in stdout → None (CLI maps to rc=2)."""

    def runner(_script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="garbage", stderr="")

    assert cleanup_disabled_stacks(["jupyter"], script_runner=runner) is None


def test_cleanup_forwards_diagnostics_to_local_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Per-stack ✓/✗/⚠ lines reach local stderr; RESULT does NOT."""

    def runner(_script: str) -> subprocess.CompletedProcess[str]:
        out = (
            "  Stopping old-stack (disabled)...\n"
            "  Removing old-stack stack folder...\n"
            "RESULT stopped=1 removed=1 failed=0\n"
        )
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=out, stderr="")

    cleanup_disabled_stacks(["jupyter"], script_runner=runner)
    captured = capsys.readouterr()
    assert "old-stack" in captured.err
    assert "RESULT stopped=" not in captured.err


# ---------------------------------------------------------------------------
# run_stack_sync — orchestration
# ---------------------------------------------------------------------------


def _ok_cleanup_runner(
    stopped: int = 0, removed: int = 0, failed: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh"],
        returncode=0,
        stdout=f"RESULT stopped={stopped} removed={removed} failed={failed}",
        stderr="",
    )


def test_run_stack_sync_happy_path(tmp_path: Path) -> None:
    (tmp_path / "jupyter").mkdir()
    (tmp_path / "marimo").mkdir()

    result = run_stack_sync(
        tmp_path,
        ["jupyter", "marimo"],
        rsync_runner=_ok_rsync,
        script_runner=lambda _: _ok_cleanup_runner(removed=2),
    )
    assert isinstance(result, StackSyncResult)
    assert result.synced == 2
    assert result.missing == 0
    assert result.failed_rsync == 0
    assert result.cleanup is not None
    assert result.cleanup.removed == 2
    assert result.is_success


def test_run_stack_sync_partial_failure(tmp_path: Path) -> None:
    """One rsync fails, cleanup OK → not is_success, but synced > 0."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    def runner(local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        if local.name == "b":
            raise subprocess.CalledProcessError(1, ["rsync"])
        return _ok_rsync(local, _remote)

    result = run_stack_sync(
        tmp_path,
        ["a", "b"],
        rsync_runner=runner,
        script_runner=lambda _: _ok_cleanup_runner(),
    )
    assert result.synced == 1
    assert result.failed_rsync == 1
    assert result.is_success is False


def test_run_stack_sync_cleanup_unparseable_means_not_success(tmp_path: Path) -> None:
    (tmp_path / "jupyter").mkdir()

    def bad_runner(_script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="garbage", stderr="")

    result = run_stack_sync(
        tmp_path,
        ["jupyter"],
        rsync_runner=_ok_rsync,
        script_runner=bad_runner,
    )
    assert result.cleanup is None
    assert result.is_success is False


# ---------------------------------------------------------------------------
# CLI rc=0/1/2 mapping (direct _stack_sync call, no subprocess)
# ---------------------------------------------------------------------------


def test_cli_stack_sync_missing_enabled_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nexus_deploy.__main__ import _stack_sync

    rc = _stack_sync([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--enabled" in err


def test_cli_stack_sync_unknown_arg_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nexus_deploy.__main__ import _stack_sync

    rc = _stack_sync(["--enabled", "jupyter", "--bogus"])
    assert rc == 2
    assert "unknown arg" in capsys.readouterr().err


def test_cli_stack_sync_bad_stacks_dir_returns_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nexus_deploy.__main__ import _stack_sync

    bogus = tmp_path / "does-not-exist"
    rc = _stack_sync(["--enabled", "jupyter", "--stacks-dir", str(bogus)])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_stack_sync_happy_returns_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()

    def fake_run(_local: Path, _enabled: list[str]) -> StackSyncResult:
        return StackSyncResult(
            rsync=(RsyncResult(service="jupyter", status="synced"),),
            cleanup=CleanupResult(stopped=0, removed=0, failed=0),
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", fake_run)
    rc = _stack_sync(["--enabled", "jupyter", "--stacks-dir", str(tmp_path)])
    assert rc == 0


def test_cli_stack_sync_partial_returns_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()
    (tmp_path / "marimo").mkdir()

    def fake_run(_local: Path, _enabled: list[str]) -> StackSyncResult:
        return StackSyncResult(
            rsync=(
                RsyncResult(service="jupyter", status="synced"),
                RsyncResult(service="marimo", status="failed", detail="rsync rc=1"),
            ),
            cleanup=CleanupResult(stopped=0, removed=0, failed=0),
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", fake_run)
    rc = _stack_sync(["--enabled", "jupyter,marimo", "--stacks-dir", str(tmp_path)])
    assert rc == 1


def test_cli_stack_sync_all_failed_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero rsync succeeded AND zero cleanup actions → rc=2."""
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()

    def fake_run(_local: Path, _enabled: list[str]) -> StackSyncResult:
        return StackSyncResult(
            rsync=(RsyncResult(service="jupyter", status="failed", detail="rsync rc=1"),),
            cleanup=CleanupResult(stopped=0, removed=0, failed=0),
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", fake_run)
    rc = _stack_sync(["--enabled", "jupyter", "--stacks-dir", str(tmp_path)])
    assert rc == 2


def test_cli_stack_sync_unparseable_cleanup_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cleanup=None (no RESULT line) → rc=2 even if rsync succeeded."""
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()

    def fake_run(_local: Path, _enabled: list[str]) -> StackSyncResult:
        return StackSyncResult(
            rsync=(RsyncResult(service="jupyter", status="synced"),),
            cleanup=None,
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", fake_run)
    rc = _stack_sync(["--enabled", "jupyter", "--stacks-dir", str(tmp_path)])
    assert rc == 2


def test_cli_stack_sync_forwards_rsync_stderr_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-2 PR #523: when an rsync failed, the CLI must print the
    captured stderr_excerpt to stderr so operators see WHY (not just
    `rc=23`). Each line indented under the per-service ✗ line."""
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()

    def fake_run(_local: Path, _enabled: list[str]) -> StackSyncResult:
        return StackSyncResult(
            rsync=(
                RsyncResult(
                    service="jupyter",
                    status="failed",
                    detail="rsync rc=23",
                    stderr_excerpt="rsync: connection unexpectedly closed\nrsync error: code 23",
                ),
            ),
            cleanup=CleanupResult(stopped=0, removed=0, failed=0),
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", fake_run)
    rc = _stack_sync(["--enabled", "jupyter", "--stacks-dir", str(tmp_path)])
    assert rc == 2  # all-failed → rc=2
    err = capsys.readouterr().err
    # Per-service ✗ line
    assert "✗ jupyter rsync failed" in err
    assert "rsync rc=23" in err
    # Indented stderr block — both lines surfaced, indented
    assert "      rsync: connection unexpectedly closed" in err
    assert "      rsync error: code 23" in err


def test_cli_stack_sync_rc2_on_transport_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ssh/rsync failure → rc=2; exc.cmd never leaks to stderr/stdout."""
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()

    def boom(_local: Path, _enabled: list[str]) -> StackSyncResult:
        raise subprocess.CalledProcessError(255, ["ssh", "secret-bearing-arg"])

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", boom)
    rc = _stack_sync(["--enabled", "jupyter", "--stacks-dir", str(tmp_path)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "transport failure" in captured.err
    assert "secret-bearing-arg" not in captured.err
    assert "secret-bearing-arg" not in captured.out


def test_cli_stack_sync_rc2_on_unexpected_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Programming errors → rc=2 (NOT Python's default rc=1).

    Class name only in stderr; no str/repr that could leak attribute
    values.
    """
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()

    def boom(_local: Path, _enabled: list[str]) -> StackSyncResult:
        raise RuntimeError("secret-bearing-message-NEVER-print")

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", boom)
    rc = _stack_sync(["--enabled", "jupyter", "--stacks-dir", str(tmp_path)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "secret-bearing-message-NEVER-print" not in captured.err
    assert "secret-bearing-message-NEVER-print" not in captured.out


def test_cli_stack_sync_filters_empty_csv_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`,jupyter,,marimo,` → ['jupyter', 'marimo'] — no empty entries."""
    from nexus_deploy.__main__ import _stack_sync

    (tmp_path / "jupyter").mkdir()
    (tmp_path / "marimo").mkdir()

    captured: dict[str, list[str]] = {}

    def fake_run(_local: Path, enabled: list[str]) -> StackSyncResult:
        captured["enabled"] = enabled
        return StackSyncResult(
            rsync=tuple(RsyncResult(service=s, status="synced") for s in enabled),
            cleanup=CleanupResult(stopped=0, removed=0, failed=0),
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_stack_sync", fake_run)
    rc = _stack_sync(["--enabled", ",jupyter,,marimo,", "--stacks-dir", str(tmp_path)])
    assert rc == 0
    assert captured["enabled"] == ["jupyter", "marimo"]


# ---------------------------------------------------------------------------
# Subprocess-level CLI smoke (one happy path through the real entry point)
# ---------------------------------------------------------------------------


def test_cli_subprocess_unknown_arg_returns_2() -> None:
    """`python -m nexus_deploy stack-sync --bogus` exits 2."""
    proc = subprocess.run(
        [sys.executable, "-m", "nexus_deploy", "stack-sync", "--bogus"],
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    assert proc.returncode == 2
    assert "unknown arg" in proc.stderr
