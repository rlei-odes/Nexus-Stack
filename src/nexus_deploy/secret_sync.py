"""Per-stack ``.infisical.env`` sync from Infisical (Phase 1, #505 Modul 1.2).

Replaces the two ~300-line heredoc blocks in ``scripts/deploy.sh`` that
fetched Infisical secrets and wrote them to
``/opt/docker-server/stacks/{jupyter,marimo}/.infisical.env``. Both
blocks were functionally identical — only stack-name + paths differed.
Migration collapses them to one parametrised :class:`StackTarget` plus
a single rendering layer, executed remotely via
:func:`_remote.ssh_run_script` (script via stdin, NOT argv, so the
Infisical token can't leak through ``ps`` / CI logs / exception messages).

Why render bash and exec server-side instead of doing it all in Python:
the curl + jq + sed + atomic-mv pipeline has been hardened across 8 rounds
of post-#495 fixes; one SSH round-trip vs ~80 small HTTP roundtrips
matters at deploy time. Phase 3 (#505 Modul 3.1) replaces the bash
rendering with paramiko + port-forwarding + ``requests``.

Eight rounds of hardening are preserved (one regression test per round,
see ``tests/unit/test_secret_sync.py``):

R1. ``set -euo pipefail`` inside heredoc — remote bash doesn't inherit.
R2. Credential transit (was base64-over-heredoc; now stdin via
    :func:`_remote.ssh_run_script` — no transit-layer encoding needed).
R3. Tmpfile cleanup via ``trap``.
R4. Two-stage jq validation per folder (shape check + extraction).
R5. Key-regex ``^[A-Za-z_][A-Za-z0-9_]*$``.
R6. Multi-line value guard (``\\n`` in decoded value → skip, log key only).
R7. Atomic write via same-dir ``mktemp`` + rename.
R8. Two outage-safety gates (``succeeded == 0`` and ``pushed == 0``)
    — both produce ``wrote=0`` and leave the existing file untouched.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from nexus_deploy import _remote

# Marker block delimiters. Must match deploy.sh:4811-4813 (Jupyter) and
# the equivalent Marimo lines BYTE-FOR-BYTE — operators expect the same
# greppable marker, and the legacy-env strip step uses the same regex.
_END_MARKER = "# === END nexus-secret-sync ==="


# Server-side Infisical endpoint (same as `nexus_deploy.infisical`).
_INFISICAL_BASE_URL = "http://localhost:8070"

# Server-side path prefix for stack directories. Each stack lives at
# /opt/docker-server/stacks/<name>/ — convention enforced by the
# stack-rsync step earlier in deploy.sh.
_REMOTE_STACKS_DIR = "/opt/docker-server/stacks"

# RESULT-line parser (matches deploy.sh's ``echo "RESULT pushed=...
# wrote=..."`` shape exactly). Anchor at the start so a stray RESULT
# substring elsewhere in stderr/stdout can't false-match.
_RESULT_PATTERN = re.compile(
    r"^RESULT pushed=(?P<pushed>\d+) "
    r"skipped_name=(?P<skipped_name>\d+) "
    r"skipped_multi=(?P<skipped_multi>\d+) "
    r"failed=(?P<failed>\d+) "
    r"collisions=(?P<collisions>\d+) "
    r"succeeded=(?P<succeeded>\d+) "
    r"wrote=(?P<wrote>[01])$",
    re.MULTILINE,
)

# Same regex deploy.sh:4727 uses (POSIX shell-identifier rules).
_VALID_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class StackTarget:
    """Per-stack parameters that vary between Jupyter and Marimo.

    The two stacks share the entire sync logic; only these fields differ.
    Both heredocs in deploy.sh are byte-for-byte identical apart from
    these substitutions.
    """

    name: str  # "jupyter" | "marimo" — used for paths + the friendly
    # name in the BEGIN marker comment.

    @property
    def env_file(self) -> str:
        """Server-side path the sync writes to."""
        return f"{_REMOTE_STACKS_DIR}/{self.name}/.infisical.env"

    @property
    def legacy_env_file(self) -> str:
        """Pre-#495 location of the same block. Stripped after successful write."""
        return f"{_REMOTE_STACKS_DIR}/{self.name}/.env"

    @property
    def compose_dir(self) -> str:
        """Where ``docker compose up -d <name>`` runs from on restart."""
        return f"{_REMOTE_STACKS_DIR}/{self.name}"

    @property
    def begin_marker(self) -> str:
        """Marker comment ABOVE the rendered block. Preserves the legacy wording.

        deploy.sh used `Infisical → Jupyter env` / `Infisical → Marimo env`
        (capitalised stack name) — same here so existing greps + the
        sed-based legacy-strip continue to match.
        """
        friendly = self.name.capitalize()
        return (
            f"# === BEGIN nexus-secret-sync (Infisical → {friendly} env, "
            "plaintext, regenerated each spin-up — do not edit by hand) ==="
        )


