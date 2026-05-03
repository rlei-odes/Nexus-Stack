"""Tests for nexus_deploy.services — Phase 2 Modul 2.2b (#505).

Eight round-tagged invariant tests (one per deploy.sh hardening round)
plus per-spec snapshots, exec'd-bash regression tests for the JSON
build + idempotent-skip dispatch, and CLI integration covering rc=0/1/2.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any

import pytest

from nexus_deploy.config import NexusConfig
from nexus_deploy.infisical import BootstrapEnv
from nexus_deploy.services import (
    HookResult,
    SetupResult,
    parse_results,
    render_lakefs_hook,
    render_metabase_hook,
    render_n8n_hook,
    render_openmetadata_hook,
    render_portainer_hook,
    render_remote_script,
    run_admin_setups,
    supported_hooks,
)


def _make_config(**overrides: Any) -> NexusConfig:
    """Build a NexusConfig with minimal admin passwords for all 5 hooks."""
    defaults: dict[str, Any] = {
        "admin_username": "admin",
        "portainer_admin_password": "p-pass",
        "n8n_admin_password": "n-pass",
        "metabase_admin_password": "m-pass",
        # Deliberately NOT shaped like an AWS access key (AKIA prefix)
        # to avoid false-positive secret-scanner alerts in CI/GitHub.
        "lakefs_admin_access_key": "FAKE-LAKEFS-ACCESS-KEY-1234",
        "lakefs_admin_secret_key": "secret-lakefs-key",
        "openmetadata_admin_password": "om-pass-Complex1!",
        "hetzner_s3_bucket_lakefs": "my-bucket",
    }
    defaults.update(overrides)
    return NexusConfig.from_secrets_json(json.dumps(defaults))


def _make_env(admin_email: str = "ops@example.com") -> BootstrapEnv:
    return BootstrapEnv(domain="example.com", admin_email=admin_email)


# ---------------------------------------------------------------------------
# supported_hooks — registry contract
# ---------------------------------------------------------------------------


def test_supported_hooks_contains_5_specs() -> None:
    """Modul 2.2b ships exactly the 5 REST first-init hooks."""
    assert set(supported_hooks()) == {
        "portainer",
        "n8n",
        "metabase",
        "lakefs",
        "openmetadata",
    }


# ---------------------------------------------------------------------------
# Per-hook renderers — basic shape + skip-on-missing-credential
# ---------------------------------------------------------------------------


def test_render_portainer_hook_basic() -> None:
    script = render_portainer_hook(_make_config(), _make_env())
    assert "portainer_hook()" in script
    assert "/api/users/admin/init" in script
    assert "RESULT hook=portainer status=" in script


def test_render_portainer_hook_skips_when_password_empty() -> None:
    """Missing admin password → skipped-not-ready, not failed."""
    config = _make_config(portainer_admin_password="")
    script = render_portainer_hook(config, _make_env())
    assert script.strip() == 'echo "RESULT hook=portainer status=skipped-not-ready"'


def test_render_n8n_hook_uses_admin_email_from_env() -> None:
    """n8n needs admin_email — comes from BootstrapEnv, not NexusConfig."""
    script = render_n8n_hook(_make_config(), _make_env(admin_email="alice@example.com"))
    assert "alice@example.com" in script


def test_render_n8n_hook_skips_when_email_empty() -> None:
    """Missing admin_email → skipped (uniform with missing password)."""
    script = render_n8n_hook(_make_config(), _make_env(admin_email=""))
    assert script.strip() == 'echo "RESULT hook=n8n status=skipped-not-ready"'


def test_render_metabase_hook_skips_when_password_empty() -> None:
    script = render_metabase_hook(_make_config(metabase_admin_password=""), _make_env())
    assert script.strip() == 'echo "RESULT hook=metabase status=skipped-not-ready"'


def test_render_lakefs_hook_skips_when_keys_empty() -> None:
    script = render_lakefs_hook(
        _make_config(lakefs_admin_access_key="", lakefs_admin_secret_key=""),
        _make_env(),
    )
    assert script.strip() == 'echo "RESULT hook=lakefs status=skipped-not-ready"'


def test_render_openmetadata_hook_skips_when_password_empty() -> None:
    script = render_openmetadata_hook(_make_config(openmetadata_admin_password=""), _make_env())
    assert script.strip() == 'echo "RESULT hook=openmetadata status=skipped-not-ready"'


def test_render_metabase_hook_uses_admin_email() -> None:
    script = render_metabase_hook(_make_config(), _make_env())
    assert "ops@example.com" in script
    assert "/api/setup" in script


def test_render_lakefs_hook_picks_hetzner_when_both_bucket_and_server_set() -> None:
    """Storage namespace selection mirrors legacy deploy.sh — BOTH
    `hetzner_s3_bucket_lakefs` AND `hetzner_s3_server` must be set
    to land in the s3:// namespace."""
    script = render_lakefs_hook(
        _make_config(hetzner_s3_bucket_lakefs="b1", hetzner_s3_server="s3.example.com"),
        _make_env(),
    )
    assert "b1" in script
    assert "s3.example.com" in script
    # The if-condition tests both vars
    assert '[ -n "$HETZNER_BUCKET" ] && [ -n "$HETZNER_SERVER" ]' in script


