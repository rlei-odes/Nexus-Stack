"""Tests for nexus_deploy.gitea — Phase 2 Modul 2.2e (#505).

Covers the 8 named regression tests (R1-R8) per Appendix D plus
orthogonal CLI/branch tests:

- R1 column-exact awk match on user existence (PR #464 bug class)
- R2 legacy email-collision PATCH
- R3 DB password sync retry loop
- R4 token retry-via-delete on conflict
- R5 path-safety regex on URL segments
- R6 repo-create-409 → patch_repo_private fallback
- R7 token never in argv / URL (only in Authorization header)
- R8 stdout emits eval-able GITEA_TOKEN= and RESTART_SERVICES=

Mocks: ``responses`` for REST, ``unittest.mock.MagicMock`` for SSH.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import responses

from nexus_deploy.config import NexusConfig
from nexus_deploy.gitea import (
    CreateRepoResult,
    CreateUserResult,
    GiteaCli,
    GiteaClient,
    GiteaError,
    GiteaResult,
    _compute_restart_services,
    _escape_sql_string_literal,
    _parse_admin_list_for_user,
    _render_db_pw_sync_script,
    _validate_path_segment,
    run_configure_gitea,
)

BASE_URL = "http://localhost:3300"
ADMIN = "admin"
ADMIN_PASSWORD = "p@ss-w0rd!"


def _make_config(**overrides: Any) -> NexusConfig:
    defaults: dict[str, Any] = {
        "admin_username": ADMIN,
        "gitea_admin_password": ADMIN_PASSWORD,
        "gitea_db_password": "db-secret",
    }
    defaults.update(overrides)
    return NexusConfig.from_secrets_json(json.dumps(defaults))


def _make_ssh(stdouts: list[str | tuple[int, str]] | None = None) -> MagicMock:
    """Build a MagicMock SSH that returns the given stdouts in order.

    Each stdout entry is either a string (rc=0) or (rc, stdout).
    Once exhausted, returns rc=0 with empty stdout.
    """
    queue: list[tuple[int, str]] = []
    for entry in stdouts or []:
        if isinstance(entry, tuple):
            queue.append(entry)
        else:
            queue.append((0, entry))

    def run_script(
        _script: str, *, check: bool = False, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if queue:
            rc, out = queue.pop(0)
        else:
            rc, out = 0, ""
        return subprocess.CompletedProcess(args=["ssh"], returncode=rc, stdout=out, stderr="")

    ssh = MagicMock()
    ssh.run_script.side_effect = run_script
    return ssh


# ---------------------------------------------------------------------------
# R5 — path safety regex (CRITICAL — directory-traversal class)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "admin",
        "stefan.koch",  # dotted username — Gitea allows
        "user_42",
        "a-b-c",
        "Stefan",
    ],
)
def test_round_5_path_safety_accepts_valid(value: str) -> None:
    _validate_path_segment(value, kind="username")  # no raise


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "..",  # traversal
        "../etc/passwd",
        "user/admin",  # slash
        "user;rm -rf",  # shell meta
        "user`whoami`",
        "user$VAR",
        "user with space",
        "user'quote",
        'user"quote',
        "user\nnewline",
        "user@host",  # @ not allowed (emails go elsewhere)
    ],
)
def test_round_5_path_safety_rejects_unsafe(value: str) -> None:
    with pytest.raises(GiteaError, match="unsafe"):
        _validate_path_segment(value, kind="username")


# ---------------------------------------------------------------------------
# R1 — column-exact admin-list parser (PR #464 bug class)
# ---------------------------------------------------------------------------


_ADMIN_LIST_FIXTURE = (
    "ID    Username       Email                          FullName\n"
    "1     admin          admin@example.com              Admin\n"
    "2     stefan.koch    stefan.koch@hslu.ch            Stefan\n"
)


def test_round_1_column_exact_match_finds_user() -> None:
    exists, email = _parse_admin_list_for_user(_ADMIN_LIST_FIXTURE, "stefan.koch")
    assert exists is True
    assert email == "stefan.koch@hslu.ch"


def test_round_1_column_exact_does_not_substring_match_email_column() -> None:
    """The PR #464 bug: substring-grep matched 'koch' in admin's email column.

    With column-exact awk equivalent, a username 'koch' must NOT
    match 'stefan.koch' (column 2) and must NOT match 'stefan.koch@hslu.ch'
    (column 3, even though it contains the substring).
    """
    exists, _ = _parse_admin_list_for_user(_ADMIN_LIST_FIXTURE, "koch")
    assert exists is False


def test_round_1_column_exact_does_not_substring_match_other_username() -> None:
    """'admi' must not match 'admin'."""
    exists, _ = _parse_admin_list_for_user(_ADMIN_LIST_FIXTURE, "admi")
    assert exists is False


def test_parse_empty_list_returns_false() -> None:
    assert _parse_admin_list_for_user("", "admin") == (False, None)


def test_parse_only_header_returns_false() -> None:
    assert _parse_admin_list_for_user("ID Username Email\n", "admin") == (False, None)


def test_parse_handles_short_lines_gracefully() -> None:
    """Malformed lines (whitespace-only, <2 columns) must not crash the parser.

    Line shapes:
    - ``  `` (whitespace only) → split() = [] → skipped
    - ``5 someuser`` (2 cols, no email) → matches by column 2,
      email returned as None
    """
    text = "ID Username Email\n  \n5 someuser\n"
    exists, email = _parse_admin_list_for_user(text, "someuser")
    assert exists is True
    # email column missing → None
    assert email is None


# ---------------------------------------------------------------------------
# SQL escape
# ---------------------------------------------------------------------------


def test_sql_escape_handles_backslash_first() -> None:
    """Backslash MUST be doubled BEFORE single-quote — order matters."""
    assert _escape_sql_string_literal("a\\b") == "a\\\\b"
    assert _escape_sql_string_literal("a'b") == "a''b"
    # Combination: \' should NOT become \\\\\\' (which would close+open) —
    # it should become \\\\\'\' i.e. backslash doubled then quote doubled.
    # Result: a\\\\\'\'b   (4 chars source → "a", "\\\\", "''", "b")
    assert _escape_sql_string_literal("a\\'b") == "a\\\\''b"


def test_sql_escape_passthrough_safe_chars() -> None:
    assert _escape_sql_string_literal("simple-pw_42") == "simple-pw_42"


# ---------------------------------------------------------------------------
# DB sync render + R3 retry loop
# ---------------------------------------------------------------------------


def test_render_db_sync_script_contains_set_euo_pipefail() -> None:
    script = _render_db_pw_sync_script("escaped", attempts=3, interval_s=1.0)
    assert script.splitlines()[0] == "set -euo pipefail"


def test_render_db_sync_script_uses_peer_auth() -> None:
    """No -W, no PGPASSWORD — peer auth via -U nexus-gitea inside container."""
    script = _render_db_pw_sync_script("xx", attempts=3, interval_s=1.0)
    assert "-U nexus-gitea" in script
    assert "PGPASSWORD" not in script
    assert " -W " not in script


def test_render_db_sync_script_parses_as_valid_bash() -> None:
    """``bash -n`` must accept the rendered script (R1 defence-in-depth).

    Static-text tests caught the Modul-2.0 multi-line skip bug only
    because we also exec'd bash. For the DB-sync script the surface
    is small enough that ``bash -n`` (parse-only) is sufficient.
    """
    script = _render_db_pw_sync_script("p''q\\\\quoted", attempts=15, interval_s=2.0)
    result = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_render_db_sync_script_shlex_protects_against_password_with_quotes() -> None:
    """The SQL string MUST be shlex-quoted for the bash command.

    A password containing a single quote (post-SQL-escape: ``''``)
    must not break out of bash quoting. shlex.quote wraps the whole
    SQL string in single quotes and escapes any internal single
    quote as ``'\\''`` — so the literal ``''`` is transformed by
    shlex but the bash-quoted form remains a single argument.

    Verification: the rendered script must NOT contain an unbalanced
    quoting pattern like ``'p''q'`` (which bash would parse as
    ``'p' '' 'q'`` — three separate args, breaking psql's ``-c``).
    """
    escaped = _escape_sql_string_literal("p'q")  # → "p''q"
    script = _render_db_pw_sync_script(escaped, attempts=3, interval_s=1.0)
    # Should contain the SHLEX-escaped form, not the raw '' literal.
    # shlex.quote of a string with single quotes uses '"'"' (or '\'') to
    # escape, so the unsafe `'p''q'` form must NOT appear.
    assert "'p''q'" not in script
    # Must contain the unique substrings once shlex unwraps it.
    assert "p" in script
    assert "q" in script


def test_round_3_db_sync_succeeds_after_retries() -> None:
    """rc=0 + RESULT line on first or later attempt → True."""
    ssh = _make_ssh([(0, "RESULT db_pw=synced\n")])
    cli = GiteaCli(ssh)
    assert cli.sync_db_password("secret", attempts=3, interval_s=0.01) is True
    ssh.run_script.assert_called_once()


def test_round_3_db_sync_fails_after_all_retries() -> None:
    """rc=1 after exhausting attempts → False, no exception."""
    ssh = _make_ssh([(1, "RESULT db_pw=failed\n")])
    cli = GiteaCli(ssh)
    assert cli.sync_db_password("secret", attempts=3, interval_s=0.01) is False


def test_db_sync_skips_when_password_empty() -> None:
    """Empty password → no ssh call, returns False."""
    ssh = _make_ssh()
    cli = GiteaCli(ssh)
    assert cli.sync_db_password("", attempts=3, interval_s=0.01) is False
    ssh.run_script.assert_not_called()


def test_db_sync_password_never_in_argv_only_in_script_stdin() -> None:
    """R7 / defence-in-depth — password must reach SSH via run_script
    (stdin), not via run (argv). Verify by asserting `run` is never called.
    """
    ssh = _make_ssh([(0, "RESULT db_pw=synced\n")])
    cli = GiteaCli(ssh)
    cli.sync_db_password("supersecret-do-not-leak", attempts=2, interval_s=0.01)
    # MagicMock.run is NOT called
    ssh.run.assert_not_called()
    # The script (which contains the password) was passed as the first
    # positional arg to run_script.
    call_script = ssh.run_script.call_args[0][0]
    assert "supersecret-do-not-leak" in call_script


# ---------------------------------------------------------------------------
# GiteaCli — admin list + create + sync
# ---------------------------------------------------------------------------


def test_list_admin_users_returns_stdout_on_success() -> None:
    ssh = _make_ssh([(0, _ADMIN_LIST_FIXTURE)])
    assert GiteaCli(ssh).list_admin_users() == _ADMIN_LIST_FIXTURE


def test_list_admin_users_returns_empty_on_ssh_failure() -> None:
    """Non-zero rc → empty string (caller routes to CREATE branch)."""
    ssh = _make_ssh([(1, "boom\n")])
    assert GiteaCli(ssh).list_admin_users() == ""


def test_create_admin_returns_created_on_success_keyword() -> None:
    ssh = _make_ssh([(0, "New user 'admin' has been created\n")])
    result = GiteaCli(ssh).create_admin("admin", "pw", "a@b.c")
    assert result.status == "created"


def test_create_admin_returns_already_exists_on_collision() -> None:
    ssh = _make_ssh([(1, "user already exists\n")])
    result = GiteaCli(ssh).create_admin("admin", "pw", "a@b.c")
    assert result.status == "already_exists"


def test_create_admin_returns_failed_on_other_error() -> None:
    ssh = _make_ssh([(1, "Some other validation error\n")])
    result = GiteaCli(ssh).create_admin("admin", "pw", "a@b.c")
    assert result.status == "failed"
    assert "Some other validation error" in result.detail


def test_create_admin_path_safety() -> None:
    ssh = _make_ssh([(0, "")])
    with pytest.raises(GiteaError, match="unsafe"):
        GiteaCli(ssh).create_admin("ad;min", "pw", "a@b.c")
    ssh.run_script.assert_not_called()


def test_sync_password_returns_synced_on_rc_zero() -> None:
    ssh = _make_ssh([(0, "")])
    assert GiteaCli(ssh).sync_password("admin", "newpw").status == "synced"


def test_sync_password_returns_failed_on_rc_nonzero() -> None:
    ssh = _make_ssh([(1, "user not found")])
    result = GiteaCli(ssh).sync_password("admin", "newpw")
    assert result.status == "failed"


def test_sync_password_uses_run_script_not_run() -> None:
    """R7 — password is in the rendered script, fed via stdin."""
    ssh = _make_ssh([(0, "")])
    GiteaCli(ssh).sync_password("admin", "leakable-pw")
    ssh.run.assert_not_called()
    assert "leakable-pw" in ssh.run_script.call_args[0][0]


# ---------------------------------------------------------------------------
# GiteaClient (REST) — basic-auth + token-auth
# ---------------------------------------------------------------------------


def _client(token: str | None = None) -> GiteaClient:
    base = GiteaClient(BASE_URL, admin_username=ADMIN, admin_password=ADMIN_PASSWORD)
    return base.with_token(token) if token else base


def test_client_rejects_empty_admin_username() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GiteaClient(BASE_URL, admin_username="", admin_password="pw")


def test_client_rejects_empty_admin_password() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GiteaClient(BASE_URL, admin_username="admin", admin_password="")


def test_client_rejects_unsafe_admin_username() -> None:
    with pytest.raises(GiteaError, match="unsafe"):
        GiteaClient(BASE_URL, admin_username="adm;in", admin_password="pw")


def test_with_token_rejects_empty_token() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _client().with_token("")


@responses.activate
def test_wait_ready_returns_true_on_200() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)
    assert _client().wait_ready(timeout_s=1.0, interval_s=0.05) is True


@responses.activate
def test_wait_ready_times_out() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=503)
    assert _client().wait_ready(timeout_s=0.2, interval_s=0.05) is False


# ---------------------------------------------------------------------------
# R2 — legacy email-collision PATCH
# ---------------------------------------------------------------------------


@responses.activate
def test_round_2_patch_user_email_returns_true_on_200() -> None:
    responses.add(
        responses.PATCH,
        f"{BASE_URL}/api/v1/admin/users/admin",
        status=200,
        json={"id": 1},
    )
    assert _client().patch_user_email("admin", "new@e.com", login_name="admin") is True
    body = json.loads(responses.calls[0].request.body)  # type: ignore[arg-type]
    assert body == {"email": "new@e.com", "source_id": 0, "login_name": "admin"}


@responses.activate
def test_round_2_patch_user_email_includes_required_full_body() -> None:
    """Schema requires source_id + login_name even for email-only update."""
    responses.add(responses.PATCH, f"{BASE_URL}/api/v1/admin/users/admin", status=200)
    _client().patch_user_email("admin", "x@y.z", login_name="admin")
    body = json.loads(responses.calls[0].request.body)  # type: ignore[arg-type]
    assert "source_id" in body
    assert "login_name" in body


@responses.activate
def test_patch_user_email_returns_false_on_4xx() -> None:
    responses.add(responses.PATCH, f"{BASE_URL}/api/v1/admin/users/admin", status=403)
    assert _client().patch_user_email("admin", "x@y.z", login_name="admin") is False


def test_patch_user_email_path_safety() -> None:
    with pytest.raises(GiteaError, match="unsafe"):
        _client().patch_user_email("admi;n", "x@y.z", login_name="admin")


# ---------------------------------------------------------------------------
# R4 — token create / retry-via-delete
# ---------------------------------------------------------------------------


def test_round_4_mint_token_returns_sha1_on_success() -> None:
    """Happy path: CLI returns "Access token was successfully created: <40-hex>"."""
    ssh = _make_ssh(
        [
            (0, ""),  # delete-access-token best-effort (rc=0 fine)
            (
                0,
                "Access token was successfully created: aebafa8bbcff4e5e7edde8dc89571df698648e7d\n",
            ),
        ]
    )
    sha1, err = GiteaCli(ssh).mint_token("admin", "nexus-automation")
    assert sha1 == "aebafa8bbcff4e5e7edde8dc89571df698648e7d"
    assert err == ""


def test_round_4_mint_token_idempotent_delete_first() -> None:
    """Token already exists → CLI delete succeeds → CLI generate succeeds.

    The legacy deploy.sh delete-then-create pattern is preserved via
    the unconditional delete in mint_token. CLI delete-access-token
    returns rc=0 if the token existed and rc!=0 (silenced via ``|| true``)
    if not. Either way, the next generate runs and returns the new sha1.
    """
    ssh = _make_ssh(
        [
            (1, ""),  # delete: token didn't exist (rc!=0, silenced)
            (
                0,
                "Access token was successfully created: 0000000000000000000000000000000000000001\n",
            ),
        ]
    )
    sha1, err = GiteaCli(ssh).mint_token("admin", "nexus-automation")
    assert sha1 == "0000000000000000000000000000000000000001"
    assert err == ""
    # Both delete + generate were called.
    assert ssh.run_script.call_count == 2


def test_mint_token_returns_diagnostic_on_cli_failure() -> None:
    """Generate fails (non-zero rc) → returns (None, diagnostic) — no crash.

    Regression test for the post-#519 silent-fail bug class: previously
    a GiteaError was caught silently with ``token = None`` and no stderr
    diagnostic, making the spin-up failure undebuggable. Now the
    diagnostic is captured and surfaced via stderr by the CLI handler.
    """
    ssh = _make_ssh(
        [
            (0, ""),  # delete OK
            (1, "User does not exist [name: admin]\n"),  # generate fails
        ]
    )
    sha1, err = GiteaCli(ssh).mint_token("admin", "nexus-automation")
    assert sha1 is None
    assert "rc=1" in err
    assert "User does not exist" in err


def test_mint_token_returns_diagnostic_on_unparseable_output() -> None:
    """rc=0 but no sha1 in output → still surfaces a diagnostic."""
    ssh = _make_ssh(
        [
            (0, ""),
            (0, "weird unexpected success output\n"),  # no 40-hex
        ]
    )
    sha1, err = GiteaCli(ssh).mint_token("admin", "nexus-automation")
    assert sha1 is None
    assert "no sha1" in err.lower()


def test_mint_token_path_safety_on_username() -> None:
    ssh = _make_ssh()
    with pytest.raises(GiteaError, match="unsafe"):
        GiteaCli(ssh).mint_token("admin;rm -rf /", "nexus-automation")
    ssh.run_script.assert_not_called()


def test_mint_token_path_safety_on_token_name() -> None:
    ssh = _make_ssh()
    with pytest.raises(GiteaError, match="unsafe"):
        GiteaCli(ssh).mint_token("admin", "evil; name")
    ssh.run_script.assert_not_called()


def test_mint_token_uses_run_script_not_run() -> None:
    """R7 — peer-auth CLI commands feed via stdin to ssh.run_script,
    NOT argv. No password is involved (peer auth), so the secrecy
    surface is the username + token-name, both of which are
    already non-secret values per the path-safety contract.
    """
    ssh = _make_ssh(
        [
            (0, ""),
            (0, "Access token was successfully created: " + "f" * 40 + "\n"),
        ]
    )
    GiteaCli(ssh).mint_token("admin", "nexus-automation")
    # Never use ssh.run (argv form)
    ssh.run.assert_not_called()


def test_mint_token_supports_custom_scopes() -> None:
    """Default scopes='all' is preserved, but caller can pass alternatives."""
    ssh = _make_ssh(
        [
            (0, ""),
            (0, "Access token was successfully created: " + "a" * 40 + "\n"),
        ]
    )
    GiteaCli(ssh).mint_token("admin", "nexus-automation", scopes="write:repository")
    # Inspect the rendered generate script — last call to run_script
    last_script = ssh.run_script.call_args[0][0]
    assert "write:repository" in last_script


# ---------------------------------------------------------------------------
# R7 — token never in argv / URL, only Authorization header
# ---------------------------------------------------------------------------


@responses.activate
def test_round_7_token_in_authorization_header_not_argv_or_url() -> None:
    """After ``with_token``, every request carries
    ``Authorization: token <sha>`` and the token MUST NOT appear in
    the URL or in the request body.
    """
    secret_token = "do-not-leak-this-token-anywhere-please"
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/user/repos",
        status=201,
        json={"id": 1},
    )
    client = _client(token=secret_token)
    client.create_repo("nexus-test-gitea", description="Hello")

    call = responses.calls[0]
    # URL: token NOT present
    assert secret_token not in (call.request.url or "")
    # Body: token NOT present
    raw_body = call.request.body
    if isinstance(raw_body, bytes):
        body_text = raw_body.decode("utf-8")
    elif isinstance(raw_body, str):
        body_text = raw_body
    else:
        body_text = ""
    assert secret_token not in body_text
    # Authorization header: token IS present in `token <sha>` form
    auth = call.request.headers.get("Authorization", "")
    assert auth == f"token {secret_token}"


@responses.activate
def test_round_7_token_not_in_basic_auth_after_with_token() -> None:
    """After ``with_token``, the basic-auth credentials MUST be gone."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/repos/admin/foo", status=200)
    client = _client(token="some-token")
    client.repo_exists("admin", "foo")
    auth_header = responses.calls[0].request.headers.get("Authorization", "")
    # Must NOT be Basic ... (would be base64 of admin:password)
    assert auth_header.startswith("token ")


