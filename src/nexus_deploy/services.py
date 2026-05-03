"""Per-service admin-setup hooks (Phase 2 Moduls 2.2b + 2.2c + 2.2d, #505).

Replaces admin-setup blocks in ``scripts/deploy.sh`` (the [7/7]
Auto-configuring services section). Three hook families:

**REST first-init** (Modul 2.2b — Portainer, n8n, Metabase, LakeFS,
OpenMetadata):

  1. Waits for the service container to be HTTP-ready
  2. Optionally checks "already configured" (idempotent skip)
  3. POSTs the admin-init / first-setup payload
  4. Yellow-warns on failure, never aborts

**docker-exec CLI** (Modul 2.2c — RedPanda, Superset):

  1. Waits for the service container to be HTTP-ready
  2. Runs an in-container CLI (``rpk`` for RedPanda, ``superset
     fab`` for Superset) via ``docker exec -i``, with passwords
     piped via stdin to keep them out of docker's argv on the
     remote host
  3. Idempotent re-runs: RedPanda's ``rpk acl user create`` errors
     harmlessly if user exists; Superset falls back to ``fab
     reset-password`` if ``fab create-admin`` reports user-exists

**Python-side file mutation** (Modul 2.2d — Filestash):

  1. Stage 1: rendered bash pulls the container's config.json via
     ``docker exec cat``, base64-encoded over the wire
  2. Python locally mutates the JSON (Pydantic-typed config →
     dict transformations: strip protocol from host, force_ssl=true,
     inject S3 backend connections + middleware)
  3. Stage 2: rendered bash pipes the new config via base64 →
     ``docker exec -i sh -c 'cat > …'`` → ``docker restart`` →
     wait for /healthz again
  This pattern uses TWO ssh round-trips (vs. one for the bash-render
  family). The win: JSON mutation is pure-Python testable, replacing
  a 100-line jq chain with a typed dict transform.

Future admin-setup families:
- 2.2e: Gitea (synchronous, depends on Postgres + seeder)
- Future: SFTPGo, Garage, Windmill, Wikijs, Dify (each has its own
  pattern; migrating piecemeal as time allows)

Why one ssh round-trip with rendered bash (consistent with infisical /
secret_sync / seeder / compose_runner): the curl loop is proven, one
SSH connection vs N, the rendered script is testable as a string,
and Phase 3 (#505 Modul 3.1) replaces the bash rendering wholesale
with paramiko + ``requests``.

Eight rounds of hardening preserved (one regression test per round
in ``tests/unit/test_services.py``):

R1. Orchestrator script begins with ``set -u`` (unset-var detection)
    but **NOT** ``set -e`` — a failed hook MUST not abort the rest
    (see R6). Per-hook bodies stay safe via explicit branches +
    ``|| echo ""`` capture patterns; no ``set -e`` reliance inside
    hooks either. R3 below is the corollary on tmpfile cleanup.
R2. Per-spec healthcheck timeout (Metabase 120s, OpenMetadata 180s,
    LakeFS 60s, Portainer 5s, n8n 60s — NOT a global default).
R3. Per-hook tmpfile cleanup. LakeFS + OpenMetadata create mode-600
    `mktemp` curl-config files (R4 — auth via --config, NOT argv)
    and clean them up via per-hook ``trap ... RETURN`` + explicit
    ``rm -f`` after the curl call. No shared cross-hook tmpfiles
    or EXIT trap; each hook is self-contained. (Portainer + n8n +
    Metabase don't need a tmpfile — their POST endpoints are
    auth-free, only the body is sensitive, and that travels via
    --data-binary @- stdin.)
R4. JSON setup-body built via jq with secrets injected as env vars
    (``NEXUS_P=value jq -n 'env.NEXUS_P'``), NOT positional
    ``--arg`` values that would land in jq's argv (visible via
    ``ps``). The body is then fed to curl via stdin
    (``--data-binary @-``) so neither jq nor curl carry secrets in
    argv. Auth headers / basic-auth go via ``curl --config <tmpfile>``
    (mode 600, RETURN-trap cleanup) — never via ``-H`` / ``-u``
    argv either. Together: no fork visible via ``ps -ef`` carries
    a credential value.
R5. Idempotent skip when ``already_configured_substring`` appears in
    the pre-setup probe response (deploy.sh's ``[ -z "$SETUP_TOKEN" ]``
    / ``"setup_complete":true`` / etc. branches).
R6. error_strategy=continue: a failed hook NEVER aborts the orchestrator;
    the next hook still runs. Mirrors deploy.sh's ``yellow-warn,
    continue`` pattern.
R7. Hook execution order matches the caller-provided ``enabled_hooks``
    argument (NOT registry insertion order). Operators get the order
    they typed.
R8. RESULT-line-per-hook: ``RESULT hook=<name> status=<configured|
    already-configured|failed|skipped-not-ready>``. The orchestrator
    parses one line per hook, never grepping for emoji.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from nexus_deploy import _remote
from nexus_deploy.config import NexusConfig
from nexus_deploy.infisical import BootstrapEnv

_RESULT_LINE_RE = re.compile(
    r"^RESULT hook=(?P<name>[A-Za-z0-9_-]+) "
    r"status=(?P<status>configured|already-configured|failed|skipped-not-ready)$",
    re.MULTILINE,
)

# Same alphabet as the RESULT line's `name` group. Used to validate
# hook names from the caller-provided `enabled_hooks` list before
# interpolating into the rendered bash — prevents shell injection
# via $(), backticks, semicolons, etc. if a buggy or adversarial
# caller ever passes a name with shell metacharacters. In production
# `enabled_hooks` comes from deploy.sh's $ENABLED_SERVICES (which
# itself comes from `tofu output -json` keys, all alphanumeric +
# dash), so this is defence in depth.
_VALID_HOOK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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

    Bounded by **wall-clock** (``$SECONDS``), not iteration count.
    Earlier versions used ``for _ in $(seq 1 N)`` with N derived
    from ``timeout_seconds // interval_seconds``, but each iteration
    could spend up to curl's ``--max-time`` waiting for a stalled
    response PLUS ``sleep interval_seconds`` between probes — so a
    "60s" timeout could blow out to ~200s in the worst case while
    still printing the misleading "after 60s" warning. Using
    ``$SECONDS`` keeps the upper bound close to ``timeout_seconds``:
    the worst case is ~``timeout_seconds + curl_max_time +
    interval_seconds`` (the loop can enter at SECONDS=N-1, then
    spend one more probe + sleep before the while-check fires
    again). For the typical Portainer/n8n/Metabase configs that's
    ~+7s; not exact, but bounded and accurate enough for the
    "after Ns — skipping" warning.

    The predicate runs against ``$STATUS`` (HTTP code from curl
    ``-w '%{http_code}'``). Specs that need a body-substring check
    (OpenMetadata's ``grep 'version'``) build a custom inner block
    instead of using this helper.
    """
    return f"""
READY=false
SECONDS=0
while [ "$SECONDS" -lt {timeout_seconds} ]; do
    STATUS=$(curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 5 {shlex.quote(url)} 2>/dev/null || echo "000")
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
    """Portainer first-init: ``POST /api/users/admin/init`` (no auth).

    Secrets reach jq via env vars (``NEXUS_U`` / ``NEXUS_P``) and are
    referenced in the filter as ``env.NEXUS_U`` / ``env.NEXUS_P`` —
    NEVER as positional ``--arg`` values, which would put them in
    jq's argv (visible via ``ps``). The body is then piped to curl
    via stdin (``--data-binary @-``) so neither jq nor curl carry
    secrets in argv (R4).
    """
    del env  # not used; signature uniform across hooks
    username = config.admin_username or "admin"
    password = config.portainer_admin_password or ""
    if not password:
        return 'echo "RESULT hook=portainer status=skipped-not-ready"\n'
    username_q = shlex.quote(username)
    password_q = shlex.quote(password)
    wait = _render_wait_healthy(
        name="portainer",
        url="http://localhost:9090/api/system/status",
        timeout_seconds=5,
        interval_seconds=1,
    )
    return f"""