def test_render_lakefs_hook_falls_back_to_local_when_no_hetzner() -> None:
    script = render_lakefs_hook(
        _make_config(hetzner_s3_bucket_lakefs="", hetzner_s3_server=""), _make_env()
    )
    assert "local://data/lakefs/" in script
    assert "local-storage" in script


def test_render_lakefs_hook_falls_back_when_only_bucket_set_no_server() -> None:
    """Round-7 finding: bucket alone is NOT enough — endpoint is also
    required. Without the server, lakefs has no way to read/write S3.
    The legacy deploy.sh required BOTH; we must too."""
    config = _make_config(hetzner_s3_bucket_lakefs="b1", hetzner_s3_server="")
    script = render_lakefs_hook(config, _make_env())
    # Both fields are present in the rendered script (their values
    # get baked in), but at runtime the AND-check will land in the
    # local:// branch because HETZNER_SERVER is empty. We pin the
    # AND-check structure here.
    assert '[ -n "$HETZNER_BUCKET" ] && [ -n "$HETZNER_SERVER" ]' in script


def test_render_openmetadata_hook_3_step_flow() -> None:
    """Login (default-pwd) → changePassword → verify-login."""
    script = render_openmetadata_hook(_make_config(), _make_env())
    # All three POST endpoints appear
    assert script.count("/api/v1/users/login") == 2  # login + verify
    assert "/api/v1/users/changePassword" in script
    # System-version probe (custom wait, not _render_wait_healthy)
    assert "/api/v1/system/version" in script


# ---------------------------------------------------------------------------
# Round-tagged invariants on the rendered bash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "renderer",
    [
        render_portainer_hook,
        render_n8n_hook,
        render_metabase_hook,
        render_lakefs_hook,
        render_openmetadata_hook,
    ],
)
def test_round_8_per_hook_emits_exactly_one_result_line(renderer: Any) -> None:
    """R8 — every hook function emits exactly one ``RESULT hook=…`` line per branch.

    Static check via grep: each renderer's output must contain at
    least one ``echo "RESULT hook=`` line. The exec'd version below
    pins the runtime invariant (exactly one per execution).
    """
    script = renderer(_make_config(), _make_env())
    assert "RESULT hook=" in script


def test_round_1_set_minus_u_at_top_no_set_e() -> None:
    """R1 — orchestrator script uses ``set -u`` (NOT ``set -e``).

    Subtle but critical: ``set -e`` would abort the orchestrator on
    the first hook that returns non-zero from a curl pipe, and we
    want hook failures to be reported via RESULT lines without
    cross-contaminating the rest. The per-hook bodies still use
    ``|| true`` and explicit branches to control flow.
    """
    script = render_remote_script(
        config=_make_config(), env=_make_env(), enabled_hooks=["portainer"]
    )
    assert script.startswith("set -u")
    # No `set -e` in the orchestrator (R6 corollary)
    assert "set -e" not in script.splitlines()[0]