@dataclass(frozen=True)
class SyncResult:
    """Counters parsed from the remote ``RESULT`` line.

    Mirrors the bash counters one-to-one. ``wrote=False`` is the
    "no-touch on outage" signal — Gate 1 (no folder fetch succeeded)
    OR Gate 2 (zero usable secrets across all successful fetches)
    fired, and the existing ``.infisical.env`` was left alone.
    """

    pushed: int
    skipped_invalid_name: int
    skipped_multiline: int
    failed_folders: int
    collisions: int
    succeeded_folders: int
    wrote: bool

    @property
    def is_partial(self) -> bool:
        """True if the sync wrote a file but had failed-folder counts > 0.

        Maps to CLI rc=1 (deploy.sh warns + continues). Distinct from
        rc=2 (transport / unexpected exception → deploy.sh aborts).
        """
        return self.wrote and self.failed_folders > 0


# ---------------------------------------------------------------------------
# Pure-logic helpers — deploy.sh-bash equivalents, unit-testable in Python.
# Each one has a matching invariant in test_secret_sync.py.
# ---------------------------------------------------------------------------


def is_safe_envfile_key(key: str) -> bool:
    """Mirror deploy.sh:4727 — POSIX shell identifier rules.

    Keys that fail this are skipped with `SKIPPED_NAME++`. Examples:
    ``FOO_BAR`` ok, ``1FOO`` rejected (leading digit), ``FOO-BAR``
    rejected (hyphen), ``FOO BAR`` rejected (space), ``""`` rejected.
    """
    return bool(_VALID_KEY_RE.fullmatch(key))


def has_multiline(value: str) -> bool:
    """Mirror deploy.sh:4739 — env-file format can't carry newlines portably.

    Values containing ``\\n`` are skipped with `SKIPPED_MULTI++`. The
    log line emitted server-side names the KEY only, never the value
    (R6: don't leak secret bytes via the warning channel).
    """
    return "\n" in value


def escape_dotenv_value(value: str) -> str:
    r"""Mirror deploy.sh:4761 — dotenv-safe escape.

    Two replacements, in this order (order matters: backslash first
    so we don't double-escape the escapes from the quote-replacement):
        ``\\`` → ``\\\\``    (literal backslash → escaped backslash)
        ``"``  → ``\\"``     (literal quote → escaped quote)
    Multi-line values are filtered upstream by :func:`has_multiline`,
    so we don't need to escape literal newlines here.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Bash rendering — produces the exact server-side script that
# `_remote.ssh_run_script` will exec via stdin.
# ---------------------------------------------------------------------------


def render_remote_script(
    *,
    target: StackTarget,
    project_id: str,
    infisical_token: str,
    infisical_env: str,
    gitea_token: str = "",
) -> str:
    """Render the remote bash script for one stack's secret sync.

    All inputs are shlex-quoted into single-quoted bash strings — token
    + env values can't break out of the script no matter what they
    contain. The script is fed to ``ssh nexus bash -s`` via stdin
    (:func:`_remote.ssh_run_script`), so neither argv nor ``ps`` ever
    sees the secrets.
    """
    pid_q = shlex.quote(project_id)
    token_q = shlex.quote(infisical_token)
    env_q = shlex.quote(infisical_env)
    gtoken_q = shlex.quote(gitea_token)
    env_file_q = shlex.quote(target.env_file)
    legacy_q = shlex.quote(target.legacy_env_file)
    begin_marker_q = shlex.quote(target.begin_marker)
    end_marker_q = shlex.quote(_END_MARKER)
    folders_url_q = shlex.quote(f"{_INFISICAL_BASE_URL}/api/v1/folders")
    secrets_url_q = shlex.quote(f"{_INFISICAL_BASE_URL}/api/v3/secrets/raw")

    # The bash below is deploy.sh:4616-4848 lifted near-verbatim.
    # Differences from the heredoc form:
    #   - Inputs come pre-decoded (shlex-quoted via stdin), no
    #     base64 transit step (R2).
    #   - Marker strings are interpolated as variables instead of
    #     literal sed patterns, because sed-pattern-escaping the long
    #     comment-style markers from Python is fragile. We use grep -F
    #     + awk to find the marker line numbers and a one-pass cat
    #     instead.
    #   - The "BEGIN marker" comment text in the new block matches
    #     deploy.sh's wording exactly (target.begin_marker).
    return f"""set -euo pipefail