# ---------------------------------------------------------------------------
# R6 — repo create 409 → patch_repo_private fallback
# ---------------------------------------------------------------------------


@responses.activate
def test_create_repo_returns_created_on_201() -> None:
    responses.add(responses.POST, f"{BASE_URL}/api/v1/user/repos", status=201, json={"id": 1})
    result = _client(token="t").create_repo("nexus-test", description="x")
    assert result.status == "created"


@responses.activate
def test_round_6_create_repo_409_returns_already_exists() -> None:
    responses.add(responses.POST, f"{BASE_URL}/api/v1/user/repos", status=409)
    result = _client(token="t").create_repo("nexus-test")
    assert result.status == "already_exists"


@responses.activate
def test_round_6_create_repo_422_already_exists_returns_already_exists() -> None:
    """Some Gitea modes return 422 with 'already exists' body."""
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/user/repos",
        status=422,
        json={"message": "repository already exists"},
    )
    result = _client(token="t").create_repo("nexus-test")
    assert result.status == "already_exists"


@responses.activate
def test_create_repo_422_validation_returns_failed() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/user/repos",
        status=422,
        json={"message": "invalid name"},
    )
    result = _client(token="t").create_repo("nexus-test")
    assert result.status == "failed"


@responses.activate
def test_round_6_patch_repo_private_idempotent_204_or_200() -> None:
    """Used after 409 to ensure the existing repo is private."""
    responses.add(
        responses.PATCH,
        f"{BASE_URL}/api/v1/repos/admin/nexus-test",
        status=200,
    )
    assert _client(token="t").patch_repo_private("admin", "nexus-test", private=True)
    body = json.loads(responses.calls[0].request.body)  # type: ignore[arg-type]
    assert body == {"private": True}