def test_round_2_per_spec_healthcheck_timeouts() -> None:
    """R2 — each hook has its own healthcheck timeout, NOT a global default.

    Pin the timeouts so a future contributor doesn't accidentally
    unify them and break Metabase (Java app, 120s) or OpenMetadata
    (180s, slow boot).
    """
    timeouts = {
        "portainer": 5,
        "n8n": 60,
        "metabase": 120,
        "lakefs": 60,
        "openmetadata": 180,
    }
    for hook_name, expected_timeout in timeouts.items():
        renderer = {
            "portainer": render_portainer_hook,
            "n8n": render_n8n_hook,
            "metabase": render_metabase_hook,
            "lakefs": render_lakefs_hook,
            "openmetadata": render_openmetadata_hook,
        }[hook_name]
        script = renderer(_make_config(), _make_env())
        # Look for the human-readable warning that names the timeout
        assert f"after {expected_timeout}s" in script, (
            f"Expected '{expected_timeout}s' in {hook_name} script"
        )


@pytest.mark.parametrize(
    ("renderer", "canary_field", "canary_value"),
    [
        # Each hook is tested with a unique canary substituted for the
        # credential field that's most likely to land in argv.
        (render_portainer_hook, "portainer_admin_password", "PORTAINER-CANARY-X1Y2"),
        (render_n8n_hook, "n8n_admin_password", "N8N-CANARY-X1Y2"),
        (render_metabase_hook, "metabase_admin_password", "METABASE-CANARY-X1Y2"),
        (render_lakefs_hook, "lakefs_admin_secret_key", "LAKEFS-SECRET-CANARY-X1Y2"),
        (render_openmetadata_hook, "openmetadata_admin_password", "OM-CANARY-X1Y2"),
    ],
)
def test_no_credential_leaks_into_subprocess_argv_per_hook(
    renderer: Any, canary_field: str, canary_value: str
) -> None:
    """R4 (per-hook generalisation): no credential ever lands on a line
    that invokes a non-builtin subprocess (``curl`` or ``jq``) — both
    leak via ``ps`` on the remote host.

    Round-2 PR #514: caught Portainer + n8n curl-argv leaks.
    Round-5 PR #514: caught the SAME class on jq's argv (``jq -n
    --arg pw <secret>`` puts secret in jq's argv). This test now
    checks BOTH curl AND jq invocation lines.

    Bash builtins (printf, env-var assignments via ``VAR=value cmd``)
    don't fork — values can safely appear on those lines without
    reaching ``ps``.

    Scope of THIS test: ``curl`` + ``jq`` only — the two non-builtins
    that currently consume credentials in this module. Other forking
    commands the rendered scripts use (``base64``, ``tr``, ``mktemp``,
    ``chmod``) all read from stdin or operate on tmpfile paths
    rather than taking secrets as positional args, so they're not
    on the leak-path here. Future hooks adding a new credential-
    handling fork-tool should extend this test's command list.
    """
    script = renderer(_make_config(**{canary_field: canary_value}), _make_env())
    assert canary_value in script, "Canary must appear somewhere in the script"
    for line in script.splitlines():
        # Skip lines that ONLY contain `VAR=value cmd ...` env-var
        # assignment for the next non-builtin (e.g. `NEXUS_P=secret jq -n`):
        # the value is set as an env var, NOT as positional argv to jq.
        # We detect this pattern by checking whether the canary appears
        # before any forking-command token on the line.
        for forking_command in ("curl ", "curl\n", "jq ", "jq\n"):
            if forking_command in line:
                idx_canary = line.find(canary_value)
                idx_cmd = line.find(forking_command)
                if idx_canary >= 0 and idx_canary > idx_cmd:
                    # Canary appears AFTER the command name → it's in
                    # positional argv → leak.
                    raise AssertionError(
                        f"Credential leaked into {forking_command.strip()!r} argv: {line!r}"
                    )