PID={pid_q}
ITOK={token_q}
INF_ENV={env_q}
GTOKEN={gtoken_q}
ENV_FILE={env_file_q}
LEGACY_ENV={legacy_q}
BEGIN_MARKER={begin_marker_q}
END_MARKER={end_marker_q}
FOLDERS_URL={folders_url_q}
SECRETS_URL={secrets_url_q}

CFG=$(mktemp)
SEEN=$(mktemp)
APPEND=$(mktemp)
NEW_BLOCK=$(mktemp)
TSV=$(mktemp)
TMP_OUT=""
chmod 600 "$CFG" "$APPEND" "$NEW_BLOCK" "$TSV"
trap 'rm -f "$CFG" "$SEEN" "$APPEND" "$NEW_BLOCK" "$TSV" "$TMP_OUT"' EXIT
printf 'header = "Authorization: Bearer %s"\\n' "$ITOK" > "$CFG"

if ! command -v jq >/dev/null 2>&1; then
    echo "  ⚠ jq is not installed on the remote VM — Infisical sync needs jq, install with: sudo apt-get install -y jq" >&2
    echo "RESULT pushed=0 skipped_name=0 skipped_multi=0 failed=0 collisions=0 succeeded=0 wrote=0"
    exit 0
fi

PUSHED=0; SKIPPED_NAME=0; SKIPPED_MULTI=0; FAILED=0; COLLISIONS=0; SUCCEEDED=0; WROTE=0

FOLDERS_JSON=$(curl -sS --config "$CFG" --get \\
    --connect-timeout 5 --max-time 15 \\
    --data-urlencode "workspaceId=$PID" \\
    --data-urlencode "environment=$INF_ENV" \\
    "$FOLDERS_URL" || echo '{{}}')
FOLDERS=$(printf '%s' "$FOLDERS_JSON" | jq -r '.folders[]?.name' | LC_ALL=C sort || true)
FOLDERS=$(printf '%s\\n/\\n' "$FOLDERS")

