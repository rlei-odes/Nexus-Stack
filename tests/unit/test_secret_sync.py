"""Tests for nexus_deploy.secret_sync — Phase 1 Modul 1.2 (#505).

Eight round-tagged invariant tests (one per deploy.sh hardening round)
plus property tests for the dotenv-escape roundtrip and CLI integration
covering the rc=0/1/2 dispatch contract that deploy.sh's case-block
relies on.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from syrupy.assertion import SnapshotAssertion

from nexus_deploy.secret_sync import (
    StackTarget,
    SyncResult,
    escape_dotenv_value,
    has_multiline,
    is_safe_envfile_key,
    parse_result,
    render_remote_script,
    run_sync_for_stack,
)

# ---------------------------------------------------------------------------
# StackTarget — per-stack divergences match deploy.sh's path conventions
# ---------------------------------------------------------------------------


def test_stack_target_jupyter_paths() -> None:
    target = StackTarget(name="jupyter")
    assert target.env_file == "/opt/docker-server/stacks/jupyter/.infisical.env"
    assert target.legacy_env_file == "/opt/docker-server/stacks/jupyter/.env"
    assert target.compose_dir == "/opt/docker-server/stacks/jupyter"


def test_stack_target_marimo_paths() -> None:
    target = StackTarget(name="marimo")
    assert target.env_file == "/opt/docker-server/stacks/marimo/.infisical.env"
    assert target.legacy_env_file == "/opt/docker-server/stacks/marimo/.env"
    assert target.compose_dir == "/opt/docker-server/stacks/marimo"


def test_stack_target_begin_marker_capitalised() -> None:
    """Marker comment includes capitalised stack name (mirrors legacy deploy.sh wording)."""
    assert "Infisical → Jupyter env" in StackTarget(name="jupyter").begin_marker
    assert "Infisical → Marimo env" in StackTarget(name="marimo").begin_marker


# ---------------------------------------------------------------------------
# Pure-logic helpers — direct tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        ("FOO", True),
        ("FOO_BAR", True),
        ("FOO123", True),
        ("_LEADING_UNDERSCORE", True),
        ("a", True),
        ("1FOO", False),  # leading digit
        ("FOO-BAR", False),  # hyphen
        ("FOO BAR", False),  # space
        ("FOO.BAR", False),  # dot
        ("", False),  # empty
        ("FOO=BAR", False),  # equals
    ],
)
def test_is_safe_envfile_key(key: str, ok: bool) -> None:
    """Round 5 — POSIX shell-identifier rules. Mirror legacy deploy.sh secret-sync."""
    assert is_safe_envfile_key(key) is ok


@given(st.text(min_size=1, max_size=20))
def test_is_safe_envfile_key_property(text: str) -> None:
    """Property: result matches the regex deploy.sh uses."""
    expected = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text))
    assert is_safe_envfile_key(text) is expected


def test_has_multiline_round_6() -> None:
    """Round 6 — multi-line value guard."""
    assert has_multiline("with\nnewline") is True
    assert has_multiline("plain value") is False
    assert has_multiline("") is False
    # Carriage-return alone is not "multi-line" per the bash check
    # (deploy.sh's `grep -q $'\n'` only matches \n)
    assert has_multiline("with\rcarriage") is False


def test_escape_dotenv_value_basic() -> None:
    """Round 6 escape rules — mirror legacy deploy.sh secret-sync sed."""
    assert escape_dotenv_value("plain") == "plain"
    assert escape_dotenv_value('with"quote') == 'with\\"quote'
    assert escape_dotenv_value("with\\backslash") == "with\\\\backslash"
    # Order matters: backslash escape FIRST, then quote
    assert escape_dotenv_value('mix"\\') == 'mix\\"\\\\'


@given(st.text(alphabet=st.characters(blacklist_characters="\n\r\x00$`"), max_size=40))
@settings(max_examples=50, deadline=None)
def test_escape_dotenv_roundtrip_via_bash_eval(value: str) -> None:
    """Property: escape → embed in `K="..."` → bash-eval-parse → original.

    The escape is correct iff bash's dotenv-style assignment parses
    the escaped form back to the original. Excluded:
      - newlines / CR (filtered upstream by has_multiline)
      - NUL (env-var values can't carry NUL)
      - ``$`` and backtick — deploy.sh's sed escape doesn't neutralise
        them either (a parity choice, not a security claim). Such
        values are vanishingly rare in real Infisical content; if one
        occurs, the resulting ``.infisical.env`` line is bash-evaluated
        when docker-compose loads it. Tracked as a known-divergence
        from "fully shell-safe" semantics.
    """
    escaped = escape_dotenv_value(value)
    line = f'K="{escaped}"'
    completed = subprocess.run(
        ["bash", "-c", f"{line}\nprintf '%s' \"$K\""],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert completed.stdout == value


# ---------------------------------------------------------------------------
# render_remote_script — locks the 8 hardening rounds in the rendered bash
# ---------------------------------------------------------------------------


def _render_default(stack: str = "jupyter", **kwargs: Any) -> str:
    defaults: dict[str, Any] = {
        "target": StackTarget(name=stack),
        "project_id": "p",
        "infisical_token": "tok",
        "infisical_env": "dev",
        "gitea_token": "",
    }
    defaults.update(kwargs)
    return render_remote_script(**defaults)


def test_round_1_set_euo_pipefail_first_executable_line() -> None:
    """Round 1 — `set -euo pipefail` must be the FIRST command in the script.

    Otherwise an early failure (e.g. `mktemp` failing) wouldn't abort
    cleanly; the bash heredoc runs in a fresh shell that doesn't
    inherit the parent's `set` flags.
    """
    script = _render_default()
    first_executable = next(
        line for line in script.splitlines() if line and not line.startswith("#")
    )
    assert first_executable == "set -euo pipefail"


def test_round_3_trap_cleans_all_tmpfiles() -> None:
    """Round 3 — trap on EXIT removes every tmpfile.

    Any addition/removal of tmpfiles must be reflected in the trap;
    test ensures we don't drift. ``$TMP_OUT`` and ``$LEGACY_TMP`` are
    optional (created later in conditional branches) and must be
    guarded by a non-empty check inside the trap so an early-exit
    doesn't `rm -f ""`.
    """
    script = _render_default()
    trap_line = next(line for line in script.splitlines() if line.startswith("trap"))
    for var in ("$CFG", "$SEEN", "$APPEND", "$NEW_BLOCK", "$TSV", "$TMP_OUT", "$LEGACY_TMP"):
        assert var in trap_line, f"trap is missing {var}"
    # Optional tmpfiles must be guarded by a non-empty check
    assert '[ -n "$TMP_OUT" ]' in trap_line
    assert '[ -n "$LEGACY_TMP" ]' in trap_line


def test_round_4_two_stage_jq_validation() -> None:
    """Round 4 — both `.secrets | type == "array"` AND TSV extraction must succeed.

    If we lost the second check, a malformed `secretValue` could let
    SUCCEEDED++ fire while the TSV file is broken — the per-secret
    loop would then read garbage and possibly emit invalid env-vars.
    """
    script = _render_default()
    assert '.secrets | type == "array"' in script
    assert "@base64)] | @tsv" in script
    # The two checks gate SUCCEEDED++ in sequence (continue on either fail)
    assert script.count("FAILED=$((FAILED+1))") >= 2


def test_round_5_key_regex_inline() -> None:
    """Round 5 — exact regex deploy.sh uses, embedded in the rendered bash."""
    script = _render_default()
    assert "'^[A-Za-z_][A-Za-z0-9_]*$'" in script


def test_round_6_multiline_warning_does_not_emit_value() -> None:
    """Round 6 — the SKIPPED_MULTI warning logs only the key + folder, NEVER the value.

    Critical security invariant: if a secret value happens to contain
    a newline, the warning channel must not echo it (could leak partial
    secret to the deploy log). deploy.sh's wording was verbatim:
        "  ⚠ Skipping multi-line secret '$KEY' (folder '$FOLDER_LABEL')"
    """
    script = _render_default()
    skip_warning = next(
        line for line in script.splitlines() if "Skipping multi-line secret" in line
    )
    assert "$KEY" in skip_warning
    assert "$VALUE" not in skip_warning  # NEVER include the value
    assert "$VALUE_B64" not in skip_warning


def test_round_7_atomic_write_same_directory_mktemp() -> None:
    """Round 7 — `mktemp` for `.infisical.env` is in the SAME directory.

    Cross-filesystem `mv` falls back to copy+unlink (NOT atomic); a
    same-fs rename is atomic, which guarantees a Ctrl-C / SIGKILL
    can never leave $ENV_FILE in a half-state.
    """
    script = _render_default()
    assert 'TMP_OUT=$(mktemp -p "$(dirname "$ENV_FILE")"' in script
    # Make sure we mv (atomic rename), not cp
    mv_line = next(line for line in script.splitlines() if 'mv "$TMP_OUT"' in line)
    assert "$ENV_FILE" in mv_line


def test_round_8_two_outage_gates() -> None:
    """Round 8 — Gate 1 (succeeded==0) AND Gate 2 (pushed==0). Both → wrote=0, exit 0.

    Both gates must be present and BOTH must produce `wrote=0` so the
    existing `.infisical.env` is preserved. Removing either creates a
    silent secrets-wipe vector during partial Infisical outages.
    """
    script = _render_default()
    assert '[ "$SUCCEEDED" -eq 0 ]' in script  # Gate 1
    assert '[ "$PUSHED" -eq 0 ]' in script  # Gate 2
    # Both gates emit RESULT with wrote=0 + exit 0
    wrote_zero_count = script.count("succeeded=0 wrote=0") + script.count(
        "succeeded=$SUCCEEDED wrote=0"
    )
    assert wrote_zero_count >= 2


def test_render_quotes_token_safely() -> None:
    """Adversarial token can't break out of the rendered bash.

    A token with a literal single-quote would have escaped the heredoc
    in deploy.sh's old form (no shlex.quote). Python's shlex.quote
    closes that — verified by bash-eval'ing the TOKEN-extraction line
    against a pytest tmp canary.
    """
    nasty = "tok';rm -rf /;echo '"
    script = _render_default(infisical_token=nasty)
    # Token appears, but ONLY inside a shlex-quoted form. We extract
    # the assignment line and bash-eval just that, then check $ITOK.
    itok_line = next(line for line in script.splitlines() if line.startswith("ITOK="))
    completed = subprocess.run(
        ["bash", "-c", f'{itok_line}\nprintf "%s" "$ITOK"'],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert completed.stdout == nasty


def test_render_includes_legacy_env_strip() -> None:
    """Legacy `.env` block stripped only after successful new-file write.

    The ordering matters: if we stripped the legacy first and the new
    write failed, we'd lose the only working copy of the secrets.
    """
    script = _render_default()
    # WROTE=1 must come BEFORE the legacy-strip block
    wrote_idx = script.index("WROTE=1")
    # Find the LATEST legacy-strip occurrence (the actual sed call,
    # not the variable assignment at the top)
    legacy_strip_idx = script.rindex("LEGACY_ENV")
    assert wrote_idx < legacy_strip_idx


def test_render_jq_missing_path_emits_zero_result() -> None:
    """If jq is missing on the VM, the script emits a zero RESULT and exits 0.

    deploy.sh's pre-flight jq check exists so operators don't get
    misleading "all folder fetches failed" messages — they'd debug
    Infisical instead of installing jq.
    """
    script = _render_default()
    jq_check = next(line for line in script.splitlines() if "command -v jq" in line)
    assert jq_check.startswith("if ! command -v jq")
    # The fallback emits RESULT with all zeros + wrote=0
    assert (
        "pushed=0 skipped_name=0 skipped_multi=0 failed=0 collisions=0 succeeded=0 wrote=0"
        in script
    )


def test_render_marker_strings_match_legacy_format() -> None:
    """The BEGIN/END markers match deploy.sh's exact wording (sed-grep depends on it)."""
    jup = _render_default(stack="jupyter")
    mar = _render_default(stack="marimo")
    assert "BEGIN nexus-secret-sync (Infisical → Jupyter env" in jup
    assert "BEGIN nexus-secret-sync (Infisical → Marimo env" in mar
    # The strip regex anchors at start — must match the legacy comment
    assert "/^# === BEGIN nexus-secret-sync/,/^# === END nexus-secret-sync/d" in jup


# ---------------------------------------------------------------------------
# parse_result — RESULT line extraction
# ---------------------------------------------------------------------------


def test_parse_result_full_line() -> None:
    stdout = "some warnings here\nRESULT pushed=5 skipped_name=2 skipped_multi=1 failed=0 collisions=3 succeeded=4 wrote=1\nfooter"
    result = parse_result(stdout)
    assert result is not None
    assert result == SyncResult(
        pushed=5,
        skipped_invalid_name=2,
        skipped_multiline=1,
        failed_folders=0,
        collisions=3,
        succeeded_folders=4,
        wrote=True,
    )


def test_parse_result_wrote_zero() -> None:
    stdout = (
        "RESULT pushed=0 skipped_name=0 skipped_multi=0 failed=0 collisions=0 succeeded=0 wrote=0"
    )
    result = parse_result(stdout)
    assert result is not None
    assert result.wrote is False
    assert result.pushed == 0


def test_parse_result_missing_returns_none() -> None:
    """A missing RESULT line yields None — caller maps to a 'no parseable result' path."""
    assert parse_result("some unrelated output") is None
    assert parse_result("") is None


def test_parse_result_must_anchor_at_line_start() -> None:
    """A stray 'RESULT ...' substring inside another sentence must not match."""
    stdout = "  warning: see RESULT pushed=1 ... above for details\n"
    assert parse_result(stdout) is None


def test_sync_result_is_partial() -> None:
    base_kwargs: dict[str, Any] = {
        "pushed": 5,
        "skipped_invalid_name": 0,
        "skipped_multiline": 0,
        "collisions": 0,
        "succeeded_folders": 1,
    }
    assert SyncResult(failed_folders=0, wrote=True, **base_kwargs).is_partial is False
    assert SyncResult(failed_folders=2, wrote=True, **base_kwargs).is_partial is True
    assert SyncResult(failed_folders=2, wrote=False, **base_kwargs).is_partial is False


# ---------------------------------------------------------------------------
# Round 8 outage-gate: gates are simulated via the script_runner mock,
# proving run_sync_for_stack handles the wrote=False path correctly.
# ---------------------------------------------------------------------------


def _ok_script_runner(stdout: str) -> Any:
    def runner(_script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr="")

    return runner


def _no_op_command_runner() -> Any:
    def runner(_cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")

    return runner


def test_round_8_gate_1_succeeded_zero_yields_wrote_false() -> None:
    """Gate 1 — no folder fetch succeeded → wrote=False, no restart issued."""
    target = StackTarget(name="jupyter")
    out = "RESULT pushed=0 skipped_name=0 skipped_multi=0 failed=3 collisions=0 succeeded=0 wrote=0"
    restart_called = {"n": 0}

    def cmd_runner(_cmd: str) -> subprocess.CompletedProcess[str]:
        restart_called["n"] += 1
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")

    result = run_sync_for_stack(
        target,
        project_id="p",
        infisical_token="t",
        script_runner=_ok_script_runner(out),
        command_runner=cmd_runner,
    )
    assert result.wrote is False
    assert result.failed_folders == 3
    assert restart_called["n"] == 0  # NO restart on wrote=False


def test_round_8_gate_2_pushed_zero_yields_wrote_false() -> None:
    """Gate 2 — folder fetches OK but no usable secrets → wrote=False."""
    target = StackTarget(name="marimo")
    out = "RESULT pushed=0 skipped_name=0 skipped_multi=0 failed=0 collisions=0 succeeded=2 wrote=0"
    result = run_sync_for_stack(
        target,
        project_id="p",
        infisical_token="t",
        script_runner=_ok_script_runner(out),
        command_runner=_no_op_command_runner(),
    )
    assert result.wrote is False
    assert result.succeeded_folders == 2


def test_run_sync_invokes_restart_on_wrote_true() -> None:
    """`docker compose up -d <stack>` runs after a successful write."""
    target = StackTarget(name="jupyter")
    out = "RESULT pushed=5 skipped_name=0 skipped_multi=0 failed=0 collisions=0 succeeded=2 wrote=1"
    captured: dict[str, str] = {}

    def cmd_runner(cmd: str) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")

    result = run_sync_for_stack(
        target,
        project_id="p",
        infisical_token="t",
        script_runner=_ok_script_runner(out),
        command_runner=cmd_runner,
    )
    assert result.wrote is True
    assert "/opt/docker-server/stacks/jupyter" in captured["cmd"]
    assert "docker compose up -d jupyter" in captured["cmd"]


def test_run_sync_restart_failure_does_not_alter_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Restart-on-change failure surfaces via stderr but doesn't change SyncResult.

    The sync itself was successful (wrote=True) — the operator already
    has a fresh `.infisical.env`. A failed restart-on-change is a
    separate concern (next spin-up will pick it up); we don't reverse
    the result.
    """
    target = StackTarget(name="jupyter")
    out = "RESULT pushed=5 skipped_name=0 skipped_multi=0 failed=0 collisions=0 succeeded=2 wrote=1"

    def failing_cmd(_cmd: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            1, ["ssh"], output="pull failed: image not found", stderr=""
        )

    result = run_sync_for_stack(
        target,
        project_id="p",
        infisical_token="t",
        script_runner=_ok_script_runner(out),
        command_runner=failing_cmd,
    )
    assert result.wrote is True
    captured = capsys.readouterr()
    # Warning goes to stderr (matches the docstring contract)
    assert "docker compose up -d jupyter failed" in captured.err
    assert "rc=1" in captured.err
    # Captured docker-compose output is forwarded so the operator can debug
    assert "pull failed: image not found" in captured.err
    # exc.cmd/argv must NOT leak (defence in depth)
    assert "['ssh']" not in captured.out
    assert "['ssh']" not in captured.err


def test_run_sync_no_result_returns_zero_struct() -> None:
    """Remote stdout without RESULT line → SyncResult all-zeros, wrote=False."""
    target = StackTarget(name="jupyter")
    result = run_sync_for_stack(
        target,
        project_id="p",
        infisical_token="t",
        script_runner=_ok_script_runner("garbage output"),
        command_runner=_no_op_command_runner(),
    )
    assert result == SyncResult(
        pushed=0,
        skipped_invalid_name=0,
        skipped_multiline=0,
        failed_folders=0,
        collisions=0,
        succeeded_folders=0,
        wrote=False,
    )


def test_run_sync_forwards_remote_warnings_to_local_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Remote diagnostic lines reach local stderr; RESULT is stripped.

    Operationally critical: when the remote script skips a multi-line
    secret, drops a malformed folder, or fires an outage gate, the
    operator sees the warning in the local workflow log. The legacy
    deploy.sh heredoc had this for free (no capture); the migration
    must replicate it explicitly because `_remote.ssh_run_script`
    captures stdout/stderr.
    """
    target = StackTarget(name="jupyter")
    remote_output = (
        "  ⚠ Infisical fetch '<root>' returned bad shape, skipping\n"
        "  ⚠ Skipping multi-line secret 'PEM_KEY' (folder 'storage')\n"
        "RESULT pushed=3 skipped_name=0 skipped_multi=1 failed=1 collisions=0 succeeded=2 wrote=1\n"
    )
    run_sync_for_stack(
        target,
        project_id="p",
        infisical_token="t",
        script_runner=_ok_script_runner(remote_output),
        command_runner=_no_op_command_runner(),
    )
    captured = capsys.readouterr()
    assert "Infisical fetch '<root>' returned bad shape" in captured.err
    assert "Skipping multi-line secret 'PEM_KEY'" in captured.err
    # RESULT line is wire-format, not human-readable — must NOT pollute stderr
    assert "RESULT pushed=" not in captured.err
    assert "RESULT pushed=" not in captured.out


# ---------------------------------------------------------------------------
# Snapshot — full rendered script for both stacks (locks every detail)
# ---------------------------------------------------------------------------


def test_render_jupyter_snapshot(snapshot: SnapshotAssertion) -> None:
    """Full rendered script for a known fixture set — locks every byte."""
    script = render_remote_script(
        target=StackTarget(name="jupyter"),
        project_id="snapshot-project",
        infisical_token="snapshot-token",
        infisical_env="dev",
        gitea_token="snapshot-gitea-token",
    )
    assert script == snapshot


def test_render_marimo_snapshot(snapshot: SnapshotAssertion) -> None:
    script = render_remote_script(
        target=StackTarget(name="marimo"),
        project_id="snapshot-project",
        infisical_token="snapshot-token",
        infisical_env="dev",
        gitea_token="snapshot-gitea-token",
    )
    assert script == snapshot


# ---------------------------------------------------------------------------
# CLI — `nexus-deploy secret-sync --stack <jupyter|marimo>`
# ---------------------------------------------------------------------------


def test_cli_secret_sync_missing_stack_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "--stack <jupyter|marimo> is required" in captured.err


def test_cli_secret_sync_unknown_stack_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "redpanda"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown stack" in captured.err


def test_cli_secret_sync_unknown_arg_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--bogus"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown arg" in captured.err


def test_cli_secret_sync_stack_without_value_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "--stack requires a value" in captured.err


def test_cli_secret_sync_missing_env_vars_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "jupyter"])
    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "PROJECT_ID and INFISICAL_TOKEN" in captured.err


