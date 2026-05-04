"""Gitea admin/user/repo configuration client (Phase 2 Modul 2.2e, #505).

Replaces deploy.sh's L2465-2810 — the synchronous Gitea-configure block
covering DB password sync, admin user lifecycle (create or sync, with
legacy email-collision PATCH for stacks deployed pre-v0.51.9), regular
user lifecycle, API token creation with retry-via-delete on conflict,
workspace repo creation with private-PATCH fallback, and collaborator
add. Mirror-mode (``GH_MIRROR_REPOS``) and Woodpecker OAuth
registration are deferred to Modul 2.2f.

Two transports — by design, mirroring deploy.sh's split:

- **CLI via ssh.run_script** for admin/user CRUD (list, create,
  change-password). Inside the gitea container the ``gitea admin user``
  CLI authenticates via peer auth (``-u git``) and DOES NOT need a
  working REST password — which matters because the whole point of
  the SYNC step is to make basic-auth work after persistent-volume
  password drift. Using REST for these would chicken-egg.

- **REST via port-forward + requests** for token, email PATCH, repo
  CRUD, collaborator add. By the time the token is minted, the admin
  password has already been synced via CLI, so basic-auth works.

R7 (token-not-in-LOCAL-argv): all REST calls use ``requests`` with
``auth=(user, pw)`` or ``headers={"Authorization": f"token {tok}"}``
— credentials live in the Authorization header, never in argv on
the deploy host (no shell-out for these calls).

What we DON'T claim — for the SSH/CLI paths, secrets DO transit
the remote container's argv for the brief duration of the docker-
exec call: ``gitea admin user create --password '<pw>'`` and
``psql -c "ALTER USER ... PASSWORD '<pw>'"`` are visible in
``ps -ef`` inside the relevant container while running. We feed
the rendered bash via ``ssh.run_script`` (stdin, not argv) so the
secret never lands in:
  - LOCAL ``ps`` on the deploy host
  - LOCAL CI logs (workflow argv-echoes the bash invocation only)
  - ``CalledProcessError.cmd`` / ``TimeoutExpired.cmd`` exception
    payloads

This matches the exposure profile of the legacy deploy.sh block
exactly — same security boundary, no regression. Tightening
further (e.g. piping the password into ``gitea admin user create``
via stdin or ``--password-stdin``) is upstream-tooling-dependent
and out of scope for this migration.

R5 (path safety): all user/repo path segments are validated against
``^[a-zA-Z0-9._-]+$`` before URL interpolation OR shell-quoting.
Username/repo-name with shell metacharacters are rejected up front.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from typing import Literal

import requests

from nexus_deploy.config import NexusConfig
from nexus_deploy.ssh import SSHClient

_CONNECT_TIMEOUT_S: float = 3.0
_READ_TIMEOUT_S: float = 15.0
_HTTP_TIMEOUT: tuple[float, float] = (_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S)

_PATH_SAFE_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9._-]+$")

# Services that have Git integration (clone the workspace repo on start).
# Order matters for stable RESTART_SERVICES output → CLI emits the list
# in this order, intersected with `enabled_services`. Must match
# deploy.sh L2795 exactly so the strangler-fig handoff doesn't drift.
_GIT_INTEGRATED_SERVICES: tuple[str, ...] = (
    "jupyter",
    "marimo",
    "code-server",
    "meltano",
    "prefect",
)


def _http_timeout_for_deadline(deadline: float) -> tuple[float, float]:
    """Build a (connect, read) tuple clamped to time remaining.

    Same pattern as kestra.py — keeps ``wait_ready(timeout_s=0.05)``
    honest. Both legs are floored at 0.1s so requests doesn't hit its
    own zero-timeout edge case.
    """
    remaining = max(deadline - time.monotonic(), 0.1)
    return (
        min(_CONNECT_TIMEOUT_S, remaining),
        min(_READ_TIMEOUT_S, remaining),
    )


def _validate_path_segment(value: str, *, kind: str) -> None:
    """Reject shell-meta / URL-traversal in user/repo identifiers (R5).

    Allowed: ``[a-zA-Z0-9._-]+`` — but explicitly NOT ``.`` or ``..``
    (which match the regex but are URL-traversal in path context).
    Dotted usernames like ``stefan.koch`` are allowed (Gitea permits
    them — that's the dotted-username class from PR #464).
    """
    if not _PATH_SAFE_RE.fullmatch(value):
        raise GiteaError(f"unsafe {kind}: {value!r}")
    if value in (".", ".."):
        raise GiteaError(f"unsafe {kind}: {value!r}")


def _escape_sql_string_literal(value: str) -> str:
    """Escape a value for safe inclusion in a single-quoted SQL string.

    Mirrors deploy.sh L2479-2480: ``\\`` → ``\\\\`` first, then
    ``'`` → ``''``. Order matters — escape backslashes before quotes
    so a literal backslash in the password doesn't end up doubling
    the quote escape.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


def _parse_admin_list_for_user(text: str, username: str) -> tuple[bool, str | None]:
    """Column-exact awk-equivalent on ``gitea admin user list`` output (R1).

    Gitea CLI output:

    .. code-block:: text

        ID    Username    Email                FullName    IsActive
        1     admin       admin@example.com    Admin       true
        2     stefan      stefan@example.com   Stefan      true

    Returns ``(exists, email)``: column-2 (Username) must equal
    ``username`` exactly. NEVER substring match — the dotted-username
    bug from PR #464 was: ``grep -c 'stefan.koch'`` matched admin's
    email column ``stefan.koch@hslu.ch`` even though no user with
    that username existed, so CREATE was skipped, SYNC then failed.

    Empty / malformed output → ``(False, None)``. Headers (``NR==1``)
    are skipped.
    """
    for line_no, raw_line in enumerate(text.splitlines()):
        if line_no == 0:
            continue  # header
        parts = raw_line.split()
        if len(parts) < 2:
            continue
        if parts[1] == username:
            email = parts[2] if len(parts) >= 3 else None
            return True, email
    return False, None


def _render_db_pw_sync_script(
    escaped_pw: str,
    *,
    attempts: int,
    interval_s: float,
) -> str:
    """Render bash to retry psql ALTER USER inside the gitea-db container.

    Peer auth via ``-U nexus-gitea`` (no ``-W``), so no PGPASSWORD env
    var is needed and the password value only enters the SQL string
    literal. The SCRIPT body (containing the SQL) is fed via stdin
    by the caller (``ssh.run_script``), so the password does NOT
    appear in:
      - LOCAL ``ps`` on the deploy host
      - LOCAL CI logs / ``CalledProcessError.cmd`` payloads
      - SSH argv on the deploy host

    The password DOES appear in the gitea-db container's ``ps -ef``
    for the brief duration of the ``psql -c "ALTER USER … PASSWORD
    '<pw>'"`` call (since psql takes the SQL via argv). This matches
    deploy.sh L2483-2485's exposure profile exactly — no regression.
    Tightening would require either ``\\password`` (interactive) or
    a server-side script feeding the SQL via stdin to psql, both of
    which add complexity for marginal gain on a runner-isolated
    container.

    Mirrors deploy.sh L2477-2491. RESULT line emitted on success so
    the caller can disambiguate "succeeded after N tries" from "all
    N tries failed".
    """
    sql = f"ALTER USER \"nexus-gitea\" WITH PASSWORD '{escaped_pw}'"
    quoted_sql = shlex.quote(sql)
    return (
        "set -euo pipefail\n"
        f"for i in $(seq 1 {attempts}); do\n"
        f"  if docker exec gitea-db psql -U nexus-gitea -d gitea "
        f"-c {quoted_sql} >/dev/null 2>&1; then\n"
        '    echo "RESULT db_pw=synced"\n'
        "    exit 0\n"
        "  fi\n"
        f"  sleep {interval_s}\n"
        "done\n"
        'echo "RESULT db_pw=failed"\n'
        "exit 1\n"
    )


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


CreateUserStatus = Literal["created", "already_exists", "synced", "failed"]
CreateRepoStatus = Literal["created", "already_exists", "failed"]


@dataclass(frozen=True)
class CreateUserResult:
    name: str
    status: CreateUserStatus
    detail: str = ""


@dataclass(frozen=True)
class CreateRepoResult:
    name: str
    status: CreateRepoStatus
    detail: str = ""


@dataclass(frozen=True)
class GiteaResult:
    """Aggregate of all Gitea-config sub-steps.

    ``token`` is None until :meth:`GiteaCli.mint_token` succeeds (post-
    #519 fix switched from REST basic-auth to CLI peer-auth). The CLI
    handler emits ``GITEA_TOKEN=<token>`` to stdout iff token is
    non-None — eval-able by deploy.sh.
    """

    db_pw_synced: bool
    admin: CreateUserResult
    user: CreateUserResult | None
    token: str | None
    # Diagnostic message when ``token is None`` — empty string on
    # success. Captures the Gitea-CLI-side error description so the
    # CLI handler can emit it to stderr without leaking secrets.
    # Added in the post-#519 fix when production spin-up surfaced a
    # silent token-mint failure with no diagnostic trace.
    token_error: str
    repo: CreateRepoResult | None
    collaborator_added: bool
    restart_services: tuple[str, ...]

    @property
    def is_success(self) -> bool:
        """Strict success: every step that ran must have succeeded.

        - admin status must be ``created``, ``already_exists``, or ``synced``
          (NOT ``failed``)
        - user (if present) same
        - token must exist (if expected — i.e. core happy path)
        - repo (if present) must be ``created`` or ``already_exists``

        deploy.sh maps False → rc=1 (yellow warn, continue). The CLI
        only emits ``GITEA_TOKEN=`` to stdout when ``token is not None``
        — so on partial-failure paths where the token DID get minted
        (e.g. legacy email PATCH failed but token + repo OK), deploy.sh
        captures the token via ``eval`` and downstream seed/kestra
        still work; on paths where token is None (token-mint failed)
        is_success is False AND no token line is emitted, so deploy.sh
        sees rc=1 but no `$GITEA_TOKEN`, and the seed/kestra blocks
        skip themselves on the empty-token guard.
        """
        if self.admin.status == "failed":
            return False
        if self.user is not None and self.user.status == "failed":
            return False
        if self.token is None:
            return False
        return not (self.repo is not None and self.repo.status == "failed")


class GiteaError(Exception):
    """Transport/validation failure surfaced to the caller.

    Carries no response body — Gitea error responses on auth-failure
    paths can echo back the credentials we just sent. Constructed
    from fixed format strings + status codes / type names only.
    """


# ---------------------------------------------------------------------------
# Hybrid client (post-#519 fix):
#   SSH CLI peer-auth: admin/user CRUD + token mint
#   REST basic-auth or token-auth: legacy email PATCH, repo CRUD, collab add
# Token minting moved from REST to CLI after the production
# 400-from-CreateAccessToken bug — see GiteaCli.mint_token docstring.
# ---------------------------------------------------------------------------


class GiteaCli:
    """SSH-driven ``docker exec gitea`` CLI wrapper.

    Used for admin/user CRUD where peer auth (``-u git`` inside the
    container) bypasses the chicken-egg of "we need to sync the
    password before basic-auth works". Output is parsed locally so
    the typed dispatch (``created`` / ``already_exists`` / ``synced``
    / ``failed``) matches the rest of the module.

    All commands fed via ``ssh.run_script`` so passwords land in
    stdin to the remote shell, not argv on either host.
    """

    def __init__(self, ssh: SSHClient) -> None:
        self.ssh = ssh

    def sync_db_password(
        self,
        password: str,
        *,
        attempts: int = 15,
        interval_s: float = 2.0,
    ) -> bool:
        """Retry ``ALTER USER`` until psql accepts the connection.

        On first start, gitea-db can take ~10-30s to accept connections.
        Bounded retry loop renders bash that exits 0 on first success
        or non-zero after exhausting attempts.
        """
        if not password:
            return False
        escaped = _escape_sql_string_literal(password)
        script = _render_db_pw_sync_script(escaped, attempts=attempts, interval_s=interval_s)
        # Generous overall timeout: attempts * interval_s + a safety
        # margin for ssh + docker exec setup per iteration. Never
        # raises — this is best-effort and we map a non-zero rc to
        # ``False`` so the caller can route to a yellow warning.
        timeout = float(attempts) * float(interval_s) + 30.0
        result = self.ssh.run_script(script, check=False, timeout=timeout)
        return result.returncode == 0 and "RESULT db_pw=synced" in result.stdout

    def list_admin_users(self) -> str:
        """Run ``gitea admin user list --admin`` and return raw output.

        Empty string if ssh/docker fails — caller routes empty list
        to the CREATE branch (deploy.sh L2620-2630 pattern: any
        unexpected error surfaces on the next CLI call rather than
        spinning here).
        """
        # ``2>/dev/null`` mirrors deploy.sh — the CLI sometimes warns
        # on stderr about deprecated flags; we don't want that noise
        # mixed into the parsed output. ``|| echo ""`` swallows non-zero
        # exit (transient docker/gitea startup race).
        result = self.ssh.run_script(
            "docker exec -u git gitea gitea admin user list --admin 2>/dev/null || echo ''",
            check=False,
            timeout=30.0,
        )
        return result.stdout if result.returncode == 0 else ""

    def list_users(self) -> str:
        """Run ``gitea admin user list`` (non-admin scope)."""
        result = self.ssh.run_script(
            "docker exec -u git gitea gitea admin user list 2>/dev/null || echo ''",
            check=False,
            timeout=30.0,
        )
        return result.stdout if result.returncode == 0 else ""

    def create_admin(self, username: str, password: str, email: str) -> CreateUserResult:
        return self._create_user(username, password, email, is_admin=True)

    def create_user(self, username: str, password: str, email: str) -> CreateUserResult:
        return self._create_user(username, password, email, is_admin=False)

    def _create_user(
        self,
        username: str,
        password: str,
        email: str,
        *,
        is_admin: bool,
    ) -> CreateUserResult:
        _validate_path_segment(username, kind="username")
        # email is not a URL segment but still feed via shlex.quote
        # since it lands in argv after rendering. The container's
        # `gitea admin user create` accepts it directly.
        admin_flag = "--admin " if is_admin else ""
        script = (
            "set -euo pipefail\n"
            f"docker exec -u git gitea gitea admin user create {admin_flag}"
            f"--username {shlex.quote(username)} "
            f"--password {shlex.quote(password)} "
            f"--email {shlex.quote(email)} "
            "--must-change-password=false 2>&1\n"
        )
        result = self.ssh.run_script(script, check=False, timeout=30.0)
        text = result.stdout
        # deploy.sh L2600 / L2659: success substrings.
        # ``CreateUserResult.name`` is always the real username (Copilot
        # round 1) — using a role label ("admin"/"user") here while
        # ``sync_password`` returns the actual username made the field
        # semantics inconsistent and confused downstream reporting.
        text_lc = text.lower()
        if any(kw in text_lc for kw in ("created", "success", "new user")):
            return CreateUserResult(name=username, status="created")
        # Gitea returns "user already exists" / "email already in use" on
        # collision — both route to ``already_exists`` so the caller can
        # follow up with a sync_password (which is idempotent) instead of
        # treating it as a failure.
        if "already" in text_lc:
            return CreateUserResult(name=username, status="already_exists")
        return CreateUserResult(
            name=username,
            status="failed",
            detail=text.strip()[:200] if text else "(no output)",
        )

    def mint_token(
        self,
        username: str,
        name: str,
        scopes: str = "all",
    ) -> tuple[str | None, str]:
        """Generate API token via ``gitea admin user generate-access-token``.

        Returns ``(sha1_or_None, diagnostic_message)``. On success the
        diagnostic is empty. On failure the diagnostic is a short
        Gitea-CLI-error description suitable for stderr — Gitea's
        delete/generate output is just username + token-name +
        status, never password material.

        Idempotent: deletes any existing token with the same name
        first (best-effort, ``|| true`` swallows the non-zero rc Gitea's
        CLI returns when the token doesn't exist), then generates fresh.
        Mirrors the legacy deploy.sh delete-then-create pattern but
        via peer-auth CLI instead of REST basic-auth — eliminates
        the chicken-egg of "we need a working REST password to mint
        a token" that bit production in PR #519's spin-up: REST POST
        returned 400 inside ``CreateAccessToken`` despite the prior
        admin password sync reporting success. The CLI peer-auths
        as the container's git user, so password drift between the
        CLI sync and the basic-auth attempt cannot manifest.
        """
        _validate_path_segment(username, kind="username")
        _validate_path_segment(name, kind="token_name")
        # Best-effort delete — peer auth CLI; rc=0 if token existed,
        # rc!=0 if not. Either way, the next generate succeeds.
        delete_script = (
            "docker exec -u git gitea gitea admin user delete-access-token "
            f"--username {shlex.quote(username)} "
            f"--token {shlex.quote(name)} "
            ">/dev/null 2>&1 || true\n"
        )
        self.ssh.run_script(delete_script, check=False, timeout=30.0)

        generate_script = (
            "set -euo pipefail\n"
            "docker exec -u git gitea gitea admin user generate-access-token "
            f"--username {shlex.quote(username)} "
            f"--token-name {shlex.quote(name)} "
            f"--scopes {shlex.quote(scopes)} 2>&1\n"
        )
        result = self.ssh.run_script(generate_script, check=False, timeout=30.0)
        if result.returncode != 0:
            # Output examples on failure: "User does not exist" or
            # "...invalid scopes...". Capture first line only.
            first_line = (result.stdout or "").splitlines()
            detail = first_line[0][:200] if first_line else "(no output)"
            return None, f"CLI rc={result.returncode}: {detail}"

        # Success output: "Access token was successfully created: <40-hex>"
        match = re.search(r"\b([a-f0-9]{40})\b", result.stdout or "")
        if match:
            return match.group(1), ""
        return None, "CLI rc=0 but no sha1 in output"

    def sync_password(self, username: str, password: str) -> CreateUserResult:
        """``gitea admin user change-password`` — peer-auth, no old password.

        Gitea's CLI command takes the username + new password and
        updates the credential without requiring the previous one.
        Idempotent: running twice on the same password is a no-op.
        """
        _validate_path_segment(username, kind="username")
        script = (
            "set -euo pipefail\n"
            "docker exec -u git gitea gitea admin user change-password "
            f"--username {shlex.quote(username)} "
            f"--password {shlex.quote(password)} "
            "--must-change-password=false 2>&1\n"
        )
        result = self.ssh.run_script(script, check=False, timeout=30.0)
        if result.returncode == 0:
            return CreateUserResult(name=username, status="synced")
        return CreateUserResult(
            name=username,
            status="failed",
            detail=result.stdout.strip()[:200] if result.stdout else "(no output)",
        )


class GiteaClient:
    """REST client for Gitea. Basic-auth pre-token, token-auth post-token.

    Path components in URL interpolation are validated against the
    R5 path-safety regex before the f-string runs. Credentials in
    Authorization header only — ``with_token`` returns a new client
    instance so the pre/post-token modes don't share mutable state.
    """

    def __init__(
        self,
        base_url: str,
        *,
        admin_username: str,
        admin_password: str,
    ) -> None:
        if not admin_username or not admin_password:
            raise ValueError("GiteaClient requires non-empty admin credentials")
        _validate_path_segment(admin_username, kind="admin_username")
        self.base_url = base_url.rstrip("/")
        self.admin_username = admin_username
        self._auth: tuple[str, str] | None = (admin_username, admin_password)
        self._token: str | None = None

    def with_token(self, token: str) -> GiteaClient:
        """Return a NEW client that uses token-auth instead of basic-auth.

        Token must be non-empty. Returns a separate instance so callers
        can't accidentally fall back to basic-auth on a client that's
        meant to be token-only.
        """
        if not token:
            raise ValueError("with_token requires a non-empty token")
        # New instance — copy URL + admin_username, drop basic-auth,
        # set token. Bypass __init__'s admin_password requirement.
        new = GiteaClient.__new__(GiteaClient)
        new.base_url = self.base_url
        new.admin_username = self.admin_username
        new._auth = None
        new._token = token
        return new

    def _request_kwargs(self) -> dict[str, object]:
        """Build auth kwargs (headers OR auth tuple, never both)."""
        if self._token is not None:
            return {"headers": {"Authorization": f"token {self._token}"}}
        if self._auth is not None:
            return {"auth": self._auth}
        raise GiteaError("client has no auth configured")  # pragma: no cover

    def wait_ready(self, *, timeout_s: float = 60.0, interval_s: float = 2.0) -> bool:
        """Poll ``GET /api/healthz`` until 200. Public endpoint, no auth.

        Sleep clamped to deadline (kestra.py pattern).
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                resp = requests.get(
                    f"{self.base_url}/api/healthz",
                    timeout=_http_timeout_for_deadline(deadline),
                )
            except (requests.ConnectionError, requests.Timeout):
                resp = None
            if resp is not None and resp.status_code == 200:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval_s, remaining))
        return False

    def patch_user_email(self, username: str, email: str, *, login_name: str) -> bool:
        """``PATCH /api/v1/admin/users/<u>`` — email-only update (R2).

        Gitea's admin-users schema rejects partial bodies, so the
        full ``{email, source_id, login_name}`` triple is required
        even though we only want to change email. ``source_id: 0`` =
        local auth provider. Returns True on 200, False on any other
        status (including auth failures — caller handles non-fatal
        path).
        """
        _validate_path_segment(username, kind="username")
        body = {"email": email, "source_id": 0, "login_name": login_name}
        try:
            resp = requests.patch(
                f"{self.base_url}/api/v1/admin/users/{username}",
                json=body,
                timeout=_HTTP_TIMEOUT,
                **self._request_kwargs(),  # type: ignore[arg-type]
            )
        except (requests.ConnectionError, requests.Timeout):
            return False
        return resp.status_code == 200

    def repo_exists(self, owner: str, name: str) -> bool:
        _validate_path_segment(owner, kind="owner")
        _validate_path_segment(name, kind="repo_name")
        try:
            resp = requests.get(
                f"{self.base_url}/api/v1/repos/{owner}/{name}",
                timeout=_HTTP_TIMEOUT,
                **self._request_kwargs(),  # type: ignore[arg-type]
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise GiteaError(f"repo_exists transport ({type(exc).__name__})") from exc
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise GiteaError(f"repo_exists HTTP {resp.status_code}")

    def create_repo(
        self,
        name: str,
        *,
        private: bool = True,
        auto_init: bool = True,
        default_branch: str = "main",
        description: str = "",
    ) -> CreateRepoResult:
        """``POST /api/v1/user/repos`` — creates under the authenticated user.

        409 → ``already_exists``. ``patch_repo_private`` is the
        recommended fallback to ensure the existing repo is private.
        """
        _validate_path_segment(name, kind="repo_name")
        body = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": auto_init,
            "default_branch": default_branch,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/user/repos",
                json=body,
                timeout=_HTTP_TIMEOUT,
                **self._request_kwargs(),  # type: ignore[arg-type]
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            return CreateRepoResult(
                name=name, status="failed", detail=f"transport ({type(exc).__name__})"
            )
        if resp.status_code in (200, 201):
            return CreateRepoResult(name=name, status="created", detail="POST 201")
        if resp.status_code == 409:
            return CreateRepoResult(name=name, status="already_exists", detail="POST 409")
        # Gitea also returns 422 with "already exists" for some modes
        # (CE vs EE differ). Match conservatively.
        if resp.status_code == 422:
            try:
                msg = resp.json().get("message", "") if resp.content else ""
            except ValueError:
                msg = ""
            if isinstance(msg, str) and "already exists" in msg.lower():
                return CreateRepoResult(
                    name=name, status="already_exists", detail="POST 422 already exists"
                )
        return CreateRepoResult(name=name, status="failed", detail=f"POST {resp.status_code}")

    def patch_repo_private(self, owner: str, name: str, *, private: bool = True) -> bool:
        """``PATCH /api/v1/repos/<o>/<n>`` — ensure repo is/isn't private."""
        _validate_path_segment(owner, kind="owner")
        _validate_path_segment(name, kind="repo_name")
        try:
            resp = requests.patch(
                f"{self.base_url}/api/v1/repos/{owner}/{name}",
                json={"private": private},
                timeout=_HTTP_TIMEOUT,
                **self._request_kwargs(),  # type: ignore[arg-type]
            )
        except (requests.ConnectionError, requests.Timeout):
            return False
        return resp.status_code == 200

    def add_collaborator(
        self,
        owner: str,
        name: str,
        collaborator: str,
        *,
        permission: str = "write",
    ) -> bool:
        """``PUT /api/v1/repos/<o>/<n>/collaborators/<c>`` — idempotent.

        204 (added) and 422 ("already a collaborator") both → True.
        """
        _validate_path_segment(owner, kind="owner")
        _validate_path_segment(name, kind="repo_name")
        _validate_path_segment(collaborator, kind="collaborator")
        try:
            resp = requests.put(
                f"{self.base_url}/api/v1/repos/{owner}/{name}/collaborators/{collaborator}",
                json={"permission": permission},
                timeout=_HTTP_TIMEOUT,
                **self._request_kwargs(),  # type: ignore[arg-type]
            )
        except (requests.ConnectionError, requests.Timeout):
            return False
        return resp.status_code in (204, 422)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def _compute_restart_services(enabled: list[str]) -> tuple[str, ...]:
    """Intersection of ``_GIT_INTEGRATED_SERVICES`` and ``enabled``.

    Order preserved from ``_GIT_INTEGRATED_SERVICES`` so the CLI
    output is deterministic across runs.
    """
    enabled_set = set(enabled)
    return tuple(s for s in _GIT_INTEGRATED_SERVICES if s in enabled_set)


def run_configure_gitea(
    config: NexusConfig,
    *,
    base_url: str,
    ssh: SSHClient,
    admin_email: str,
    gitea_user_email: str | None,
    gitea_user_password: str | None,
    repo_name: str,
    gitea_repo_owner: str,
    is_mirror_mode: bool,
    enabled_services: list[str],
    ready_timeout_s: float = 60.0,
    db_sync_attempts: int = 15,
    db_sync_interval_s: float = 2.0,
) -> GiteaResult:
    """End-to-end Gitea configure (deploy.sh L2465-2810 equivalent).

    Steps:

    1. Sync gitea-db postgres password (peer-auth psql ALTER USER)
    2. Wait for ``/api/healthz``
    3. Admin: list via CLI → exists?
       - yes + legacy email collision → REST PATCH email
       - yes → CLI sync_password
       - no  → CLI create_admin
    4. User (if ``gitea_user_email``): list → exists?
       - yes → CLI sync_password
       - no  → CLI create_user
    5. Token: CLI ``mint_token`` (peer-auth ``generate-access-token``,
       idempotent delete-then-create; switched from REST in post-#519 fix)
    6. (non-mirror) repo: create → on already_exists → patch_repo_private
    7. (non-mirror, with user) collaborator add
    8. Build restart_services list (intersection with enabled)

    Returns :class:`GiteaResult` with token in stdout-eval-able form
    via the CLI handoff. Even on partial failures (e.g. legacy email
    PATCH failed but token created), the token IS in the result so
    deploy.sh can capture it via eval.
    """
    admin_username = config.admin_username or "admin"
    admin_password = config.gitea_admin_password or ""
    db_password = config.gitea_db_password or ""

    cli = GiteaCli(ssh)
    rest = GiteaClient(
        base_url=base_url,
        admin_username=admin_username,
        admin_password=admin_password,
    )

    # 1. DB password sync
    db_pw_synced = (
        cli.sync_db_password(
            db_password,
            attempts=db_sync_attempts,
            interval_s=db_sync_interval_s,
        )
        if db_password
        else False
    )

    # 2. Wait for Gitea HTTP ready
    if not rest.wait_ready(timeout_s=ready_timeout_s):
        return GiteaResult(
            db_pw_synced=db_pw_synced,
            # Use the configured admin_username (Copilot round 2) — not
            # the literal "admin" — so CreateUserResult.name carries
            # the same value across all paths regardless of how the
            # operator named the admin user.
            admin=CreateUserResult(name=admin_username, status="failed", detail="gitea not ready"),
            user=None,
            token=None,
            token_error="gitea not ready",  # noqa: S106 — diagnostic, not a credential
            repo=None,
            collaborator_added=False,
            restart_services=_compute_restart_services(enabled_services),
        )

    # 3. Admin: CLI list → parse → exists check → branch
    admin_list = cli.list_admin_users()
    admin_exists, current_admin_email = _parse_admin_list_for_user(admin_list, admin_username)

    # 3a. Legacy email-collision PATCH (before sync_password — if PATCH
    # fails because of password drift, sync_password later will fix
    # the password and the next deploy's PATCH will succeed).
    if admin_exists and gitea_user_email and current_admin_email == gitea_user_email:
        # Best-effort. If it fails, the sync_password below still runs;
        # next deploy will retry the PATCH.
        rest.patch_user_email(admin_username, admin_email, login_name=admin_username)

    if admin_exists:
        admin_result = cli.sync_password(admin_username, admin_password)
    else:
        admin_result = cli.create_admin(admin_username, admin_password, admin_email)
        # CREATE returns ``already_exists`` when the existence check was a
        # false negative (e.g. ssh+docker exec failed → empty list → CREATE
        # path → "user already exists"). Without a follow-up sync, the
        # admin password drift stays — the subsequent REST token mint
        # uses basic-auth with the OpenTofu-generated password and 401s.
        # Fall back to sync_password so we converge on the desired state.
        # Same defence-in-depth pattern as deploy.sh's rerun-tolerance,
        # but tightened (Copilot round 1).
        if admin_result.status == "already_exists":
            admin_result = cli.sync_password(admin_username, admin_password)

    # 4. Regular user (only if email + password provided)
    user_result: CreateUserResult | None = None
    user_username: str | None = None
    if gitea_user_email and gitea_user_password:
        user_username = gitea_user_email.split("@", 1)[0]
        user_list = cli.list_users()
        user_exists, _ = _parse_admin_list_for_user(user_list, user_username)
        if user_exists:
            user_result = cli.sync_password(user_username, gitea_user_password)
        else:
            user_result = cli.create_user(user_username, gitea_user_password, gitea_user_email)
            # Same already_exists → sync_password fallback as for admin.
            if user_result.status == "already_exists":
                user_result = cli.sync_password(user_username, gitea_user_password)

    # 5. Token via CLI peer auth (was: REST basic-auth in PR #519).
    # Switched after production spin-up surfaced a silent 400 from
    # POST /api/v1/users/<u>/tokens despite admin password sync
    # reporting success — likely a subtle password-state race
    # between the bcrypt commit and the next-millisecond REST
    # auth check. CLI peer auth eliminates the chicken-egg: the
    # docker-exec runs as the container's git user with no
    # password verification needed.
    token, token_error = cli.mint_token(admin_username, "nexus-automation", "all")

    # 6+7. Repo + collaborator (skip in mirror mode)
    repo_result: CreateRepoResult | None = None
    collaborator_added = False
    if not is_mirror_mode and token is not None:
        rest_token = rest.with_token(token)
        repo_result = rest_token.create_repo(
            repo_name,
            private=True,
            auto_init=True,
            default_branch="main",
            description="Shared workspace for notebooks, workflows, and pipelines",
        )
        if repo_result.status == "already_exists":
            # Belt-and-suspenders: ensure existing repo is private.
            rest_token.patch_repo_private(gitea_repo_owner, repo_name, private=True)
        if repo_result.status != "failed" and user_username is not None and gitea_user_password:
            collaborator_added = rest_token.add_collaborator(
                gitea_repo_owner, repo_name, user_username, permission="write"
            )

    return GiteaResult(
        db_pw_synced=db_pw_synced,
        admin=admin_result,
        user=user_result,
        token=token,
        token_error=token_error,
        repo=repo_result,
        collaborator_added=collaborator_added,
        restart_services=_compute_restart_services(enabled_services),
    )