# ---------------------------------------------------------------------------
# Collaborator add — idempotent
# ---------------------------------------------------------------------------


@responses.activate
def test_add_collaborator_returns_true_on_204() -> None:
    responses.add(
        responses.PUT,
        f"{BASE_URL}/api/v1/repos/admin/nexus-test/collaborators/user",
        status=204,
    )
    assert _client(token="t").add_collaborator("admin", "nexus-test", "user")


@responses.activate
def test_add_collaborator_returns_true_on_422_already_collaborator() -> None:
    """Idempotent: 422 ('already a collaborator') counted as success."""
    responses.add(
        responses.PUT,
        f"{BASE_URL}/api/v1/repos/admin/nexus-test/collaborators/user",
        status=422,
    )
    assert _client(token="t").add_collaborator("admin", "nexus-test", "user")


@responses.activate
def test_add_collaborator_returns_false_on_403() -> None:
    responses.add(
        responses.PUT,
        f"{BASE_URL}/api/v1/repos/admin/nexus-test/collaborators/user",
        status=403,
    )
    assert _client(token="t").add_collaborator("admin", "nexus-test", "user") is False


def test_add_collaborator_path_safety() -> None:
    with pytest.raises(GiteaError, match="unsafe"):
        _client(token="t").add_collaborator("admin", "nexus-test", "user;rm")