def test_cli_secret_sync_happy_path_returns_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful sync (wrote=True, failed=0, collisions=0) → rc=0."""
    from nexus_deploy.__main__ import main

    out = "RESULT pushed=7 skipped_name=0 skipped_multi=0 failed=0 collisions=0 succeeded=2 wrote=1"

    def fake_script(_s: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=out, stderr="")

    def fake_cmd(_c: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.ssh_run_script", fake_script)
    monkeypatch.setattr("nexus_deploy._remote.ssh_run", fake_cmd)
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "jupyter"])
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "wrote 7 env-vars" in captured.out


def test_cli_secret_sync_partial_returns_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """wrote=True AND failed_folders>0 → rc=1 (deploy.sh warns + continues)."""
    from nexus_deploy.__main__ import main

    out = "RESULT pushed=5 skipped_name=0 skipped_multi=0 failed=2 collisions=0 succeeded=3 wrote=1"

    def fake_script(_s: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("nexus_deploy._remote.ssh_run_script", fake_script)
    monkeypatch.setattr(
        "nexus_deploy._remote.ssh_run",
        lambda _c: subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "marimo"])
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "2 folder fetch(es) failed" in captured.out


def test_cli_secret_sync_outage_gate_returns_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """wrote=False from one of the two outage gates → rc=0 (deploy.sh continues)."""
    from nexus_deploy.__main__ import main

    out = "RESULT pushed=0 skipped_name=0 skipped_multi=0 failed=3 collisions=0 succeeded=0 wrote=0"

    def fake_script(_s: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("nexus_deploy._remote.ssh_run_script", fake_script)
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "jupyter"])
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "skipped" in captured.out


def test_cli_secret_sync_transport_failure_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh/rsync transport error → rc=2 (deploy.sh aborts)."""
    from nexus_deploy.__main__ import main

    def failing_script(_s: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(255, ["ssh", "secret-token-leak-attempt"])

    monkeypatch.setattr("nexus_deploy._remote.ssh_run_script", failing_script)
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "jupyter"])
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "transport failure" in captured.err
    # Defence-in-depth: argv (which would carry the secret-shaped payload) must NOT surface
    assert "secret-token-leak-attempt" not in captured.err
    assert "secret-token-leak-attempt" not in captured.out


def test_cli_secret_sync_unexpected_exception_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-transport exception (KeyError etc.) → rc=2; secret args must not leak."""
    from nexus_deploy.__main__ import main

    secret = "very-secret-value-must-not-appear"

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise KeyError(secret)

    monkeypatch.setattr("nexus_deploy.__main__.run_sync_for_stack", boom)
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "jupyter"])
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "unexpected error (KeyError)" in captured.err
    # Class name only; exception args (which include the secret) must not surface
    assert secret not in captured.err
    assert secret not in captured.out


def test_cli_secret_sync_no_result_line_returns_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote stdout w/o RESULT line → all-zero SyncResult → rc=0 + warning.

    Mirrors deploy.sh's `[ -z "$JUP_PUSHED" ]` "no-result" path: the
    inner script's stderr already explained why; here we just don't
    abort the deploy.
    """
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(
        "nexus_deploy._remote.ssh_run_script",
        lambda _s: subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="garbage", stderr=""
        ),
    )
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "secret-sync", "--stack", "jupyter"])
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "no usable result" in captured.out