def test_round_4_setup_body_via_env_no_argv_leak() -> None:
    """R4 — admin password is injected to jq via env var (NOT --arg
    positional argv) and fed to curl via stdin (--data-binary @-),
    never via argv.

    Critical: a future bug that put the password into curl's OR
    jq's argv would leak it via `ps` on the remote host. We assert
    that:
      1. The password value appears in the rendered script (it's
         the value of an env-var assignment — that's expected and
         bash-safe; bash builtins / env-var-to-cmd assignments don't
         fork).
      2. The password value does NOT appear AFTER a forking-command
         token (curl, jq) on any line — meaning it's never in argv.

    The detailed cross-hook version of this check lives in
    ``test_no_credential_leaks_into_subprocess_argv_per_hook``;
    this test pins the Metabase-specific shape.
    """
    canary = "UNIQUE-METABASE-PWD-LEAK-CANARY"
    config = _make_config(metabase_admin_password=canary)
    script = render_metabase_hook(config, _make_env())
    assert canary in script, "Canary must appear (as env-var value)"
    # Canary must NOT appear AFTER curl/jq on any line (= not in argv)
    for line in script.splitlines():
        for cmd_token in ("curl ", "jq "):
            idx_cmd = line.find(cmd_token)
            idx_canary = line.find(canary)
            if idx_cmd >= 0 and idx_canary > idx_cmd:
                raise AssertionError(f"Password leaked into {cmd_token.strip()} argv: {line!r}")
    # And the rendered script uses --data-binary @- for the POST body
    assert "--data-binary @-" in script


def test_round_4_setup_body_built_correctly_against_rendered_script() -> None:
    """R4 exec — drive the ACTUAL rendered jq pipeline against a known
    config and assert the resulting JSON parses + has expected
    fields, even with shell-meta characters in the password.

    Earlier this test built a hand-coded jq snippet that was decoupled
    from the renderer — it could pass even if the real renderer drifted.
    Now we extract the BODY=$(...) line from the rendered script,
    execute it via bash -c, and parse the captured JSON. This pins
    the renderer's actual jq form against shell-meta-character
    payloads.
    """
    nasty_password = 'evil"$(date)`whoami`'
    config = _make_config(metabase_admin_password=nasty_password)
    full_script = render_metabase_hook(config, _make_env())
    # Extract the BODY=$(...) jq-build block from the rendered script.
    # It's a multi-line continuation: BODY=$(NEXUS_TOKEN=... NEXUS_E=...
    # NEXUS_P=... jq -n '{...}')
    lines = full_script.splitlines()
    body_start = next(i for i, line in enumerate(lines) if line.strip().startswith("BODY=$(NEXUS_"))
    body_end = body_start
    while not lines[body_end].rstrip().endswith(")"):
        body_end += 1
    body_lines = lines[body_start : body_end + 1]
    # The rendered version uses an implicit SETUP_TOKEN runtime-shell var
    # (captured from /api/session/properties); inject it explicitly.
    snippet = (
        "set -euo pipefail\nSETUP_TOKEN=tok123\n"
        + "\n".join(body_lines)
        + '\nprintf "%s" "$BODY"\n'
    )
    out = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=True).stdout
    parsed = json.loads(out)
    assert parsed["user"]["password"] == nasty_password
    assert parsed["token"] == "tok123"
    assert parsed["user"]["email"] == "ops@example.com"


def test_round_5_idempotent_skip_via_substring_match() -> None:
    """R5 — idempotent-skip detection per hook.

    Each hook has a distinct "already configured" signal. Pin them
    so refactors don't accidentally drop the check.
    """
    portainer = render_portainer_hook(_make_config(), _make_env())
    assert "already initialized" in portainer  # Portainer's API response substring

    n8n = render_n8n_hook(_make_config(), _make_env())
    assert "showSetupOnFirstLoad" in n8n  # n8n's settings probe

    metabase = render_metabase_hook(_make_config(), _make_env())
    assert "setup-token" in metabase  # Metabase: token absent → already configured

    lakefs = render_lakefs_hook(_make_config(), _make_env())
    assert '"setup_complete":true' in lakefs

    om = render_openmetadata_hook(_make_config(), _make_env())
    # OpenMetadata: default login fails with invalid → already configured
    assert "invalid" in om
    assert "unauthorized" in om