while IFS= read -r FOLDER; do
    [ -z "$FOLDER" ] && continue
    if [ "$FOLDER" = "/" ]; then
        SECRET_PATH="/"; FOLDER_LABEL="<root>"
    else
        SECRET_PATH="/$FOLDER"; FOLDER_LABEL="$FOLDER"
    fi
    SECRETS_JSON=$(curl -sS --config "$CFG" --get \\
        --connect-timeout 5 --max-time 30 \\
        --data-urlencode "workspaceId=$PID" \\
        --data-urlencode "environment=$INF_ENV" \\
        --data-urlencode "secretPath=$SECRET_PATH" \\
        "$SECRETS_URL" || true)
    if ! printf '%s' "$SECRETS_JSON" | jq -e '.secrets | type == "array"' >/dev/null; then
        FAILED=$((FAILED+1))
        echo "  ⚠ Infisical fetch '$FOLDER_LABEL' returned bad shape, skipping" >&2
        continue
    fi
    if ! printf '%s' "$SECRETS_JSON" | jq -r '.secrets[]? | [.secretKey, (.secretValue | @base64)] | @tsv' > "$TSV"; then
        FAILED=$((FAILED+1))
        echo "  ⚠ jq TSV extraction failed for folder '$FOLDER_LABEL' — skipping" >&2
        continue
    fi
    SUCCEEDED=$((SUCCEEDED+1))
    while IFS=$'\\t' read -r KEY VALUE_B64; do
        [ -z "$KEY" ] && continue
        if ! printf '%s' "$KEY" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*$'; then
            SKIPPED_NAME=$((SKIPPED_NAME+1)); continue
        fi
        VALUE=$(printf '%s' "$VALUE_B64" | base64 -d || true)
        if printf '%s' "$VALUE" | grep -q $'\\n'; then
            SKIPPED_MULTI=$((SKIPPED_MULTI+1))
            echo "  ⚠ Skipping multi-line secret '$KEY' (folder '$FOLDER_LABEL')" >&2
            continue
        fi
        EXISTING=$(awk -F'\\t' -v k="$KEY" '$1 == k {{print $2; exit}}' "$SEEN")
        if [ -n "$EXISTING" ]; then
            COLLISIONS=$((COLLISIONS+1))
            echo "  ⚠ Key collision: '$KEY' in folder '$FOLDER_LABEL' shadowed by earlier value from '$EXISTING' (first-wins)" >&2
            continue
        fi
        ESCAPED_VALUE=$(printf '%s' "$VALUE" | sed -e 's/\\\\/\\\\\\\\/g' -e 's/"/\\\\"/g')
        printf '%s\\t%s\\n' "$KEY" "$FOLDER_LABEL" >> "$SEEN"
        printf '%s="%s"\\n' "$KEY" "$ESCAPED_VALUE" >> "$APPEND"
        PUSHED=$((PUSHED+1))
    done < "$TSV"
done <<< "$FOLDERS"

if [ -n "$GTOKEN" ] && ! grep -qE '^GITEA_TOKEN=' "$APPEND"; then
    ESCAPED_GTOKEN=$(printf '%s' "$GTOKEN" | sed -e 's/\\\\/\\\\\\\\/g' -e 's/"/\\\\"/g')
    printf 'GITEA_TOKEN="%s"\\n' "$ESCAPED_GTOKEN" >> "$APPEND"
    PUSHED=$((PUSHED+1))
fi

if [ "$SUCCEEDED" -eq 0 ]; then
    echo "  ⚠ No Infisical folder fetch succeeded — leaving existing $ENV_FILE untouched" >&2
    echo "RESULT pushed=0 skipped_name=$SKIPPED_NAME skipped_multi=$SKIPPED_MULTI failed=$FAILED collisions=$COLLISIONS succeeded=0 wrote=0"
    exit 0
fi

if [ "$PUSHED" -eq 0 ]; then
    echo "  ⚠ Infisical returned $SUCCEEDED folder(s) but zero usable secrets — leaving existing $ENV_FILE untouched" >&2
    echo "RESULT pushed=0 skipped_name=$SKIPPED_NAME skipped_multi=$SKIPPED_MULTI failed=$FAILED collisions=$COLLISIONS succeeded=$SUCCEEDED wrote=0"
    exit 0
fi

{{
    printf '%s\\n' "$BEGIN_MARKER"
    LC_ALL=C sort -t= -k1,1 "$APPEND"
    printf '%s\\n' "$END_MARKER"
}} > "$NEW_BLOCK"

[ -f "$ENV_FILE" ] || touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

TMP_OUT=$(mktemp -p "$(dirname "$ENV_FILE")" .infisical.env.XXXXXX)
chmod 600 "$TMP_OUT"
# Strip any existing block, then append the new one. Same anchored
# regex deploy.sh used; markers are interpolated as fixed strings so
# operator-edited content above/below stays put.
sed '/^# === BEGIN nexus-secret-sync/,/^# === END nexus-secret-sync/d' "$ENV_FILE" > "$TMP_OUT"
cat "$NEW_BLOCK" >> "$TMP_OUT"
mv "$TMP_OUT" "$ENV_FILE"
chmod 600 "$ENV_FILE"
WROTE=1