# ---------------------------------------------------------------------------
# repo_exists
# ---------------------------------------------------------------------------


@responses.activate
def test_repo_exists_200() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/v1/repos/a/b", status=200)
    assert _client(token="t").repo_exists("a", "b") is True


@responses.activate
def test_repo_exists_404() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/v1/repos/a/b", status=404)
    assert _client(token="t").repo_exists("a", "b") is False


@responses.activate
def test_repo_exists_500_raises() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/v1/repos/a/b", status=500)
    with pytest.raises(GiteaError):
        _client(token="t").repo_exists("a", "b")


# ---------------------------------------------------------------------------
# RESTART_SERVICES intersection
# ---------------------------------------------------------------------------


def test_compute_restart_services_preserves_order() -> None:
    """Output order must be the canonical order (jupyter, marimo, code-server, ...)."""
    enabled = ["redpanda", "code-server", "jupyter", "marimo"]
    assert _compute_restart_services(enabled) == ("jupyter", "marimo", "code-server")


def test_compute_restart_services_empty_when_none_enabled() -> None:
    assert _compute_restart_services(["postgres", "redis"]) == ()


def test_compute_restart_services_empty_input() -> None:
    assert _compute_restart_services([]) == ()


# ---------------------------------------------------------------------------
# Top-level orchestrator — happy path + key branches
# ---------------------------------------------------------------------------