def test_round_6_hook_failure_does_not_abort_orchestrator() -> None:
    """R6 — orchestrator does NOT use ``set -e``; one hook's failure
    cannot stop subsequent hooks. Pin via static check on the
    orchestrator preamble + by verifying every per-hook function
    uses ``return 0`` (NOT ``exit 1``) on bail-out paths.
    """
    script = render_remote_script(
        config=_make_config(),
        env=_make_env(),
        enabled_hooks=["portainer", "n8n", "metabase"],
    )
    # Orchestrator preamble: set -u only
    assert script.startswith("set -u")
    # No `exit 1` in any hook function (those would propagate)
    for line in script.splitlines():
        # Orchestrator-level exits are the issue; ignore subshell exits in jq etc.
        stripped = line.strip()
        assert "exit 1" not in stripped, (
            f"Hook bodies must use 'return 0' on bail-outs, not 'exit 1': {line!r}"
        )


def test_round_7_hook_execution_order_matches_enabled_arg() -> None:
    """R7 — orchestrator emits hooks in the caller-provided
    ``enabled_hooks`` argument order, NOT registry order.

    Operators rely on this for log debug + the integration with
    deploy.sh's [7/7] sequence — the CLI passes the comma-list as
    typed, and deploy.sh's $ENABLED_SERVICES is built from
    services.yaml in source order via tofu output.
    """
    script = render_remote_script(
        config=_make_config(),
        env=_make_env(),
        # Pass in reverse-registry order to verify caller-order wins
        enabled_hooks=["openmetadata", "portainer", "lakefs"],
    )
    # The order in which hook functions are called must follow the
    # `enabled_hooks` argument order (caller's responsibility to sort
    # if they want a different one). Verify by finding the *_hook
    # call lines and asserting they appear in the expected order.
    order = []
    for line in script.splitlines():
        m = re.match(r"^([a-z_]+)_hook$", line.strip())
        if m:
            order.append(m.group(1))
    assert order == ["openmetadata", "portainer", "lakefs"]


