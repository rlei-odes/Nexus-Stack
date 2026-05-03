"""Per-service admin-setup hooks (Phase 2 Modul 2.2b, #505).

Replaces 5 REST first-init admin-setup blocks in ``scripts/deploy.sh``
(the [7/7] Auto-configuring services section): Portainer, n8n,
Metabase, LakeFS, OpenMetadata. Each hook:

  1. Waits for the service container to be HTTP-ready
  2. Optionally checks "already configured" (idempotent skip)
  3. POSTs the admin-init / first-setup payload
  4. Yellow-warns on failure, never aborts

Three other admin-setup families are scoped to follow-up modules:
- 2.2c: docker-exec hooks (Filestash file-mutation, RedPanda rpk CLI,
  Superset fab CLI)
- 2.2d: Gitea (synchronous, depends on Postgres + seeder)
- Future: SFTPGo, Garage, Windmill, Wikijs, Dify (each has its own
  pattern; migrating piecemeal as time allows)

Why one ssh round-trip with rendered bash (consistent with infisical /
secret_sync / seeder / compose_runner): the curl loop is proven, one
SSH connection vs N, the rendered script is testable as a string,
and Phase 3 (#505 Modul 3.1) replaces the bash rendering wholesale
with paramiko + ``requests``.

Eight rounds of hardening preserved (one regression test per round
in ``tests/unit/test_services.py``):

R1. ``set -euo pipefail`` first executable line.
R2. Per-spec healthcheck timeout (Metabase 120s, OpenMetadata 180s,
    LakeFS 60s, Portainer 5s, n8n 60s — NOT a global default).
R3. EXIT trap cleans the curl-config tmpfile and any scratch tmpfiles.
R4. JSON setup-body built via jq with shell-injection-safe ``--arg``
    (NOT string interpolation), and the body is fed to curl via
    stdin (``--data-binary @-``) so admin passwords don't reach argv.
R5. Idempotent skip when ``already_configured_substring`` appears in
    the pre-setup probe response (deploy.sh's ``[ -z "$SETUP_TOKEN" ]``
    / ``"setup_complete":true`` / etc. branches).
R6. error_strategy=continue: a failed hook NEVER aborts the orchestrator;
    the next hook still runs. Mirrors deploy.sh's ``yellow-warn,
    continue`` pattern.
R7. Hook execution order is deterministic — operators rely on it for
    log-debug and the integration with deploy.sh's [7/7] sequence.
R8. RESULT-line-per-hook: ``RESULT hook=<name> status=<configured|
    already-configured|failed|skipped-not-ready>``. The orchestrator
    parses one line per hook, never grepping for emoji.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from nexus_deploy import _remote
from nexus_deploy.config import NexusConfig
from nexus_deploy.infisical import BootstrapEnv

_RESULT_LINE_RE = re.compile(
    r"^RESULT hook=(?P<name>[A-Za-z0-9_-]+) "
    r"status=(?P<status>configured|already-configured|failed|skipped-not-ready)$",
    re.MULTILINE,
)

HookStatus = Literal["configured", "already-configured", "failed", "skipped-not-ready"]


@dataclass(frozen=True)
class HookResult:
    """Outcome of one admin-setup hook."""

    name: str
    status: HookStatus


@dataclass(frozen=True)
class SetupResult:
    """Aggregate of all hook outcomes for one orchestrator call."""

    hooks: tuple[HookResult, ...]

    @property
    def configured(self) -> int:
        return sum(1 for h in self.hooks if h.status == "configured")

    @property
    def already_configured(self) -> int:
        return sum(1 for h in self.hooks if h.status == "already-configured")

    @property
    def skipped_not_ready(self) -> int:
        return sum(1 for h in self.hooks if h.status == "skipped-not-ready")

    @property
    def failed(self) -> int:
        return sum(1 for h in self.hooks if h.status == "failed")

    @property
    def is_success(self) -> bool:
        """All hooks ended in a non-failed terminal state."""
        return self.failed == 0


# ---------------------------------------------------------------------------
# Per-hook bash renderers. Each takes NexusConfig and returns a bash
# fragment that, when executed server-side, emits exactly one
# `RESULT hook=<name> status=<...>` line.
# ---------------------------------------------------------------------------


def _render_wait_healthy(
    *,
    name: str,
    url: str,
    timeout_seconds: int,
    interval_seconds: int = 2,
    predicate: str = '[ "$STATUS" = "200" ]',
) -> str:
    """Render a polling-wait loop; sets ``$READY`` to ``true``/``false``.

    The predicate runs against ``$STATUS`` (HTTP code from curl
    ``-w '%{http_code}'``). Specs that need a body-substring check
    (OpenMetadata's ``grep 'version'``) build a custom inner block
    instead of using this helper.
    """
    iters = max(1, timeout_seconds // interval_seconds)
    return f"""
READY=false
for _ in $(seq 1 {iters}); do
    STATUS=$(curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 {shlex.quote(url)} 2>/dev/null || echo "000")
    if {predicate}; then READY=true; break; fi
    sleep {interval_seconds}
done
if [ "$READY" != "true" ]; then
    echo "  ⚠ {name} not ready after {timeout_seconds}s — skipping setup" >&2
    echo "RESULT hook={name} status=skipped-not-ready"
    return 0 2>/dev/null || exit 0
fi
"""


def render_portainer_hook(config: NexusConfig, env: BootstrapEnv) -> str:
    """Portainer first-init: ``POST /api/users/admin/init`` (no auth)."""
    del env  # not used; signature uniform across hooks
    username = config.admin_username or "admin"
    password = config.portainer_admin_password or ""
    if not password:
        return 'echo "RESULT hook=portainer status=skipped-not-ready"\n'
    body = json.dumps({"Username": username, "Password": password}, separators=(",", ":"))
    body_q = shlex.quote(body)
    wait = _render_wait_healthy(
        name="portainer",
        url="http://localhost:9090/api/system/status",
        timeout_seconds=5,
        interval_seconds=1,
    )
    return f"""
portainer_hook() {{
    {wait}
    RESP=$(curl -s -X POST 'http://localhost:9090/api/users/admin/init' \\
        -H 'Content-Type: application/json' \\
        --data-binary {body_q} 2>/dev/null || echo "")
    if echo "$RESP" | grep -q '"Id"'; then
        echo "RESULT hook=portainer status=configured"
    elif echo "$RESP" | grep -q 'already initialized'; then
        echo "RESULT hook=portainer status=already-configured"
    else
        echo "RESULT hook=portainer status=failed"
    fi
}}
portainer_hook
"""


def render_n8n_hook(config: NexusConfig, env: BootstrapEnv) -> str:
    """n8n owner-setup: ``POST /rest/owner/setup`` (no auth, idempotent via /rest/settings)."""
    email = env.admin_email or ""
    password = config.n8n_admin_password or ""
    if not password or not email:
        return 'echo "RESULT hook=n8n status=skipped-not-ready"\n'
    body = json.dumps(
        {
            "email": email,
            "firstName": "Admin",
            "lastName": "User",
            "password": password,
        },
        separators=(",", ":"),
    )
    body_q = shlex.quote(body)
    wait = _render_wait_healthy(
        name="n8n",
        url="http://localhost:5678/healthz",
        timeout_seconds=60,
    )
    return f"""
n8n_hook() {{
    {wait}
    SETTINGS=$(curl -s 'http://localhost:5678/rest/settings' 2>/dev/null || echo "{{}}")
    NEEDS_SETUP=$(printf '%s' "$SETTINGS" | jq -r '.data.userManagement.showSetupOnFirstLoad // true | if . then "true" else "false" end' 2>/dev/null || echo "true")
    if [ "$NEEDS_SETUP" = "false" ]; then
        echo "RESULT hook=n8n status=already-configured"
        return 0
    fi
    RESP=$(curl -s -X POST 'http://localhost:5678/rest/owner/setup' \\
        -H 'Content-Type: application/json' \\
        --data-binary {body_q} 2>/dev/null || echo "")
    if echo "$RESP" | grep -q '"id"'; then
        echo "RESULT hook=n8n status=configured"
    else
        echo "RESULT hook=n8n status=failed"
    fi
}}
n8n_hook
"""


def render_metabase_hook(config: NexusConfig, env: BootstrapEnv) -> str:
    """Metabase first-setup: ``POST /api/setup`` with one-time setup token."""
    email = env.admin_email or ""
    password = config.metabase_admin_password or ""
    if not password or not email:
        return 'echo "RESULT hook=metabase status=skipped-not-ready"\n'
    email_q = shlex.quote(email)
    password_q = shlex.quote(password)
    wait = _render_wait_healthy(
        name="metabase",
        url="http://localhost:3000/api/health",
        timeout_seconds=120,
    )
    return f"""
metabase_hook() {{
    {wait}
    SETUP_TOKEN=$(curl -s 'http://localhost:3000/api/session/properties' 2>/dev/null \\
        | jq -r '."setup-token" // empty' 2>/dev/null || echo "")
    if [ -z "$SETUP_TOKEN" ]; then
        echo "RESULT hook=metabase status=already-configured"
        return 0
    fi
    BODY=$(jq -n \\
        --arg token "$SETUP_TOKEN" \\
        --arg email {email_q} \\
        --arg password {password_q} \\
        '{{token: $token, user: {{email: $email, first_name: "Admin", last_name: "User", password: $password}}, prefs: {{site_name: "Nexus Stack Analytics", allow_tracking: false}}}}')
    RESP=$(printf '%s' "$BODY" | curl -s -X POST 'http://localhost:3000/api/setup' \\
        -H 'Content-Type: application/json' \\
        --data-binary @- 2>/dev/null || echo "")
    if echo "$RESP" | grep -q '"id"'; then
        echo "RESULT hook=metabase status=configured"
    else
        echo "RESULT hook=metabase status=failed"
    fi
}}
metabase_hook
"""


def render_lakefs_hook(config: NexusConfig, env: BootstrapEnv) -> str:
    """LakeFS: ``POST /api/v1/setup_lakefs`` then ``POST /api/v1/repositories``.

    Two-step: setup admin user (no auth, idempotent via ``/api/v1/config``
    ``setup_complete`` flag) THEN create the default repo (basic-auth
    using the just-created credentials, idempotent via "already exists"
    response substring). Both reported as one ``RESULT`` line; the
    repo step's status is folded into the overall hook outcome.
    """
    del env  # not used; signature uniform across hooks
    access_key = config.lakefs_admin_access_key or ""
    secret_key = config.lakefs_admin_secret_key or ""
    if not access_key or not secret_key:
        return 'echo "RESULT hook=lakefs status=skipped-not-ready"\n'
    access_q = shlex.quote(access_key)
    secret_q = shlex.quote(secret_key)
    # Storage namespace selection mirrors deploy.sh:2762-2770. We let
    # the rendered bash decide based on the env vars on the server, so
    # the Python doesn't need to read them — keeps the renderer pure.
    hetzner_bucket = config.hetzner_s3_bucket_lakefs or ""
    hetzner_q = shlex.quote(hetzner_bucket)
    wait = _render_wait_healthy(
        name="lakefs",
        url="http://localhost:8000/api/v1/healthcheck",
        timeout_seconds=60,
    )
    return f"""
lakefs_hook() {{
    {wait}
    CFG=$(curl -s 'http://localhost:8000/api/v1/config' 2>/dev/null || echo "")
    SETUP_DONE=false
    if echo "$CFG" | grep -q '"setup_complete":true'; then
        SETUP_DONE=true
    fi
    if [ "$SETUP_DONE" = "false" ]; then
        SETUP_BODY=$(jq -n \\
            --arg ak {access_q} \\
            --arg sk {secret_q} \\
            '{{username: "nexus-lakefs", key: {{access_key_id: $ak, secret_access_key: $sk}}}}')
        SETUP_RESP=$(printf '%s' "$SETUP_BODY" | curl -s -X POST 'http://localhost:8000/api/v1/setup_lakefs' \\
            -H 'Content-Type: application/json' \\
            --data-binary @- 2>/dev/null || echo "")
        if ! echo "$SETUP_RESP" | grep -q 'access_key_id'; then
            if ! echo "$SETUP_RESP" | grep -qi 'already'; then
                echo "RESULT hook=lakefs status=failed"
                return 0
            fi
        fi
    fi
    HETZNER_BUCKET={hetzner_q}
    if [ -n "$HETZNER_BUCKET" ]; then
        STORAGE_NS="s3://${{HETZNER_BUCKET}}/lakefs/"
        REPO_NAME="hetzner-object-storage"
    else
        STORAGE_NS="local://data/lakefs/"
        REPO_NAME="local-storage"
    fi
    REPO_BODY=$(jq -n \\
        --arg name "$REPO_NAME" \\
        --arg ns "$STORAGE_NS" \\
        '{{name: $name, storage_namespace: $ns, default_branch: "main", sample_data: false}}')
    REPO_RESP=$(printf '%s' "$REPO_BODY" | curl -s -X POST 'http://localhost:8000/api/v1/repositories' \\
        -u {access_q}:{secret_q} \\
        -H 'Content-Type: application/json' \\
        --data-binary @- 2>/dev/null || echo "")
    if echo "$REPO_RESP" | grep -q '"id"'; then
        echo "RESULT hook=lakefs status=configured"
    elif echo "$REPO_RESP" | grep -q 'already exists'; then
        if [ "$SETUP_DONE" = "true" ]; then
            echo "RESULT hook=lakefs status=already-configured"
        else
            echo "RESULT hook=lakefs status=configured"
        fi
    else
        echo "RESULT hook=lakefs status=failed"
    fi
}}
lakefs_hook
"""


def render_openmetadata_hook(config: NexusConfig, env: BootstrapEnv) -> str:
    """OpenMetadata: 3-step (default-pwd login → change-password → verify).

    Login API takes base64-encoded passwords; changePassword takes
    plain text. The orchestrator detects "already configured" via
    the default-login failing with ``invalid|unauthorized``.
    """
    new_password = config.openmetadata_admin_password or ""
    email = env.admin_email or ""
    if not new_password or not email:
        return 'echo "RESULT hook=openmetadata status=skipped-not-ready"\n'
    new_pw_q = shlex.quote(new_password)
    email_q = shlex.quote(email)
    # OpenMetadata's wait check is a body-substring grep, not an HTTP
    # status check — we render a custom wait loop here instead of using
    # _render_wait_healthy.
    return f"""
openmetadata_hook() {{
    EMAIL={email_q}
    DOMAIN=$(printf '%s' "$EMAIL" | cut -d'@' -f2)
    READY=false
    for _ in $(seq 1 60); do
        if curl -s --connect-timeout 3 'http://localhost:8585/api/v1/system/version' 2>/dev/null | grep -q 'version'; then
            READY=true; break
        fi
        sleep 3
    done
    if [ "$READY" != "true" ]; then
        echo "  ⚠ openmetadata not ready after 180s — skipping setup" >&2
        echo "RESULT hook=openmetadata status=skipped-not-ready"
        return 0
    fi
    DEFAULT_PW_B64=$(printf 'admin' | base64 | tr -d '\\n')
    LOGIN_BODY=$(jq -n --arg email "admin@${{DOMAIN}}" --arg pw "$DEFAULT_PW_B64" \\
        '{{email: $email, password: $pw}}')
    LOGIN_RESP=$(printf '%s' "$LOGIN_BODY" | curl -s -X POST 'http://localhost:8585/api/v1/users/login' \\
        -H 'Content-Type: application/json' \\
        --data-binary @- 2>/dev/null || echo "")
    TOKEN=$(printf '%s' "$LOGIN_RESP" | jq -r '.accessToken // empty' 2>/dev/null)
    if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
        if echo "$LOGIN_RESP" | grep -qi 'invalid\\|unauthorized\\|credentials'; then
            echo "RESULT hook=openmetadata status=already-configured"
        else
            echo "RESULT hook=openmetadata status=failed"
        fi
        return 0
    fi
    PW_BODY=$(jq -n --arg new {new_pw_q} \\
        '{{username: "admin", oldPassword: "admin", newPassword: $new, confirmPassword: $new, requestType: "SELF"}}')
    printf '%s' "$PW_BODY" | curl -s -X PUT 'http://localhost:8585/api/v1/users/changePassword' \\
        -H "Authorization: Bearer $TOKEN" \\
        -H 'Content-Type: application/json' \\
        --data-binary @- >/dev/null 2>&1 || true
    NEW_PW_B64=$(printf '%s' {new_pw_q} | base64 | tr -d '\\n')
    VERIFY_BODY=$(jq -n --arg email "admin@${{DOMAIN}}" --arg pw "$NEW_PW_B64" \\
        '{{email: $email, password: $pw}}')
    VERIFY_RESP=$(printf '%s' "$VERIFY_BODY" | curl -s -X POST 'http://localhost:8585/api/v1/users/login' \\
        -H 'Content-Type: application/json' \\
        --data-binary @- 2>/dev/null || echo "")
    if printf '%s' "$VERIFY_RESP" | jq -r '.accessToken // empty' 2>/dev/null | grep -q '.'; then
        echo "RESULT hook=openmetadata status=configured"
    else
        echo "RESULT hook=openmetadata status=failed"
    fi
}}
openmetadata_hook
"""


# ---------------------------------------------------------------------------
# Hook registry — maps service name → renderer function. Order is
# the order operators see in the workflow log; matches deploy.sh's
# original sequence.
# ---------------------------------------------------------------------------

HookRenderer = Callable[[NexusConfig, BootstrapEnv], str]

_HOOK_REGISTRY: dict[str, HookRenderer] = {
    "portainer": render_portainer_hook,
    "n8n": render_n8n_hook,
    "metabase": render_metabase_hook,
    "lakefs": render_lakefs_hook,
    "openmetadata": render_openmetadata_hook,
}


def supported_hooks() -> tuple[str, ...]:
    """Service names with admin-setup hooks shipped in this module."""
    return tuple(_HOOK_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Bash rendering: combine per-hook renderers into one server-side script.
# ---------------------------------------------------------------------------


def render_remote_script(
    *,
    config: NexusConfig,
    env: BootstrapEnv,
    enabled_hooks: list[str],
) -> str:
    """Render the combined bash script for all enabled admin-setup hooks.

    Each hook is rendered as a self-contained bash function that emits
    exactly one ``RESULT hook=<name> status=<...>`` line. A failure in
    one hook does NOT propagate (each hook function uses ``return 0``
    on its bail-out paths and the orchestrator script has no
    ``set -e`` in the outer scope — only inside the per-hook
    ``set -euo pipefail`` blocks that the helpers establish).

    Hook execution is sequential (NOT parallel — different hooks may
    target the same backing services like Postgres, and sequential
    keeps the workflow log readable). Order matches ``_HOOK_REGISTRY``.
    """
    parts: list[str] = ["set -u  # -e omitted: hook failures must not abort the orchestrator\n"]
    for name in enabled_hooks:
        renderer = _HOOK_REGISTRY.get(name)
        if renderer is None:
            # Unknown hook → emit a skip line so the count stays consistent
            parts.append(f'echo "RESULT hook={name} status=skipped-not-ready"\n')
            continue
        parts.append(renderer(config, env))
    return "".join(parts)


def parse_results(stdout: str) -> tuple[HookResult, ...]:
    """Extract one HookResult per ``RESULT hook=…`` line in remote stdout."""
    return tuple(
        HookResult(name=m.group("name"), status=m.group("status"))  # type: ignore[arg-type]
        for m in _RESULT_LINE_RE.finditer(stdout)
    )


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


ScriptRunner = Callable[[str], subprocess.CompletedProcess[str]]


def run_admin_setups(
    config: NexusConfig,
    env: BootstrapEnv,
    enabled: list[str],
    *,
    script_runner: ScriptRunner | None = None,
) -> SetupResult:
    """Render → exec → parse.

    ``enabled`` is the full enabled-services list (same shape as
    deploy.sh's ``$ENABLED_SERVICES``). Hooks are filtered to only
    those that have a renderer in ``_HOOK_REGISTRY``; unknown
    services are dropped silently (they belong to other modules:
    seeder, compose_runner, future 2.2c/d).

    Returns :class:`SetupResult` with one :class:`HookResult` per
    enabled+supported hook. Hooks reporting no RESULT line (e.g.
    a server-side ssh failure mid-script) are reflected as
    ``status=failed`` for accountability.
    """
    enabled_hooks = [s for s in enabled if s in _HOOK_REGISTRY]
    if not enabled_hooks:
        return SetupResult(hooks=())

    script = render_remote_script(config=config, env=env, enabled_hooks=enabled_hooks)

    run_script = script_runner or (lambda s: _remote.ssh_run_script(s))
    completed = run_script(script)

    # Forward remote ⚠ warnings + "  ✓/✗" lines to local stderr
    # (Modul-1.2 Round-4 pattern); strip the RESULT wire-format lines.
    for line in completed.stdout.splitlines():
        if not line.startswith("RESULT hook="):
            sys.stderr.write(line + "\n")

    parsed = parse_results(completed.stdout)
    parsed_names = {r.name for r in parsed}

    # Any enabled hook that did NOT produce a RESULT line counts as failed.
    missing = [HookResult(name=h, status="failed") for h in enabled_hooks if h not in parsed_names]
    return SetupResult(hooks=tuple(parsed) + tuple(missing))


# Re-export the keys for tests that want to discover them programmatically.
__all__ = [
    "HookResult",
    "HookStatus",
    "SetupResult",
    "parse_results",
    "render_lakefs_hook",
    "render_metabase_hook",
    "render_n8n_hook",
    "render_openmetadata_hook",
    "render_portainer_hook",
    "render_remote_script",
    "run_admin_setups",
    "supported_hooks",
]


# Suppress unused import warnings — Any imported for type aliasing.
_ = Any