@responses.activate
def test_run_configure_gitea_full_happy_path_admin_already_exists() -> None:
    """Admin exists, password sync, token mint via CLI, repo+collaborator add."""
    # Healthcheck OK
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)
    # Repo create
    responses.add(responses.POST, f"{BASE_URL}/api/v1/user/repos", status=201, json={"id": 1})
    # Collaborator add
    responses.add(
        responses.PUT,
        f"{BASE_URL}/api/v1/repos/admin/nexus-foo/collaborators/stefan.koch",
        status=204,
    )

    ssh = _make_ssh(
        [
            (0, "RESULT db_pw=synced\n"),  # DB pw sync
            (0, _ADMIN_LIST_FIXTURE),  # admin user list
            (0, ""),  # admin sync_password
            (0, _ADMIN_LIST_FIXTURE),  # user list (same fixture, has stefan.koch)
            (0, ""),  # user sync_password
            (0, ""),  # token: delete-access-token (best-effort)
            (0, f"Access token was successfully created: {'a' * 40}\n"),  # token: generate
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="admin@example.com",
        gitea_user_email="stefan.koch@hslu.ch",
        gitea_user_password="userpw",
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=False,
        enabled_services=["jupyter", "marimo"],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    assert result.is_success is True
    assert result.token == "a" * 40
    assert result.db_pw_synced is True
    assert result.admin.status == "synced"
    assert result.user is not None
    assert result.user.status == "synced"
    assert result.repo is not None
    assert result.repo.status == "created"
    assert result.collaborator_added is True
    assert result.restart_services == ("jupyter", "marimo")


@responses.activate
def test_run_configure_gitea_admin_does_not_exist_creates() -> None:
    """Empty admin list → CREATE branch."""
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/user/repos", status=201, json={"id": 1})

    ssh = _make_ssh(
        [
            (0, "RESULT db_pw=synced\n"),
            (0, "ID Username Email\n"),  # empty list
            (0, "New user 'admin' has been created\n"),  # create_admin
            (0, ""),  # token: delete-access-token
            (0, f"Access token was successfully created: {'b' * 40}\n"),  # token: generate
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="admin@example.com",
        gitea_user_email=None,
        gitea_user_password=None,
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=False,
        enabled_services=[],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    assert result.admin.status == "created"
    assert result.user is None  # GITEA_USER_EMAIL was None
    assert result.token == "b" * 40


@responses.activate
def test_create_admin_already_exists_falls_back_to_sync_password() -> None:
    """Defence in depth: if list_admin_users returns empty (false negative —
    e.g. transient ssh+docker exec failure), CREATE runs and may report
    "already exists" from Gitea. Without a follow-up sync, the admin
    password drift stays and the subsequent REST token mint 401s.
    The orchestrator now falls back to sync_password automatically.

    Regression test for Copilot round 1.
    """
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)

    ssh = _make_ssh(
        [
            (0, "RESULT db_pw=synced\n"),
            (0, ""),  # admin list — empty (false negative)
            (0, "user already exists\n"),  # create_admin → already_exists
            (0, ""),  # FALLBACK: sync_password runs and succeeds
            (0, ""),  # token: delete-access-token
            (0, f"Access token was successfully created: {'c' * 40}\n"),  # token: generate
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="a@b.c",
        gitea_user_email=None,
        gitea_user_password=None,
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=True,  # skip repo to keep test focused
        enabled_services=[],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    # Final admin status must be "synced" (not "already_exists") —
    # the fallback ran and the result was overwritten.
    assert result.admin.status == "synced"
    # 6 ssh.run_script calls: db_sync, list, create, sync_password (fallback),
    # delete-access-token (best-effort), generate-access-token
    assert ssh.run_script.call_count == 6


@responses.activate
def test_create_user_already_exists_falls_back_to_sync_password() -> None:
    """Same fallback as admin — protects against the false-negative
    list path for the regular user (Copilot round 1).
    """
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)

    ssh = _make_ssh(
        [
            (0, "RESULT db_pw=synced\n"),
            (0, _ADMIN_LIST_FIXTURE),  # admin exists
            (0, ""),  # admin sync_password
            (0, ""),  # user list — empty (false negative)
            (0, "user already exists\n"),  # create_user → already_exists
            (0, ""),  # FALLBACK: sync_password
            (0, ""),  # token: delete-access-token
            (0, f"Access token was successfully created: {'d' * 40}\n"),  # generate
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="a@b.c",
        gitea_user_email="stefan.koch@hslu.ch",
        gitea_user_password="userpw",
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=True,
        enabled_services=[],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    assert result.user is not None
    assert result.user.status == "synced"


@responses.activate
def test_round_2_legacy_email_collision_triggers_patch() -> None:
    """Admin row's email == GITEA_USER_EMAIL → PATCH fires before sync."""
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)
    # The PATCH call we want to verify
    responses.add(
        responses.PATCH,
        f"{BASE_URL}/api/v1/admin/users/admin",
        status=200,
    )
    responses.add(responses.POST, f"{BASE_URL}/api/v1/user/repos", status=201, json={"id": 1})
    responses.add(
        responses.PUT,
        f"{BASE_URL}/api/v1/repos/admin/nexus-foo/collaborators/stefan.koch",
        status=204,
    )

    # admin's email column == GITEA_USER_EMAIL == "stefan.koch@hslu.ch"
    legacy_admin_list = "ID Username Email FullName\n1 admin stefan.koch@hslu.ch Admin\n"
    ssh = _make_ssh(
        [
            (0, ""),  # DB pw sync (not interesting here)
            (0, legacy_admin_list),  # admin list — collision
            (0, ""),  # admin sync_password
            (0, "ID Username Email\n"),  # user list — empty
            (0, "New user 'stefan.koch' has been created\n"),  # create_user
            (0, ""),  # token: delete-access-token
            (0, f"Access token was successfully created: {'e' * 40}\n"),  # generate
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="admin@new-domain.com",
        gitea_user_email="stefan.koch@hslu.ch",
        gitea_user_password="userpw",
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=False,
        enabled_services=["jupyter"],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    assert result.is_success is True
    # Verify a PATCH was made with the new admin email
    patch_calls = [c for c in responses.calls if c.request.method == "PATCH"]
    assert len(patch_calls) == 1
    body = json.loads(patch_calls[0].request.body)  # type: ignore[arg-type]
    assert body["email"] == "admin@new-domain.com"


@responses.activate
def test_round_6_repo_already_exists_falls_back_to_patch_private() -> None:
    """409 on POST → PATCH /repos/<o>/<n> with private=True."""
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)
    # Repo create returns 409
    responses.add(responses.POST, f"{BASE_URL}/api/v1/user/repos", status=409)
    # PATCH private fallback
    responses.add(
        responses.PATCH,
        f"{BASE_URL}/api/v1/repos/admin/nexus-foo",
        status=200,
    )

    ssh = _make_ssh(
        [
            (0, "RESULT db_pw=synced\n"),
            (0, _ADMIN_LIST_FIXTURE),
            (0, ""),  # admin sync_password
            (0, ""),  # token: delete-access-token
            (0, f"Access token was successfully created: {'1' * 40}\n"),  # generate
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="a@b.c",
        gitea_user_email=None,
        gitea_user_password=None,
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=False,
        enabled_services=[],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    assert result.repo is not None
    assert result.repo.status == "already_exists"
    # Verify PATCH was issued to set private=true
    patch_calls = [c for c in responses.calls if c.request.method == "PATCH"]
    assert len(patch_calls) == 1
    body = json.loads(patch_calls[0].request.body)  # type: ignore[arg-type]
    assert body["private"] is True


@responses.activate
def test_run_configure_gitea_mirror_mode_skips_repo_and_collaborator() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)

    ssh = _make_ssh(
        [
            (0, ""),  # db_pw_sync
            (0, _ADMIN_LIST_FIXTURE),  # admin list
            (0, ""),  # admin sync_password
            (0, ""),  # token: delete-access-token
            (0, f"Access token was successfully created: {'2' * 40}\n"),  # generate
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="a@b.c",
        gitea_user_email=None,
        gitea_user_password=None,
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=True,  # ← mirror mode
        enabled_services=[],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    assert result.repo is None
    assert result.collaborator_added is False
    # No POST to /api/v1/user/repos was made
    repo_calls = [c for c in responses.calls if "/user/repos" in (c.request.url or "")]
    assert len(repo_calls) == 0


@responses.activate
def test_run_configure_gitea_not_ready_returns_failed_admin() -> None:
    """Health endpoint never 200 → admin.status=='failed', no token.

    Uses a non-default admin_username so the regression test catches
    the Copilot-round-2 finding: the early-return path on health-check
    timeout previously hardcoded ``name="admin"`` even when the
    operator configured a different admin username.
    """
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=503)

    ssh = _make_ssh([(0, "RESULT db_pw=synced\n")])

    result = run_configure_gitea(
        _make_config(admin_username="custom-admin-name"),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="a@b.c",
        gitea_user_email=None,
        gitea_user_password=None,
        repo_name="nexus-foo",
        gitea_repo_owner="custom-admin-name",
        is_mirror_mode=False,
        enabled_services=["jupyter"],
        ready_timeout_s=0.2,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )

    assert result.is_success is False
    assert result.admin.status == "failed"
    # Round-2 regression: name must be the configured admin_username,
    # not the literal "admin".
    assert result.admin.name == "custom-admin-name"
    assert result.token is None
    assert result.restart_services == ("jupyter",)


@responses.activate
def test_run_configure_gitea_token_mint_failure_returns_failure() -> None:
    """Token CLI fails (rc=1) → token=None, is_success=False, token_error populated."""
    responses.add(responses.GET, f"{BASE_URL}/api/healthz", status=200)

    ssh = _make_ssh(
        [
            (0, "RESULT db_pw=synced\n"),
            (0, _ADMIN_LIST_FIXTURE),
            (0, ""),  # admin sync_password
            (0, ""),  # token: delete-access-token (best-effort)
            (1, "User does not exist [name: admin]\n"),  # token: generate fails
        ]
    )

    result = run_configure_gitea(
        _make_config(),
        base_url=BASE_URL,
        ssh=ssh,
        admin_email="a@b.c",
        gitea_user_email=None,
        gitea_user_password=None,
        repo_name="nexus-foo",
        gitea_repo_owner="admin",
        is_mirror_mode=False,
        enabled_services=[],
        ready_timeout_s=1.0,
        db_sync_attempts=1,
        db_sync_interval_s=0.01,
    )
    assert result.token is None
    assert result.is_success is False
    # Diagnostic must be populated so CLI handler can emit it to stderr —
    # the post-#519 silent-fail bug class.
    assert "rc=1" in result.token_error
    assert "User does not exist" in result.token_error


# ---------------------------------------------------------------------------
# is_success on GiteaResult
# ---------------------------------------------------------------------------


def test_is_success_true_on_clean_path() -> None:
    r = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="synced"),
        user=CreateUserResult(name="stefan", status="created"),
        token="t",
        token_error="",
        repo=CreateRepoResult(name="nexus-foo", status="created"),
        collaborator_added=True,
        restart_services=("jupyter",),
    )
    assert r.is_success is True


def test_is_success_false_when_admin_failed() -> None:
    r = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="failed"),
        user=None,
        token="t",
        token_error="",
        repo=None,
        collaborator_added=False,
        restart_services=(),
    )
    assert r.is_success is False


def test_is_success_false_when_token_missing() -> None:
    r = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="synced"),
        user=None,
        token=None,
        token_error="CLI rc=1: simulated failure",
        repo=None,
        collaborator_added=False,
        restart_services=(),
    )
    assert r.is_success is False


def test_is_success_false_when_user_failed() -> None:
    r = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="synced"),
        user=CreateUserResult(name="stefan", status="failed"),
        token="t",
        token_error="",
        repo=None,
        collaborator_added=False,
        restart_services=(),
    )
    assert r.is_success is False