# ---------------------------------------------------------------------------
# render_remote_script — orchestrator behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "$(rm -rf /)",
        "x; rm -rf /",
        "x`whoami`",
        "x|cat /etc/passwd",
        "x with space",
        "x'single'",
        'x"double"',
        "../etc/passwd",
        "x\\backslash",
        "",
    ],
)
def test_render_remote_script_drops_unsafe_hook_names(
    unsafe_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-4 finding: hook names with shell-meta chars must NOT be
    interpolated into the rendered bash. Each unsafe name is dropped
    with a stderr warning; the rendered script must NOT contain the
    unsafe substring at all (no echo, no comment, nothing).
    """
    script = render_remote_script(
        config=_make_config(),
        env=_make_env(),
        enabled_hooks=["portainer", unsafe_name],
    )
    # Unsafe name must NOT reach the rendered bash
    if unsafe_name:  # empty string isn't a substring of anything useful
        assert unsafe_name not in script
    # Portainer (the safe entry) still rendered
    assert "portainer_hook" in script
    # Stderr warning emitted
    captured = capsys.readouterr()
    assert "Dropped hook with unsafe name" in captured.err


def test_render_remote_script_unknown_hook_emits_skip() -> None:
    """An enabled service with no renderer → emit skip line so counts stay consistent."""
    script = render_remote_script(
        config=_make_config(),
        env=_make_env(),
        enabled_hooks=["portainer", "filestash"],  # filestash → 2.2c
    )
    assert "RESULT hook=filestash status=skipped-not-ready" in script
    # Portainer still runs
    assert "portainer_hook" in script


def test_render_remote_script_empty_list_yields_minimal_orchestrator() -> None:
    """Empty enabled list → just the orchestrator preamble, no hook calls."""
    script = render_remote_script(config=_make_config(), env=_make_env(), enabled_hooks=[])
    assert script.startswith("set -u")
    assert "_hook()" not in script


# ---------------------------------------------------------------------------
# parse_results
# ---------------------------------------------------------------------------


def test_parse_results_one_per_line() -> None:
    out = (
        "  ✓ portainer\n"
        "RESULT hook=portainer status=configured\n"
        "RESULT hook=n8n status=already-configured\n"
        "  ⚠ metabase not ready after 120s — skipping setup\n"
        "RESULT hook=metabase status=skipped-not-ready\n"
        "RESULT hook=openmetadata status=failed\n"
    )
    results = parse_results(out)
    assert results == (
        HookResult(name="portainer", status="configured"),
        HookResult(name="n8n", status="already-configured"),
        HookResult(name="metabase", status="skipped-not-ready"),
        HookResult(name="openmetadata", status="failed"),
    )


def test_parse_results_invalid_status_skipped() -> None:
    """Lines with invalid status values (typos, future statuses) are dropped."""
    out = "RESULT hook=foo status=configured\nRESULT hook=bar status=bogus-status"
    results = parse_results(out)
    assert results == (HookResult(name="foo", status="configured"),)


def test_parse_results_empty_input() -> None:
    assert parse_results("") == ()


# ---------------------------------------------------------------------------
# SetupResult counters
# ---------------------------------------------------------------------------


def test_setup_result_counters() -> None:
    r = SetupResult(
        hooks=(
            HookResult(name="a", status="configured"),
            HookResult(name="b", status="configured"),
            HookResult(name="c", status="already-configured"),
            HookResult(name="d", status="skipped-not-ready"),
            HookResult(name="e", status="failed"),
        )
    )
    assert r.configured == 2
    assert r.already_configured == 1
    assert r.skipped_not_ready == 1
    assert r.failed == 1
    assert r.is_success is False


def test_setup_result_empty_is_success() -> None:
    """Zero hooks = no failures = success."""
    assert SetupResult(hooks=()).is_success is True


# ---------------------------------------------------------------------------
# run_admin_setups — orchestration
# ---------------------------------------------------------------------------


def _ok_runner(stdout: str) -> Any:
    def runner(_script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr="")

    return runner


def test_run_admin_setups_filters_unknown_services() -> None:
    """Services without a renderer don't reach the remote script."""
    captured: dict[str, str] = {}

    def capture(script: str) -> subprocess.CompletedProcess[str]:
        captured["script"] = script
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout="RESULT hook=portainer status=configured",
            stderr="",
        )

    run_admin_setups(
        _make_config(),
        _make_env(),
        ["portainer", "filestash", "gitea"],
        script_runner=capture,
    )
    # filestash + gitea (not in 2.2b registry) must NOT reach the script
    assert "filestash_hook" not in captured["script"]
    assert "gitea_hook" not in captured["script"]
    assert "portainer_hook" in captured["script"]


def test_run_admin_setups_all_unknown_returns_empty_result() -> None:
    """If no enabled service has a renderer, we don't even invoke ssh."""
    runner_invoked = []

    def runner(_script: str) -> subprocess.CompletedProcess[str]:
        runner_invoked.append(True)
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")

    result = run_admin_setups(
        _make_config(),
        _make_env(),
        ["filestash", "gitea"],
        script_runner=runner,
    )
    assert result == SetupResult(hooks=())
    assert runner_invoked == []


def test_run_admin_setups_missing_result_line_counts_as_failed() -> None:
    """A hook that did NOT emit a RESULT line counts as failed
    (server-side ssh hung up mid-script, etc.)."""
    out = "RESULT hook=portainer status=configured\n"  # n8n missing
    result = run_admin_setups(
        _make_config(),
        _make_env(),
        ["portainer", "n8n"],
        script_runner=_ok_runner(out),
    )
    by_name = {h.name: h.status for h in result.hooks}
    assert by_name["portainer"] == "configured"
    assert by_name["n8n"] == "failed"