portainer_hook() {{
    {wait}
    BODY=$(NEXUS_U={username_q} NEXUS_P={password_q} jq -n \\
        '{{Username: env.NEXUS_U, Password: env.NEXUS_P}}')
    RESP=$(printf '%s' "$BODY" | curl -s -X POST 'http://localhost:9090/api/users/admin/init' \\
        --max-time 30 \\
        -H 'Content-Type: application/json' \\
        --data-binary @- 2>/dev/null || echo "")
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
    """n8n owner-setup: ``POST /rest/owner/setup`` (no auth, idempotent via /rest/settings).

    Body built with secrets injected via env vars (``NEXUS_E`` /
    ``NEXUS_P``), referenced in jq's filter as ``env.NEXUS_E`` /
    ``env.NEXUS_P``. The body is then piped to curl via stdin
    (``--data-binary @-``). Neither jq nor curl carry the password
    in argv (R4).
    """
    email = env.admin_email or ""
    password = config.n8n_admin_password or ""
    if not password or not email:
        return 'echo "RESULT hook=n8n status=skipped-not-ready"\n'
    email_q = shlex.quote(email)
    password_q = shlex.quote(password)
    wait = _render_wait_healthy(
        name="n8n",
        url="http://localhost:5678/healthz",
        timeout_seconds=60,
    )
    return f"""
n8n_hook() {{
    {wait}
    SETTINGS=$(curl -s --max-time 10 'http://localhost:5678/rest/settings' 2>/dev/null || echo "{{}}")
    NEEDS_SETUP=$(printf '%s' "$SETTINGS" | jq -r '.data.userManagement.showSetupOnFirstLoad // true | if . then "true" else "false" end' 2>/dev/null || echo "true")
    if [ "$NEEDS_SETUP" = "false" ]; then
        echo "RESULT hook=n8n status=already-configured"
        return 0
    fi
    BODY=$(NEXUS_E={email_q} NEXUS_P={password_q} jq -n \\
        '{{email: env.NEXUS_E, firstName: "Admin", lastName: "User", password: env.NEXUS_P}}')
    RESP=$(printf '%s' "$BODY" | curl -s -X POST 'http://localhost:5678/rest/owner/setup' \\
        --max-time 30 \\
        -H 'Content-Type: application/json' \\
        --data-binary @- 2>/dev/null || echo "")
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
    SETUP_TOKEN=$(curl -s --max-time 10 'http://localhost:3000/api/session/properties' 2>/dev/null \\
        | jq -r '."setup-token" // empty' 2>/dev/null || echo "")
    if [ -z "$SETUP_TOKEN" ]; then
        echo "RESULT hook=metabase status=already-configured"
        return 0
    fi
    BODY=$(NEXUS_TOKEN="$SETUP_TOKEN" NEXUS_E={email_q} NEXUS_P={password_q} jq -n \\
        '{{token: env.NEXUS_TOKEN, user: {{email: env.NEXUS_E, first_name: "Admin", last_name: "User", password: env.NEXUS_P}}, prefs: {{site_name: "Nexus Stack Analytics", allow_tracking: false}}}}')
    RESP=$(printf '%s' "$BODY" | curl -s -X POST 'http://localhost:3000/api/setup' \\
        --max-time 30 \\
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
    # Storage namespace selection mirrors deploy.sh's legacy LakeFS
    # block: ``[ -n "$HETZNER_S3_SERVER" ] && [ -n "$HETZNER_S3_BUCKET" ]``
    # — BOTH must be set. Bucket alone isn't enough because LakeFS
    # also needs the endpoint URL to read/write S3, and a partially
    # configured tofu state (bucket without server) would land us in
    # the s3:// branch with broken connectivity. Both NexusConfig
    # fields are shlex-quoted into the rendered bash as literals, NOT
    # read from a remote env var — keeps the renderer pure.
    hetzner_bucket = config.hetzner_s3_bucket_lakefs or ""
    hetzner_server = config.hetzner_s3_server or ""
    hetzner_bucket_q = shlex.quote(hetzner_bucket)
    hetzner_server_q = shlex.quote(hetzner_server)
    wait = _render_wait_healthy(
        name="lakefs",
        url="http://localhost:8000/api/v1/healthcheck",
        timeout_seconds=60,
    )
    return f"""
lakefs_hook() {{
    {wait}
    CFG=$(curl -s --max-time 10 'http://localhost:8000/api/v1/config' 2>/dev/null || echo "")
    SETUP_DONE=false
    if echo "$CFG" | grep -q '"setup_complete":true'; then
        SETUP_DONE=true
    fi
    if [ "$SETUP_DONE" = "false" ]; then
        SETUP_BODY=$(NEXUS_AK={access_q} NEXUS_SK={secret_q} jq -n \\
            '{{username: "nexus-lakefs", key: {{access_key_id: env.NEXUS_AK, secret_access_key: env.NEXUS_SK}}}}')
        SETUP_RESP=$(printf '%s' "$SETUP_BODY" | curl -s -X POST 'http://localhost:8000/api/v1/setup_lakefs' \\
            --max-time 30 \\
            -H 'Content-Type: application/json' \\
            --data-binary @- 2>/dev/null || echo "")
        if ! echo "$SETUP_RESP" | grep -q 'access_key_id'; then
            if ! echo "$SETUP_RESP" | grep -qi 'already'; then
                echo "RESULT hook=lakefs status=failed"
                return 0
            fi
        fi
    fi
    HETZNER_BUCKET={hetzner_bucket_q}
    HETZNER_SERVER={hetzner_server_q}
    # BOTH must be set to pick the s3:// namespace (matches legacy
    # deploy.sh — bucket alone without endpoint would break read/write).
    if [ -n "$HETZNER_BUCKET" ] && [ -n "$HETZNER_SERVER" ]; then
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
    # R4: basic-auth via curl --config tmpfile, NOT -u user:secret
    # in argv. The tmpfile is mode 600 + cleaned up by a function-
    # scoped RETURN trap (fires when lakefs_hook returns) plus an
    # explicit `rm -f` after the curl call.
    LFS_CFG=$(mktemp)
    chmod 600 "$LFS_CFG"
    trap 'rm -f "$LFS_CFG"' RETURN
    printf 'user = "%s:%s"\\n' {access_q} {secret_q} > "$LFS_CFG"
    REPO_RESP=$(printf '%s' "$REPO_BODY" | curl -s -X POST 'http://localhost:8000/api/v1/repositories' \\
        --config "$LFS_CFG" \\
        --max-time 30 \\
        -H 'Content-Type: application/json' \\
        --data-binary @- 2>/dev/null || echo "")
    rm -f "$LFS_CFG"
    trap - RETURN
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
    # Wall-clock-bounded wait (matches _render_wait_healthy's pattern):
    # each iteration spends up to ~5s in curl + 3s sleep, so an
    # iteration-counted loop would blow well past 180s in the
    # worst case. ``$SECONDS`` caps the real wall-time at 180.
    READY=false
    SECONDS=0
    while [ "$SECONDS" -lt 180 ]; do
        if curl -s --connect-timeout 3 --max-time 5 'http://localhost:8585/api/v1/system/version' 2>/dev/null | grep -q 'version'; then
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
    # NEXUS_PW carries the base64 of "admin" (default OpenMetadata
    # password) — public knowledge, but keep it out of jq's argv
    # uniformly with the other hooks. NEXUS_E is the email address.
    LOGIN_BODY=$(NEXUS_E="admin@${{DOMAIN}}" NEXUS_PW="$DEFAULT_PW_B64" jq -n \\
        '{{email: env.NEXUS_E, password: env.NEXUS_PW}}')
    LOGIN_RESP=$(printf '%s' "$LOGIN_BODY" | curl -s -X POST 'http://localhost:8585/api/v1/users/login' \\
        --max-time 30 \\
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
    PW_BODY=$(NEXUS_NEW={new_pw_q} jq -n \\
        '{{username: "admin", oldPassword: "admin", newPassword: env.NEXUS_NEW, confirmPassword: env.NEXUS_NEW, requestType: "SELF"}}')
    # R4: Bearer token via curl --config tmpfile, NOT -H argv. The
    # tmpfile is mode 600 + cleaned up by a function-scoped RETURN
    # trap (fires when openmetadata_hook returns) plus an explicit
    # `rm -f` after the curl call.
    OM_CFG=$(mktemp)
    chmod 600 "$OM_CFG"
    trap 'rm -f "$OM_CFG"' RETURN
    printf 'header = "Authorization: Bearer %s"\\n' "$TOKEN" > "$OM_CFG"
    printf '%s' "$PW_BODY" | curl -s -X PUT 'http://localhost:8585/api/v1/users/changePassword' \\
        --config "$OM_CFG" \\
        --max-time 30 \\
        -H 'Content-Type: application/json' \\
        --data-binary @- >/dev/null 2>&1 || true
    rm -f "$OM_CFG"
    trap - RETURN
    # base64 reads the new password from stdin (printf is a bash
    # builtin → no fork → no `ps` exposure for the printf).
    NEW_PW_B64=$(printf '%s' {new_pw_q} | base64 | tr -d '\\n')
    # NEXUS_PW carries the base64-of-new-password — even base64 of
    # the password is sensitive (trivially reversible), so we route
    # via env var to keep it out of jq's argv.
    VERIFY_BODY=$(NEXUS_E="admin@${{DOMAIN}}" NEXUS_PW="$NEW_PW_B64" jq -n \\
        '{{email: env.NEXUS_E, password: env.NEXUS_PW}}')
    VERIFY_RESP=$(printf '%s' "$VERIFY_BODY" | curl -s -X POST 'http://localhost:8585/api/v1/users/login' \\
        --max-time 30 \\
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
# ---------------------------------------------------------------------------
# Modul 2.2c — docker-exec hooks: RedPanda, Superset.
#
# Different family from the 5 REST hooks above. Pattern:
#   1. Wait for HTTP healthcheck (mostly via ``docker exec curl`` from
#      inside the container, since some endpoints aren't exposed
#      externally).
#   2. Run an in-container CLI (``rpk`` / ``superset fab``) via
#      ``docker exec -i``, with passwords piped via stdin so they
#      never reach docker's argv on the remote host.
#   3. Idempotent re-runs handled per-hook (RedPanda: rpk's "user
#      exists" error is treated as already-configured; Superset:
#      fab create-admin → fab reset-password fallback).
#
# Why argv-vs-stdin matters for docker exec: the legacy deploy.sh
# acknowledged it didn't hide passwords, using ``docker exec -e
# RPK_PASS='$pass'`` — the env-var literal lands in docker's argv
# on the host. Our migration takes the strictly-more-correct path:
# ``printf '%s' "$pass" | docker exec -i <container> sh -c 'PASS=$(cat); ...'``
# keeps the password on stdin only. The inner CLI ``rpk acl user
# create --password "$PASS"`` still has the password in its argv
# inside the container (visible to other processes in the same
# container), but the OUTER host-level ``ps -ef`` shows just the
# benign sh -c invocation.
# ---------------------------------------------------------------------------


def render_redpanda_hook(config: NexusConfig, env: BootstrapEnv) -> str:
    """RedPanda SASL: ``rpk acl user create`` + ``rpk cluster config set superusers``.

    Wait via ``docker exec redpanda curl -sf /v1/status/ready`` (the
    admin API isn't exposed outside the container; ``-sf`` requires
    a true 2xx status, not just a transport-level success). Password
    reaches the container via stdin → in-container shell var → rpk's
    ``--password`` argv (visible only inside the container, not via
    host ``ps``).

    Idempotency contract — always converges to ``configured``
    (or ``failed`` / ``skipped-not-ready``); NO ``already-configured``
    path. Reasoning:
    - First run: create user → cluster config → restart → verify ✓
    - Re-run with same Infisical password: rpk reports "already exists"
      → delete + recreate (no broker restart, since SASL listener is
      already on) → verify ✓ — ends in ``configured``, not
      ``already-configured``, because we DID re-write state (the
      password was rotated to its current value, even if that
      happens to equal the previous value).
    - Re-run with rotated password: same path as above, password
      now genuinely differs → external clients pick up new credential
      via Infisical sync.

    The delete is gated on the first create-attempt returning
    "already exists" — we never delete a user we haven't proven the
    broker can recreate. A transient broker glitch on the first
    create returns ``failed`` without touching existing state.

    Restart of the broker happens ONLY on first setup
    (``USER_EXISTED=false``). Subsequent rotations don't need it
    because the SASL listener config is a one-time broker-side
    setting; only the credentials change. Restart failure is
    surfaced as ``failed`` (legacy ``|| true`` hid this — listener
    not picking up the SASL change is broken-but-silent).
    """
    del env  # not used; signature uniform across hooks
    password = config.redpanda_admin_password or ""
    if not password:
        return 'echo "RESULT hook=redpanda status=skipped-not-ready"\n'
    password_q = shlex.quote(password)
    return f"""
redpanda_hook() {{
    # Wall-clock-bounded readiness wait (matches Modul 2.2b R2 pattern).
    # `curl -sf` returns non-zero on 4xx/5xx responses (NOT just on
    # transport failures), so the loop only breaks on a true 200 OK
    # — earlier `curl -s` would have broken on a 503 too, letting the
    # SASL setup run while RedPanda was still "not ready".
    READY=false
    SECONDS=0
    while [ "$SECONDS" -lt 60 ]; do
        if docker exec redpanda curl -sf --connect-timeout 2 --max-time 5 'http://localhost:9644/v1/status/ready' >/dev/null 2>&1; then
            READY=true; break
        fi
        sleep 2
    done
    if [ "$READY" != "true" ]; then
        echo "  ⚠ redpanda admin API not ready after 60s — skipping SASL setup" >&2
        echo "RESULT hook=redpanda status=skipped-not-ready"
        return 0
    fi
    # Try create-first. Three outcomes:
    #   1. SUCCESS → fresh install, USER_EXISTED stays false (→ restart needed below)
    #   2. "already exists" → rotation case: delete the current user
    #      and recreate with the current Infisical password. We only
    #      open the no-user window AFTER the first create proved the
    #      broker accepts our request, so a transient broker glitch
    #      can't leave us userless mid-flight.
    #   3. Other error → bail with failed.
    # Pipe password via stdin so it never reaches docker exec's argv
    # on the host. Inside the container, `cat` consumes the full
    # stdin into RPK_PASS; rpk then receives it via shell var
    # expansion (still in argv inside the container — different
    # threat model).
    REDPANDA_PASSWORD={password_q}
    USER_EXISTED=false
    USER_RESULT=$(printf '%s' "$REDPANDA_PASSWORD" | \\
        docker exec -i redpanda sh -c 'RPK_PASS=$(cat); rpk acl user create nexus-redpanda --password "$RPK_PASS" --mechanism SCRAM-SHA-256' 2>&1 || echo "")
    if echo "$USER_RESULT" | grep -qi 'already exists\\|user already\\|already in use'; then
        # Rotation path: delete + recreate. Brief no-user window —
        # acceptable because we just proved the broker is responsive.
        # Legacy deploy.sh skipped this entirely, leaving Infisical
        # rotation silently broken.
        USER_EXISTED=true
        docker exec redpanda rpk acl user delete nexus-redpanda >/dev/null 2>&1 || true
        USER_RESULT=$(printf '%s' "$REDPANDA_PASSWORD" | \\
            docker exec -i redpanda sh -c 'RPK_PASS=$(cat); rpk acl user create nexus-redpanda --password "$RPK_PASS" --mechanism SCRAM-SHA-256' 2>&1 || echo "")
        if ! echo "$USER_RESULT" | grep -qi 'created\\|added\\|success'; then
            echo "  ⚠ rpk acl user create failed after delete (no SASL user — broker is now in a broken state): $USER_RESULT" >&2
            echo "RESULT hook=redpanda status=failed"
            return 0
        fi
    elif ! echo "$USER_RESULT" | grep -qi 'created\\|added\\|success'; then
        echo "  ⚠ rpk acl user create failed: $USER_RESULT" >&2
        echo "RESULT hook=redpanda status=failed"
        return 0
    fi
    # rpk cluster config set: superusers list. Capture the result so
    # we can fail loudly — without this, the user has no permissions
    # and the broker rejects every ACL-protected operation. Legacy
    # deploy.sh swallowed errors here, which could mark the hook
    # `configured` even when the cluster config update failed.
    SUPER_RESULT=$(docker exec redpanda rpk cluster config set superusers '["nexus-redpanda"]' 2>&1 || echo "")
    if ! echo "$SUPER_RESULT" | grep -qi 'success\\|updated\\|set'; then
        echo "  ⚠ rpk cluster config set superusers failed: $SUPER_RESULT" >&2
        echo "RESULT hook=redpanda status=failed"
        return 0
    fi
    # Restart only on FIRST setup. SASL listener config is set on the
    # broker side once and stays applied across rotations, so a
    # password-only change doesn't need a restart. Legacy deploy.sh
    # restarted unconditionally on every spin-up — harmless when the
    # broker had no traffic but introduces a multi-second window
    # where producers/consumers reconnect for no reason.
    if [ "$USER_EXISTED" = "false" ]; then
        # First-setup restart: the SASL listener config takes effect
        # only after a broker restart. Capture the exit code — if the
        # restart fails (network/disk/compose issue), the listener
        # never picks up the SASL change and external clients can't
        # authenticate, even though the user exists. Legacy `|| true`
        # would have hidden this.
        RESTART_RC=0
        if [ -f /opt/docker-server/stacks/redpanda/docker-compose.firewall.yml ]; then
            ( cd /opt/docker-server/stacks/redpanda && docker compose -f docker-compose.yml -f docker-compose.firewall.yml restart >/dev/null 2>&1 ) || RESTART_RC=$?
        else
            ( cd /opt/docker-server/stacks/redpanda && docker compose restart >/dev/null 2>&1 ) || RESTART_RC=$?
        fi
        if [ "$RESTART_RC" -ne 0 ]; then
            echo "  ⚠ docker compose restart redpanda failed (rc=$RESTART_RC) — SASL listener config not applied" >&2
            echo "RESULT hook=redpanda status=failed"
            return 0
        fi
        sleep 5
        # Wait for restart-readiness. `curl -sf` for proper status check.
        SECONDS=0
        while [ "$SECONDS" -lt 30 ]; do
            if docker exec redpanda curl -sf --connect-timeout 2 --max-time 5 'http://localhost:9644/v1/status/ready' >/dev/null 2>&1; then break; fi
            sleep 2
        done
    fi
    # Verify the user is in place after all state changes. `curl -sf`
    # → if the admin API is still not-ready, the verify probe fails
    # and we report `failed` (NOT a false-positive `configured`).
    USERS=$(docker exec redpanda curl -sf --max-time 10 'http://localhost:9644/v1/security/users' 2>/dev/null || echo "[]")
    if echo "$USERS" | grep -q 'nexus-redpanda'; then
        echo "RESULT hook=redpanda status=configured"
    else
        echo "RESULT hook=redpanda status=failed"
    fi
}}
redpanda_hook
"""


def render_superset_hook(config: NexusConfig, env: BootstrapEnv) -> str:
    """Superset admin setup: ``superset fab create-admin`` (with reset-password fallback).

    Wait via ``/health`` substring grep ('OK'). Both ``fab create-admin``
    and ``fab reset-password`` accept ``--password "$VAR"`` — the
    password reaches the in-container shell via stdin, then is
    expanded as the inner argv (visible inside the container only,
    not via host ``ps``). Idempotent re-run: if create-admin reports
    user-exists, fall back to reset-password.
    """
    password = config.superset_admin_password or ""
    email = env.admin_email or ""
    if not password or not email:
        return 'echo "RESULT hook=superset status=skipped-not-ready"\n'
    password_q = shlex.quote(password)
    email_q = shlex.quote(email)
    return f"""
superset_hook() {{
    # Wall-clock-bounded readiness wait. Superset is slow on first
    # boot (db upgrade + init) — generous 5min timeout.
    READY=false
    SECONDS=0
    while [ "$SECONDS" -lt 300 ]; do
        if curl -s --connect-timeout 2 --max-time 5 'http://localhost:8089/health' 2>/dev/null | grep -q 'OK'; then
            READY=true; break
        fi
        sleep 5
    done
    if [ "$READY" != "true" ]; then
        echo "  ⚠ superset not ready after 5min — skipping admin setup" >&2
        echo "RESULT hook=superset status=skipped-not-ready"
        return 0
    fi
    # Pass password via stdin → in-container PASS var → fab argv. The
    # email is non-secret, so it goes via -e (host argv, but harmless).
    SUPERSET_PASSWORD={password_q}
    ADMIN_EMAIL={email_q}
    CREATE_RESULT=$(printf '%s' "$SUPERSET_PASSWORD" | \\
        docker exec -i -e ADMIN_EMAIL="$ADMIN_EMAIL" superset \\
        sh -c 'PASS=$(cat); superset fab create-admin --username admin --email "$ADMIN_EMAIL" --firstname Superset --lastname Admin --password "$PASS"' 2>&1 || echo "")
    if echo "$CREATE_RESULT" | grep -qi 'created\\|added'; then
        echo "RESULT hook=superset status=configured"
        return 0
    fi
    # Fallback: fab reset-password for the existing admin user.
    RESET_RESULT=$(printf '%s' "$SUPERSET_PASSWORD" | \\
        docker exec -i superset \\
        sh -c 'PASS=$(cat); superset fab reset-password --username admin --password "$PASS"' 2>&1 || echo "")
    if echo "$RESET_RESULT" | grep -qi 'reset\\|changed\\|success'; then
        echo "RESULT hook=superset status=already-configured"
    else
        echo "RESULT hook=superset status=failed"
    fi
}}
superset_hook
"""


# ---------------------------------------------------------------------------
# Modul 2.2d — Filestash (Python-side file mutation).
#
# Filestash stores its admin-side state in a JSON file inside the
# container at ``/app/data/state/config/config.json``. Three things
# need fixing post-startup:
# 1. ``general.host`` defaults to the public URL with ``https://``
#    prefix — but Filestash treats that as a literal protocol marker
#    and breaks signed URLs unless we strip the prefix.
# 2. ``general.force_ssl`` defaults to ``null``/``false`` — must be
#    ``true`` to honour the Cloudflare-Access-only access pattern.
# 3. S3 backends (R2 / Hetzner / external) need to be injected as
#    pre-configured connections so admins don't need to re-enter
#    credentials in Filestash's web UI on first login.
#
# Legacy deploy.sh did all three via a chain of jq invocations
# server-side. The Python migration pulls the JSON, mutates with
# typed Python dict transforms, and pushes back. Two ssh round-trips,
# pure mutation logic, fully testable without any I/O.
# ---------------------------------------------------------------------------


_FILESTASH_CONFIG_PATH = "/app/data/state/config/config.json"

# Marker tokens emitted by the pull-stage script. Both are
# distinct prefixes so no JSON content can collide with them
# (JSON can't start with whitespace-followed-by-uppercase-RESULT).
_FILESTASH_PULL_OK = "RESULT_PULL_OK"
_FILESTASH_PULL_NOT_READY = "RESULT_PULL_NOT_READY"
_FILESTASH_PULL_NO_CONFIG = "RESULT_PULL_NO_CONFIG"


def _filestash_has_r2(config: NexusConfig) -> bool:
    """All four R2 fields populated."""
    return bool(
        config.r2_data_endpoint
        and config.r2_data_access_key
        and config.r2_data_secret_key
        and config.r2_data_bucket,
    )


def _filestash_has_hetzner(config: NexusConfig) -> bool:
    return bool(
        config.hetzner_s3_server
        and config.hetzner_s3_access_key
        and config.hetzner_s3_secret_key
        and config.hetzner_s3_bucket_general,
    )


def _filestash_has_external(config: NexusConfig) -> bool:
    return bool(
        config.external_s3_endpoint
        and config.external_s3_access_key
        and config.external_s3_secret_key
        and config.external_s3_bucket,
    )


def _filestash_s3_connections(config: NexusConfig) -> list[dict[str, str]]:
    """Build the ``connections`` array.

    Order matches deploy.sh: R2 → Hetzner → External. The first one
    becomes the primary backend (see :func:`_filestash_primary_backend`).
    """
    out: list[dict[str, str]] = []
    if _filestash_has_r2(config):
        out.append({"type": "s3", "label": "R2 Datalake"})
    if _filestash_has_hetzner(config):
        out.append({"type": "s3", "label": "Hetzner Storage"})
    if _filestash_has_external(config):
        out.append({"type": "s3", "label": config.external_s3_label or "External Storage"})
    return out


def _filestash_s3_params(config: NexusConfig) -> dict[str, dict[str, str]]:
    """Build the per-backend params map keyed by label.

    Endpoints are normalised: deploy.sh stores ``$HETZNER_S3_SERVER``
    without scheme but Filestash needs a full URL, so we prefix
    ``https://``. R2 + external endpoints already include scheme.
    """
    out: dict[str, dict[str, str]] = {}
    if _filestash_has_r2(config):
        out["R2 Datalake"] = {
            "type": "s3",
            "access_key_id": config.r2_data_access_key or "",
            "secret_access_key": config.r2_data_secret_key or "",
            "endpoint": config.r2_data_endpoint or "",
            "region": "auto",
            "path": f"/{config.r2_data_bucket}/",
        }
    if _filestash_has_hetzner(config):
        out["Hetzner Storage"] = {
            "type": "s3",
            "access_key_id": config.hetzner_s3_access_key or "",
            "secret_access_key": config.hetzner_s3_secret_key or "",
            "endpoint": f"https://{config.hetzner_s3_server}",
            "region": config.hetzner_s3_region or "",
            "path": f"/{config.hetzner_s3_bucket_general}/",
        }
    if _filestash_has_external(config):
        label = config.external_s3_label or "External Storage"
        out[label] = {
            "type": "s3",
            "access_key_id": config.external_s3_access_key or "",
            "secret_access_key": config.external_s3_secret_key or "",
            "endpoint": config.external_s3_endpoint or "",
            "region": config.external_s3_region or "auto",
            "path": f"/{config.external_s3_bucket}/",
        }
    return out


def _filestash_primary_backend(config: NexusConfig) -> str | None:
    """First populated backend label, or None if no S3 backend is set up."""
    if _filestash_has_r2(config):
        return "R2 Datalake"
    if _filestash_has_hetzner(config):
        return "Hetzner Storage"
    if _filestash_has_external(config):
        return config.external_s3_label or "External Storage"
    return None


def _filestash_mutate_config(
    existing: dict[str, Any],
    *,
    config: NexusConfig,
) -> dict[str, Any]:
    """Apply the three transforms to a parsed config.json dict.

    Returns a NEW dict (does not mutate ``existing``) so callers can
    snapshot pre/post for diffing. Legacy deploy.sh used in-place
    ``sed`` + ``jq`` and could leave half-written state on jq
    failures; the Python path's all-or-nothing semantics are stricter.
    """
    out: dict[str, Any] = json.loads(json.dumps(existing))  # deep copy

    general = out.setdefault("general", {})
    if isinstance(general, dict):
        host = general.get("host")
        if isinstance(host, str) and host.startswith("https://"):
            general["host"] = host[len("https://") :]
        general["force_ssl"] = True

    primary = _filestash_primary_backend(config)
    if primary is not None:
        out["connections"] = _filestash_s3_connections(config)
        params = _filestash_s3_params(config)
        # Filestash wants the middleware param values as JSON STRINGS,
        # not nested objects. This is the source of one of the original
        # PR's bug-classes — a missing tojson would parse but break the
        # admin UI on render. Pin via test snapshots.
        middleware = out.setdefault("middleware", {})
        if isinstance(middleware, dict):
            middleware["identity_provider"] = {
                "type": "passthrough",
                "params": json.dumps({"strategy": "direct"}),
            }
            middleware["attribute_mapping"] = {
                "related_backend": primary,
                "params": json.dumps(params),
            }

    return out


def _render_filestash_pull_script() -> str:
    """Stage 1: wait for filestash → wait for config.json → pull as base64.

    Emits exactly one of three marker lines so the Python-side parser
    knows what happened:
    - ``RESULT_PULL_NOT_READY`` — service never came up in 45s
    - ``RESULT_PULL_NO_CONFIG`` — service up but config.json absent
    - ``RESULT_PULL_OK <base64>`` — config.json captured

    The base64-encoding step keeps any binary bytes / newlines /
    quotes in config.json from breaking the line-based wire format
    on stdout.
    """
    return f"""
set -u
READY=false
SECONDS=0
while [ "$SECONDS" -lt 45 ]; do
    if curl -sf --connect-timeout 2 --max-time 5 \\
        'http://localhost:8334/healthz' >/dev/null 2>&1; then
        READY=true; break
    fi
    sleep 3
done
if [ "$READY" != "true" ]; then
    echo "  ⚠ filestash not ready after 45s — skipping setup" >&2
    echo "{_FILESTASH_PULL_NOT_READY}"
    exit 0
fi

CONFIG_PRESENT=false
SECONDS=0
while [ "$SECONDS" -lt 30 ]; do
    if docker exec filestash test -f {shlex.quote(_FILESTASH_CONFIG_PATH)} \\
        >/dev/null 2>&1; then
        CONFIG_PRESENT=true; break
    fi
    sleep 3
done
if [ "$CONFIG_PRESENT" != "true" ]; then
    echo "  ⚠ filestash config.json absent after 30s — skipping" >&2
    echo "{_FILESTASH_PULL_NO_CONFIG}"
    exit 0
fi

# base64 with -w0 (no-wrap) isn't on macOS / Alpine; pipe through `tr` instead.
CONFIG_B64=$(docker exec filestash cat {shlex.quote(_FILESTASH_CONFIG_PATH)} \\
    2>/dev/null | base64 | tr -d '\\n' || echo "")
if [ -z "$CONFIG_B64" ]; then
    echo "  ⚠ filestash config.json empty / unreadable" >&2
    echo "{_FILESTASH_PULL_NO_CONFIG}"
    exit 0
fi
echo "{_FILESTASH_PULL_OK} $CONFIG_B64"
"""


def _render_filestash_push_script(*, new_config_b64: str) -> str:
    """Stage 2: push base64'd config → restart → wait for /healthz.

    R4 — keep the base64 (and therefore the encoded S3 secret material)
    OUT of argv on the remote host. We feed the base64 string into
    ``base64 -d`` via heredoc on stdin (``cat << 'NEXUS_FS_PUSH_EOF'
    | base64 -d | docker exec -i …``), NOT as a positional argument to
    ``printf``. Heredoc bodies are written to the child's stdin by the
    bash shell directly; no fork in the pipeline carries the secret in
    argv visible to ``ps -ef`` on the nexus host.

    The single-quoted heredoc delimiter (``'NEXUS_FS_PUSH_EOF'``)
    disables variable expansion inside, so any ``$``-shaped bytes that
    coincidentally appear in base64 are not interpreted. The base64
    alphabet is ``[A-Za-z0-9+/=]`` — no underscores, no E-O-F sequence
    on its own line is reachable from a continuous (newline-stripped)
    base64 string — but we still defensively assert the delimiter
    doesn't appear inside the payload as a defence-in-depth check.

    pipefail is ON: a failure anywhere in the pipeline (missing
    ``base64`` binary, corrupt input, ``docker exec -i`` rejecting
    stdin) propagates to the if-branch's exit status. Without it the
    pipeline's status is just the last command's, which would mask a
    silent empty-write into config.json.

    Emits ``RESULT hook=filestash status=configured`` on success or
    ``status=failed`` if either the write or the post-restart
    healthcheck fails.
    """
    # Defensive guard: base64 alphabet check. If a future caller
    # supplies non-base64 content we want to fail loudly here, not
    # ship a broken script that the operator has to debug remotely.
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", new_config_b64):
        raise ValueError("new_config_b64 contains characters outside the base64 alphabet")
    # Defence in depth — unreachable as long as the base64 alphabet
    # check above stands (b64 alphabet excludes underscores, the
    # delimiter contains them). Kept as a tripwire for any future
    # widening of the accepted alphabet that would invalidate the
    # collision-impossibility argument.
    delimiter = "NEXUS_FS_PUSH_EOF"
    if delimiter in new_config_b64:  # pragma: no cover
        raise ValueError(f"heredoc delimiter {delimiter!r} appears inside the payload")

    return f"""
set -u
set -o pipefail  # ANY pipeline-stage failure → non-zero exit, NOT just the last
cat <<'{delimiter}' 2>/dev/null | base64 -d 2>/dev/null | \\
    docker exec -i filestash sh -c 'cat > {shlex.quote(_FILESTASH_CONFIG_PATH)}' \\
    2>/dev/null
{new_config_b64}
{delimiter}
WRITE_RC=$?
if [ "$WRITE_RC" -ne 0 ]; then
    echo "  ✗ filestash config write failed (rc=$WRITE_RC)" >&2
    echo "RESULT hook=filestash status=failed"
    exit 0
fi

if ! docker restart filestash >/dev/null 2>&1; then
    echo "  ✗ filestash restart failed" >&2
    echo "RESULT hook=filestash status=failed"
    exit 0
fi

# Wait for /healthz after restart. Bounded at 30s — restart is
# typically <10s on cax31; longer than that means something is wrong.
RESTARTED=false
SECONDS=0
while [ "$SECONDS" -lt 30 ]; do
    if curl -sf --connect-timeout 2 --max-time 5 \\
        'http://localhost:8334/healthz' >/dev/null 2>&1; then
        RESTARTED=true; break
    fi
    sleep 2
done
if [ "$RESTARTED" != "true" ]; then
    echo "  ✗ filestash not ready 30s after restart" >&2
    echo "RESULT hook=filestash status=failed"
    exit 0
fi
echo "RESULT hook=filestash status=configured"
"""


def _parse_filestash_pull_output(stdout: str) -> dict[str, Any] | None | Literal["not-ready"]:
    """Decode the pull-stage marker line into one of three states.

    Return value:
    - ``"not-ready"`` — readiness probe didn't pass in time, OR
      config.json is absent (treated identically as ``skipped-not-ready``).
    - ``None`` — pull marker line malformed (parse error, treat as failure).
    - ``dict`` — successfully decoded config.json content.
    """
    for line in stdout.splitlines():
        if line in (_FILESTASH_PULL_NOT_READY, _FILESTASH_PULL_NO_CONFIG):
            return "not-ready"
        if line.startswith(_FILESTASH_PULL_OK + " "):
            b64 = line[len(_FILESTASH_PULL_OK) + 1 :].strip()
            try:
                raw = base64.b64decode(b64, validate=True)
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                return None
            if not isinstance(parsed, dict):
                return None
            return parsed
    return None


def configure_filestash(
    config: NexusConfig,
    *,
    script_runner: ScriptRunner | None = None,
) -> HookResult:
    """End-to-end Filestash admin setup.

    Two SSH round-trips, with Python-side JSON mutation between them.
    Failure at any stage maps to ``status=failed`` (NOT ``not-ready``)
    EXCEPT for the explicit "not ready" / "no config" markers from
    stage 1 which are pre-setup states, not failures.

    ``script_runner`` defaults to :func:`_remote.ssh_run_script` so
    tests can substitute a mock; the production caller
    (``run_admin_setups``) passes the same callable through.
    """
    runner = script_runner or _remote.ssh_run_script

    # Stage 1: pull
    out1 = runner(_render_filestash_pull_script())
    pulled = _parse_filestash_pull_output(out1.stdout)
    if pulled == "not-ready":
        return HookResult(name="filestash", status="skipped-not-ready")
    if pulled is None:
        sys.stderr.write("  ✗ filestash pull stage produced no parseable result\n")
        return HookResult(name="filestash", status="failed")

    # Stage 2: mutate locally
    new_config = _filestash_mutate_config(pulled, config=config)
    new_b64 = base64.b64encode(json.dumps(new_config).encode("utf-8")).decode("ascii")

    # Stage 3: push + restart + wait
    out2 = runner(_render_filestash_push_script(new_config_b64=new_b64))
    if "RESULT hook=filestash status=configured" in out2.stdout:
        return HookResult(name="filestash", status="configured")
    return HookResult(name="filestash", status="failed")


# ScriptRunner forward reference for type hints above (defined in
# the orchestration section below; the runtime alias is set there).
ScriptRunner = Callable[[str], "subprocess.CompletedProcess[str]"]


# ---------------------------------------------------------------------------
# Hook registry — maps service name → renderer function. NOT the
# execution-order source of truth — render_remote_script iterates the
# caller-provided ``enabled_hooks`` list, so the operator (or the CLI
# parser) controls the order. The dict insertion order here is only a
# debugging convenience (``supported_hooks()`` returns it).
# ---------------------------------------------------------------------------

HookRenderer = Callable[[NexusConfig, BootstrapEnv], str]

_HOOK_REGISTRY: dict[str, HookRenderer] = {
    # Modul 2.2b — REST first-init hooks
    "portainer": render_portainer_hook,
    "n8n": render_n8n_hook,
    "metabase": render_metabase_hook,
    "lakefs": render_lakefs_hook,
    "openmetadata": render_openmetadata_hook,
    # Modul 2.2c — docker-exec CLI hooks
    "redpanda": render_redpanda_hook,
    "superset": render_superset_hook,
}


# Modul 2.2d Python-side hooks — separate registry because their
# orchestration shape differs from bash renderers: they need to issue
# multiple SSH round-trips with Python-side mutation in between.
PythonHookFn = Callable[[NexusConfig, ScriptRunner], HookResult]


def _filestash_python_hook(config: NexusConfig, runner: ScriptRunner) -> HookResult:
    """Adapter: pin the (config, runner) signature for the registry."""
    return configure_filestash(config, script_runner=runner)


_PYTHON_HOOK_REGISTRY: dict[str, PythonHookFn] = {
    "filestash": _filestash_python_hook,
}

# Single-source-of-truth invariant: a name lives in exactly one registry.
# A name in both would silently double-dispatch in run_admin_setups (one
# bash run + one python run). Checked at import time so any future
# refactor that violates the invariant fails the test suite, not
# production. If you genuinely need cross-registry routing, route via a
# wrapper function that lives in only one registry.
if _overlap := set(_HOOK_REGISTRY) & set(_PYTHON_HOOK_REGISTRY):
    raise RuntimeError(f"hook names in both registries: {sorted(_overlap)}")


def supported_hooks() -> tuple[str, ...]:
    """All service names with admin-setup hooks (bash + python families).

    Order: bash-registry insertion order, then python-registry insertion
    order. ``dict.fromkeys`` preserves order while de-duplicating —
    redundant given the import-time invariant above, but defence in depth
    if a future refactor weakens that assertion.
    """
    return tuple(dict.fromkeys((*_HOOK_REGISTRY, *_PYTHON_HOOK_REGISTRY)))


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
    ``set -e`` in the outer scope).

    Hook execution is sequential (NOT parallel). **Order matches the
    caller-provided ``enabled_hooks`` argument** — NOT
    ``_HOOK_REGISTRY`` insertion order. Callers (the CLI in
    ``__main__._services_configure``) determine the order; the
    registry is only a name → renderer map.

    KNOWN-LIMITATION (acknowledged tradeoff vs legacy deploy.sh):
    legacy ran several hooks (Portainer, LakeFS, OpenMetadata) in
    parallel via ``( ... ) & CONFIG_JOBS+=($!)`` background subshells
    + ``wait``, which capped wall-time at the slowest hook (~180s).
    Sequential here can reach ~``sum(per-hook timeouts)`` — about
    7 minutes worst case for the 5 currently-shipped hooks. Phase 3
    (#505 Modul 3.1, paramiko + asyncio) replaces the bash-render
    layer wholesale and naturally restores parallelism via
    ``asyncio.gather``. Until then we accept the increased wall-
    time in exchange for predictable, easy-to-grep linear logs.
    """
    parts: list[str] = ["set -u  # -e omitted: hook failures must not abort the orchestrator\n"]
    for name in enabled_hooks:
        # Defence in depth: drop any hook name with shell-meta chars
        # before interpolating into the rendered bash. Logged to local
        # stderr (NOT into the rendered script — we cannot trust the
        # value enough to embed it). Production callers should never
        # hit this path; deploy.sh's $ENABLED_SERVICES is alphanumeric
        # + dash by tofu-output construction.
        if not _VALID_HOOK_NAME_RE.fullmatch(name):
            sys.stderr.write(f"  ⚠ Dropped hook with unsafe name: {name!r}\n")
            continue
        renderer = _HOOK_REGISTRY.get(name)
        if renderer is None:
            # Unknown but well-formed hook → emit a skip line so the
            # operator can see the name in the workflow log.
            parts.append(f'echo "RESULT hook={name} status=skipped-not-ready"\n')
            continue
        parts.append(renderer(config, env))
    return "".join(parts)


def parse_results(stdout: str) -> tuple[HookResult, ...]:
    """Extract one HookResult per ``RESULT hook=…`` line in remote stdout.

    Both regex groups (``name``, ``status``) are required by
    ``_RESULT_LINE_RE`` — ``finditer`` only yields matches where
    every group captured something — so ``m.group(name)`` is
    statically guaranteed non-None. The ``cast`` pins the status
    string to its Literal-typed alias for the typed dataclass
    constructor (replaces the previous ``# type: ignore[arg-type]``
    suppression).
    """
    return tuple(
        HookResult(name=m.group("name"), status=cast("HookStatus", m.group("status")))
        for m in _RESULT_LINE_RE.finditer(stdout)
    )


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


def run_admin_setups(
    config: NexusConfig,
    env: BootstrapEnv,
    enabled: list[str],
    *,
    script_runner: ScriptRunner | None = None,
) -> SetupResult:
    """Render → exec → parse, dispatching to bash or python hook family.

    ``enabled`` is the full enabled-services list (same shape as
    deploy.sh's ``$ENABLED_SERVICES``). Hooks are filtered to those
    that have an entry in either ``_HOOK_REGISTRY`` (bash-rendered)
    or ``_PYTHON_HOOK_REGISTRY`` (Python-side, e.g. Filestash);
    unknown services are dropped silently (they belong to other
    modules: seeder, compose_runner, future hooks).

    Returns :class:`SetupResult` with one :class:`HookResult` per
    enabled+supported hook. Bash hooks that report no RESULT line
    (e.g. a server-side ssh failure mid-script) are reflected as
    ``status=failed`` for accountability.
    """
    bash_hooks = [s for s in enabled if s in _HOOK_REGISTRY]
    py_hooks = [s for s in enabled if s in _PYTHON_HOOK_REGISTRY]
    if not bash_hooks and not py_hooks:
        return SetupResult(hooks=())

    runner = script_runner or (lambda s: _remote.ssh_run_script(s))

    bash_results: tuple[HookResult, ...] = ()
    if bash_hooks:
        script = render_remote_script(config=config, env=env, enabled_hooks=bash_hooks)
        completed = runner(script)
        # Forward remote ⚠ warnings + "  ✓/✗" lines to local stderr
        # (Modul-1.2 Round-4 pattern); strip the RESULT wire-format lines.
        for line in completed.stdout.splitlines():
            if not line.startswith("RESULT hook="):
                sys.stderr.write(line + "\n")
        parsed = parse_results(completed.stdout)
        parsed_names = {r.name for r in parsed}
        # Any enabled bash-hook with no RESULT line counts as failed.
        missing = tuple(
            HookResult(name=h, status="failed") for h in bash_hooks if h not in parsed_names
        )
        bash_results = tuple(parsed) + missing

    py_results: list[HookResult] = []
    for name in py_hooks:
        hook_fn = _PYTHON_HOOK_REGISTRY[name]
        py_results.append(hook_fn(config, runner))

    return SetupResult(hooks=bash_results + tuple(py_results))


# Re-export the keys for tests that want to discover them programmatically.
__all__ = [
    "HookResult",
    "HookStatus",
    "SetupResult",
    "configure_filestash",
    "parse_results",
    "render_lakefs_hook",
    "render_metabase_hook",
    "render_n8n_hook",
    "render_openmetadata_hook",
    "render_portainer_hook",
    "render_redpanda_hook",
    "render_remote_script",
    "render_superset_hook",
    "run_admin_setups",
    "supported_hooks",
]