if [ -f "$LEGACY_ENV" ]; then
    LEGACY_TMP=$(mktemp -p "$(dirname "$LEGACY_ENV")" .env.XXXXXX)
    chmod 600 "$LEGACY_TMP"
    sed '/^# === BEGIN nexus-secret-sync/,/^# === END nexus-secret-sync/d' "$LEGACY_ENV" > "$LEGACY_TMP"
    mv "$LEGACY_TMP" "$LEGACY_ENV"
fi

echo "RESULT pushed=$PUSHED skipped_name=$SKIPPED_NAME skipped_multi=$SKIPPED_MULTI failed=$FAILED collisions=$COLLISIONS succeeded=$SUCCEEDED wrote=$WROTE"
"""


def parse_result(stdout: str) -> SyncResult | None:
    """Extract the ``RESULT`` line from remote stdout.

    Returns None if no parseable RESULT line exists (mirrors deploy.sh's
    ``[ -z "$JUP_PUSHED" ]`` empty-result branch).
    """
    match = _RESULT_PATTERN.search(stdout)
    if match is None:
        return None
    g = match.groupdict()
    return SyncResult(
        pushed=int(g["pushed"]),
        skipped_invalid_name=int(g["skipped_name"]),
        skipped_multiline=int(g["skipped_multi"]),
        failed_folders=int(g["failed"]),
        collisions=int(g["collisions"]),
        succeeded_folders=int(g["succeeded"]),
        wrote=g["wrote"] == "1",
    )


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


# Type alias for runner injection in tests.
import subprocess  # noqa: E402 — only needed for the type annotation below

ScriptRunner = Callable[[str], subprocess.CompletedProcess[str]]
CommandRunner = Callable[[str], subprocess.CompletedProcess[str]]


def run_sync_for_stack(
    target: StackTarget,
    *,
    project_id: str,
    infisical_token: str,
    infisical_env: str = "dev",
    gitea_token: str = "",
    script_runner: ScriptRunner | None = None,
    command_runner: CommandRunner | None = None,
) -> SyncResult:
    """Render the remote script, exec it via stdin, parse the result.

    On ``wrote=True`` follows up with ``docker compose up -d <stack>``
    via :func:`_remote.ssh_run` (separate ssh-call mirrors deploy.sh's
    two-step flow). The restart's exit code is logged but doesn't
    change the returned :class:`SyncResult` — restart failures surface
    via stderr but the secret-sync itself was successful.

    ``script_runner`` and ``command_runner`` are dependency-injection
    seams for tests; production callers leave them None.

    Returns a :class:`SyncResult` with all counters zero + ``wrote=False``
    if the remote script produced no parseable RESULT line.
    """
    run_script = script_runner or (lambda s: _remote.ssh_run_script(s))
    run_cmd = command_runner or (lambda c: _remote.ssh_run(c))

    script = render_remote_script(
        target=target,
        project_id=project_id,
        infisical_token=infisical_token,
        infisical_env=infisical_env,
        gitea_token=gitea_token,
    )

    completed = run_script(script)
    result = parse_result(completed.stdout)
    if result is None:
        # No RESULT line — return all-zeros. Caller (CLI) maps this to
        # the same warn-and-skip path deploy.sh has.
        return SyncResult(
            pushed=0,
            skipped_invalid_name=0,
            skipped_multiline=0,
            failed_folders=0,
            collisions=0,
            succeeded_folders=0,
            wrote=False,
        )

    if result.wrote:
        # Restart on change. Mirrors deploy.sh:4904 — `docker compose up
        # -d <stack>` recomputes the resolved-config hash and recreates
        # only when env_file content changed. No --force-recreate.
        # Restart failure does NOT alter result.wrote; we just emit the
        # error to stderr so the operator sees it.
        restart_cmd = f"cd {shlex.quote(target.compose_dir)} && docker compose up -d {shlex.quote(target.name)}"
        try:
            run_cmd(restart_cmd)
        except subprocess.CalledProcessError as exc:
            # Class name only (no exc.cmd) — defence in depth: even
            # though the restart command doesn't carry secrets, never
            # let exception output reach the workflow log unfiltered.
            print(f"  ⚠ docker compose up -d {target.name} failed ({type(exc).__name__})")

    return result