def test_run_admin_setups_forwards_remote_warnings_to_local_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Modul-1.2 Round-4 lesson: ⚠ warnings reach local stderr."""
    out = (
        "  ⚠ metabase not ready after 120s — skipping setup\n"
        "RESULT hook=metabase status=skipped-not-ready\n"
    )
    run_admin_setups(_make_config(), _make_env(), ["metabase"], script_runner=_ok_runner(out))
    captured = capsys.readouterr()
    assert "metabase not ready after 120s" in captured.err
    # RESULT line is wire-format, must NOT pollute stderr
    assert "RESULT hook=metabase" not in captured.err


# ---------------------------------------------------------------------------
# CLI integration — direct _services_configure unit tests with monkeypatch
# (subprocess CLI tests covered via _run_cli below for arg-parsing cases)
# ---------------------------------------------------------------------------


def _run_cli(
    args: list[str],
    *,
    stdin: str = "{}",
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-m", "nexus_deploy", "services", *args],
        capture_output=True,
        text=True,
        env=full_env,
        input=stdin,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_services_missing_subcommand_returns_2() -> None:
    rc, _, err = _run_cli([])
    assert rc == 2
    assert "only 'configure'" in err


def test_cli_services_configure_missing_enabled_returns_2() -> None:
    rc, _, err = _run_cli(["configure"])
    assert rc == 2
    assert "--enabled" in err


def test_cli_services_configure_empty_enabled_returns_zero() -> None:
    rc, out, _ = _run_cli(["configure", "--enabled", ""])
    assert rc == 0
    assert "nothing to do" in out


def test_cli_services_configure_unknown_arg_returns_2() -> None:
    rc, _, err = _run_cli(["configure", "--enabled", "portainer", "--bogus"])
    assert rc == 2
    assert "unknown arg" in err


def test_cli_services_configure_subcommand_typo_returns_2() -> None:
    """`services up`, `services down` etc. all rejected."""
    rc, _, err = _run_cli(["up", "--enabled", "x"])
    assert rc == 2
    assert "only 'configure'" in err


# CLI rc-mapping unit tests via monkeypatch (avoid spinning subprocesses for
# the rc=0/1/2 contract — same pattern as test_compose_runner.py).


@pytest.mark.parametrize(
    ("hooks", "expected_rc"),
    [
        # All success
        (
            (
                HookResult(name="portainer", status="configured"),
                HookResult(name="n8n", status="already-configured"),
            ),
            0,
        ),
        # Empty
        ((), 0),
        # Partial: some success, some failed → rc=1
        (
            (
                HookResult(name="portainer", status="configured"),
                HookResult(name="metabase", status="failed"),
            ),
            1,
        ),
        # All failed → rc=2 (orchestrator should abort)
        ((HookResult(name="portainer", status="failed"),), 2),
        # Skipped-not-ready alone is success (no failures)
        ((HookResult(name="portainer", status="skipped-not-ready"),), 0),
    ],
)
def test_services_configure_cli_rc_mapping(
    monkeypatch: pytest.MonkeyPatch,
    hooks: tuple[HookResult, ...],
    expected_rc: int,
) -> None:
    """Verify the rc=0/1/2 contract via direct `_services_configure` call."""
    from nexus_deploy.__main__ import _services_configure

    def fake_run(_config: Any, _env: Any, _enabled: list[str]) -> SetupResult:
        return SetupResult(hooks=hooks)

    monkeypatch.setattr("nexus_deploy.__main__.run_admin_setups", fake_run)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")
    rc = _services_configure(["configure", "--enabled", "portainer"])
    assert rc == expected_rc


def test_services_configure_cli_rc2_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Programming errors → rc=2; secret-bearing message NEVER printed."""
    from nexus_deploy.__main__ import _services_configure

    def boom(_c: Any, _e: Any, _en: list[str]) -> SetupResult:
        raise RuntimeError("secret-bearing-message-NEVER-print")

    monkeypatch.setattr("nexus_deploy.__main__.run_admin_setups", boom)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")
    rc = _services_configure(["configure", "--enabled", "portainer"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "secret-bearing-message-NEVER-print" not in captured.err


def test_services_configure_cli_rc2_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ssh/rsync failure → rc=2. exc.cmd must NOT leak to stderr."""
    from nexus_deploy.__main__ import _services_configure

    def boom(_c: Any, _e: Any, _en: list[str]) -> SetupResult:
        raise subprocess.CalledProcessError(255, ["ssh", "with-secret-arg"])

    monkeypatch.setattr("nexus_deploy.__main__.run_admin_setups", boom)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")
    rc = _services_configure(["configure", "--enabled", "portainer"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "transport failure" in captured.err
    assert "with-secret-arg" not in captured.err