def test_is_success_false_when_repo_failed() -> None:
    r = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="synced"),
        user=None,
        token="t",
        token_error="",
        repo=CreateRepoResult(name="nexus-foo", status="failed"),
        collaborator_added=False,
        restart_services=(),
    )
    assert r.is_success is False


# ---------------------------------------------------------------------------
# R8 — CLI stdout emits eval-able GITEA_TOKEN= AND RESTART_SERVICES=
# ---------------------------------------------------------------------------


def test_round_8_cli_emits_eval_able_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Happy-path stdout must contain BOTH eval-able lines and use shlex.quote."""
    fake_result = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="synced"),
        user=CreateUserResult(name="stefan", status="created"),
        token="abc123def-token",
        token_error="",
        repo=CreateRepoResult(name="nexus-foo", status="created"),
        collaborator_added=True,
        restart_services=("jupyter", "marimo"),
    )

    def fake_run(*_args: Any, **_kwargs: Any) -> GiteaResult:
        return fake_result

    monkeypatch.setattr("nexus_deploy.__main__.run_configure_gitea", fake_run)
    monkeypatch.setattr(
        "sys.stdin.read",
        lambda: json.dumps(
            {
                "admin_username": "admin",
                "gitea_admin_password": "x",
            }
        ),
    )
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("REPO_NAME", "nexus-foo")
    monkeypatch.setenv("GITEA_REPO_OWNER", "admin")
    monkeypatch.setenv("ENABLED_SERVICES", "jupyter,marimo")

    # Mock the SSH context-manager + port_forward so we don't actually ssh
    fake_ssh = MagicMock()
    fake_ssh.__enter__ = MagicMock(return_value=fake_ssh)
    fake_ssh.__exit__ = MagicMock(return_value=None)
    fake_pf_cm = MagicMock()
    fake_pf_cm.__enter__ = MagicMock(return_value=12345)
    fake_pf_cm.__exit__ = MagicMock(return_value=None)
    fake_ssh.port_forward = MagicMock(return_value=fake_pf_cm)
    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", lambda host: fake_ssh)

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 0

    captured = capsys.readouterr()
    out = captured.out
    # Both eval-able lines present
    assert re.search(r"^GITEA_TOKEN=.*abc123def-token", out, re.M)
    assert re.search(r"^RESTART_SERVICES=", out, re.M)
    # Token-line uses shlex-quoted form
    token_line = next(line for line in out.splitlines() if line.startswith("GITEA_TOKEN="))
    # Must be safely eval-able by bash; for a 40-hex-like value, no quotes
    # is acceptable; we only require the token substring is present.
    assert "abc123def-token" in token_line
    # Verify RESTART_SERVICES= encodes the comma-list
    rs_line = next(line for line in out.splitlines() if line.startswith("RESTART_SERVICES="))
    assert "jupyter,marimo" in rs_line


def test_round_8_cli_omits_token_line_when_token_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Token=None → only RESTART_SERVICES= on stdout, NO GITEA_TOKEN= line.

    deploy.sh must not see a stale token from a previous deploy
    leaking via empty-string assignment.
    """
    fake_result = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="synced"),
        user=None,
        token=None,
        token_error="CLI rc=1: simulated production failure",
        repo=None,
        collaborator_added=False,
        restart_services=("jupyter",),
    )

    monkeypatch.setattr("nexus_deploy.__main__.run_configure_gitea", lambda *a, **k: fake_result)
    monkeypatch.setattr("sys.stdin.read", lambda: '{"gitea_admin_password": "x"}')
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("REPO_NAME", "nexus-foo")
    monkeypatch.setenv("GITEA_REPO_OWNER", "admin")
    monkeypatch.setenv("ENABLED_SERVICES", "jupyter")

    fake_ssh = MagicMock()
    fake_ssh.__enter__ = MagicMock(return_value=fake_ssh)
    fake_ssh.__exit__ = MagicMock(return_value=None)
    fake_pf = MagicMock()
    fake_pf.__enter__ = MagicMock(return_value=12345)
    fake_pf.__exit__ = MagicMock(return_value=None)
    fake_ssh.port_forward = MagicMock(return_value=fake_pf)
    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", lambda host: fake_ssh)

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 1  # is_success=False (token is None)

    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    assert "GITEA_TOKEN=" not in out
    assert re.search(r"^RESTART_SERVICES=", out, re.M)
    # Diagnostic must reach stderr — post-#519 fix that closed the
    # "silent token-mint failure" debugging blind spot.
    assert "token: NOT minted" in err
    assert "CLI rc=1: simulated production failure" in err


# ---------------------------------------------------------------------------
# CLI argument validation — rc=2 on bad inputs
# ---------------------------------------------------------------------------


def test_cli_unknown_args_returns_rc_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure(["--bogus"])
    assert rc == 2
    assert "unknown args" in capsys.readouterr().err


def test_cli_missing_required_env_returns_rc_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("REPO_NAME", raising=False)
    monkeypatch.delenv("GITEA_REPO_OWNER", raising=False)

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ADMIN_EMAIL" in err
    assert "REPO_NAME" in err
    assert "GITEA_REPO_OWNER" in err


def test_cli_missing_admin_pass_returns_rc_1_with_empty_restart(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No gitea_admin_password → rc=1 (yellow), still emits empty RESTART_SERVICES."""
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("REPO_NAME", "nexus-foo")
    monkeypatch.setenv("GITEA_REPO_OWNER", "admin")
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 1
    out = capsys.readouterr().out
    assert "RESTART_SERVICES=" in out
    assert "GITEA_TOKEN=" not in out


def test_cli_bad_secrets_json_returns_rc_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("REPO_NAME", "nexus-foo")
    monkeypatch.setenv("GITEA_REPO_OWNER", "admin")
    monkeypatch.setattr("sys.stdin.read", lambda: "not-json")

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 2


def test_cli_ssh_tunnel_failure_returns_rc_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SSHError during port_forward → rc=2, NO token in stdout."""
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("REPO_NAME", "nexus-foo")
    monkeypatch.setenv("GITEA_REPO_OWNER", "admin")
    monkeypatch.setattr("sys.stdin.read", lambda: '{"gitea_admin_password": "x"}')

    from nexus_deploy.ssh import SSHError

    class _BoomSSH:
        def __init__(self, _host: str) -> None: ...
        def __enter__(self) -> _BoomSSH:
            return self

        def __exit__(self, *_: Any) -> None: ...
        def port_forward(self, *_a: Any, **_k: Any) -> Any:
            raise SSHError("ssh tunnel boom")

    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", _BoomSSH)

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 2
    captured = capsys.readouterr()
    assert "ssh tunnel" in captured.err
    assert "GITEA_TOKEN=" not in captured.out


def test_cli_unexpected_exception_returns_rc_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Generic exception caught and reroutes Python's default rc=1 to rc=2."""
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("REPO_NAME", "nexus-foo")
    monkeypatch.setenv("GITEA_REPO_OWNER", "admin")
    monkeypatch.setattr("sys.stdin.read", lambda: '{"gitea_admin_password": "x"}')

    fake_ssh = MagicMock()
    fake_ssh.__enter__ = MagicMock(return_value=fake_ssh)
    fake_ssh.__exit__ = MagicMock(return_value=None)
    fake_pf = MagicMock()
    fake_pf.__enter__ = MagicMock(return_value=12345)
    fake_pf.__exit__ = MagicMock(return_value=None)
    fake_ssh.port_forward = MagicMock(return_value=fake_pf)
    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", lambda host: fake_ssh)

    secret_in_message = "do-not-leak-secret-XYZZY"

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(secret_in_message)

    monkeypatch.setattr("nexus_deploy.__main__.run_configure_gitea", boom)

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 2
    err = capsys.readouterr().err
    # Type name only, never str(exc) — the exception's message MUST NOT leak
    assert "RuntimeError" in err
    assert secret_in_message not in err


def test_cli_transport_failure_returns_rc_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CalledProcessError from ssh/rsync → rc=2."""
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("REPO_NAME", "nexus-foo")
    monkeypatch.setenv("GITEA_REPO_OWNER", "admin")
    monkeypatch.setattr("sys.stdin.read", lambda: '{"gitea_admin_password": "x"}')

    fake_ssh = MagicMock()
    fake_ssh.__enter__ = MagicMock(return_value=fake_ssh)
    fake_ssh.__exit__ = MagicMock(return_value=None)
    fake_pf = MagicMock()
    fake_pf.__enter__ = MagicMock(return_value=12345)
    fake_pf.__exit__ = MagicMock(return_value=None)
    fake_ssh.port_forward = MagicMock(return_value=fake_pf)
    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", lambda host: fake_ssh)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise subprocess.CalledProcessError(255, ["ssh", "secret-arg"])

    monkeypatch.setattr("nexus_deploy.__main__.run_configure_gitea", boom)

    from nexus_deploy.__main__ import _gitea_configure

    rc = _gitea_configure([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "transport failure" in err
    assert "secret-arg" not in err  # exc.cmd MUST NOT leak


def test_cli_dispatcher_routes_gitea_configure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m nexus_deploy gitea configure` reaches the handler."""
    monkeypatch.setattr(sys, "argv", ["nexus_deploy", "gitea", "configure", "--bogus"])

    from nexus_deploy.__main__ import main

    rc = main()
    assert rc == 2  # --bogus rejected
    assert "gitea configure" in capsys.readouterr().err
