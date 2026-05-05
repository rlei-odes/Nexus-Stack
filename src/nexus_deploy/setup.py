"""Server bootstrap helpers (Phase 3 Modul 3.4a, #505).

Replaces the four pre-orchestration bash blocks in
``scripts/deploy.sh`` (lines 173-459, ~270 LoC) with typed, tested
Python equivalents:

* **``configure_ssh``** — render the ``Host nexus`` block in
  ``~/.ssh/config`` with the Cloudflare Access ProxyCommand. Atomic
  awk-equivalent dedup of any pre-existing block, mode 600 file
  permissions. Aborts (raises :class:`SetupError`) when no Service
  Token is provided — browser-login fallback is impossible in CI
  and we'd rather fail loudly than confuse operators with a hung
  ssh prompt.
* **``wait_for_service_token``** — linear-backoff retry loop
  (5/10/15/20/25s) for Service Token propagation in Cloudflare
  Access. Cloudflare needs ~10s to activate a freshly-rotated
  token; without the initial sleep + retries the first ssh
  attempt nearly always 401s on cold-start deploys.
* **``wait_for_ssh``** — connectivity loop with exponential timeout
  (5s for first 3 attempts, 10s for next 4, 15s thereafter, max 15
  retries). Mirrors deploy.sh's ramp; an absolute Python-side
  timeout would convert "slow Hetzner ARM cold-start" into spurious
  hard failure.
* **``ensure_jq``** — idempotent ``apt-get install -y jq`` on the
  remote. Bootstrap for VMs that pre-date the cloud-init jq install
  (newer VMs have it baked in via ``tofu/stack/main.tf``); near-no-op
  on healthy VMs.
* **``mount_persistent_volume``** — render server-side bash that
  mounts the Hetzner block storage at ``/mnt/nexus-data`` with
  three-stage device discovery (scsi-id → automount inspection →
  ``/dev/sdb`` fallback), idempotent ``/etc/fstab`` entry, and
  ownership setup for Gitea (uid 1000) + Postgres (uid 70) sub-dirs.
  Empty volume_id skips the entire block (matches deploy.sh's
  ``[ "$PERSISTENT_VOLUME_ID" != "0" ]`` guard — not every deploy
  has a persistent-volume).

Two transports, consistent with the rest of the migration:

1. Local subprocess (``ssh``) for the readiness probes — same
   pattern as :class:`SSHClient.run` but called pre-tunnel, before
   any persistent SSHClient context exists.
2. Server-side bash via :func:`SSHClient.run_script` for ``jq``
   install + volume mount, run from a caller-supplied SSHClient
   that the orchestrator (Modul 3.4b) will provide.

The EXIT-trap setup in deploy.sh (``REMOTE_CLEANUP_PATHS``,
``RUNNER_CLEANUP_PATHS``) stays in bash for 3.4a — it's referenced
by other already-migrated modules' deploy.sh wrapper code (kestra,
gitea-oauth, gitea-mirror) and surviving deploy.sh blocks. Modul
3.4b (orchestrator.py) replaces the trap with a Python
``contextlib.ExitStack`` once deploy.sh's role shrinks to a
five-line wrapper.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nexus_deploy.ssh import SSHClient

# Default ssh-config path is resolved INSIDE :func:`configure_ssh`
# (not as a module-level constant) so HOME changes after import
# take effect — important for tests and CI runners that may
# redirect HOME mid-process. Round-4 PR #524 finding: the previous
# module-level constant locked the path at import time, contrary
# to the docstring's claim about call-time resolution.


class SetupError(Exception):
    """Raised for setup-phase errors not modelled by ``CalledProcessError``.

    Used for service-token-missing, max-retries-exhausted, and
    inconsistent-state cases. Subprocess-level failures still surface
    as :class:`subprocess.CalledProcessError` from the standard library.
    """


@dataclass(frozen=True)
class SSHConfigSpec:
    """Inputs for rendering the ``Host nexus`` ssh-config block.

    ``cf_client_id`` and ``cf_client_secret`` together form the
    Cloudflare Access Service Token. When BOTH are non-empty we
    emit the env-var-prefixed ProxyCommand variant that lets
    cloudflared authenticate without a browser. When EITHER is
    missing we raise :class:`SetupError` from :func:`configure_ssh`
    rather than emit the browser-login fallback — the deploy script
    runs in CI where browser login is impossible, and a missing
    token is a configuration error the operator must fix upstream.
    """

    ssh_host: str
    cf_client_id: str | None = None
    cf_client_secret: str | None = None
    host_alias: str = "nexus"
    identity_file: str = "~/.ssh/id_ed25519"

    @property
    def has_service_token(self) -> bool:
        return bool(self.cf_client_id and self.cf_client_secret)


@dataclass(frozen=True)
class SSHReadinessResult:
    """Outcome of a readiness probe loop.

    ``last_error`` carries the captured stderr tail (truncated to
    2000 chars, tail-preserving — same pattern as RsyncResult in
    stack_sync.py Round-2). Used both for diagnostics on success
    (mostly empty) and for the operator log on max-retries-exhausted.
    """

    succeeded: bool
    attempts: int
    last_error: str = ""


@dataclass(frozen=True)
class WettyAgentResult:
    """Outcome of :func:`setup_wetty_ssh_agent`. Tracks 5 idempotent
    steps — re-runs that find nothing to do still return
    successfully with the corresponding flag set to ``False``.

    Only the rendered server-side script knows whether each step ran
    or was a no-op; the result line is parsed back into this
    dataclass for the workflow log + tests.
    """

    keypair_generated: bool  # ssh-keygen ran (False = key already existed)
    pubkey_added: bool  # appended to authorized_keys (False = already present)
    agent_started: bool  # ssh-agent forked (False = socket present + responsive)
    key_added_to_agent: bool  # ssh-add ran (False = key fingerprint already loaded)
    auth_sock_written: bool  # SSH_AUTH_SOCK= written to wetty .env (always True on success)


@dataclass(frozen=True)
class VolumeMountResult:
    """Outcome of the persistent-volume mount step.

    ``mounted=False`` covers two distinct cases (callers that need
    to distinguish them check ``detail``): (a) ``volume_id`` was
    empty → skipped, deploy continues; (b) every device-discovery
    fallback failed → operator should investigate but deploy is
    not aborted (downstream stacks that don't need the volume can
    still come up healthy).
    """

    mounted: bool
    fstab_added: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Pure logic — render + dedup helpers.
# ---------------------------------------------------------------------------


def render_ssh_config_block(spec: SSHConfigSpec) -> str:
    """Render the ``Host nexus`` block as a single trailing-newline
    ssh-config snippet.

    Caller must pre-validate Service Token presence — this function
    will happily render a no-token block (matches the legacy
    fallback path), but :func:`configure_ssh` rejects that case
    upstream.
    """
    lines = [
        "",
        f"Host {spec.host_alias}",
        f"  HostName {spec.ssh_host}",
        "  User root",
        f"  IdentityFile {spec.identity_file}",
        "  IdentitiesOnly yes",
    ]
    if spec.has_service_token:
        # ProxyCommand uses the `env VAR=val cmd` form (Round-2 PR
        # #524 finding). The legacy `bash -c 'VAR=val cmd'` form
        # interpolated raw token values inside single quotes — a
        # token containing a single quote (or shell metacharacter)
        # would either break the resulting ssh-config or, worse,
        # change the executed command. Cloudflare Service Token IDs
        # are practically always alphanumeric+UUID-like in current
        # spec, but defence in depth: we shlex-quote the values so
        # the rendered line stays safe regardless of future
        # token-format changes.
        #
        # `env` is a real binary that takes argv-form `KEY=value`
        # pairs (no shell parsing of the assignments) and execs the
        # rest of argv as a command. ssh's ProxyCommand is parsed
        # by /bin/sh, but each whitespace-separated argv after sh's
        # tokenization reaches `env` as a single string — so
        # shlex.quote on the value handles the shell layer, and env
        # consumes the result without re-parsing.
        id_q = shlex.quote(spec.cf_client_id or "")
        secret_q = shlex.quote(spec.cf_client_secret or "")
        proxy = (
            f"ProxyCommand env "
            f"TUNNEL_SERVICE_TOKEN_ID={id_q} "
            f"TUNNEL_SERVICE_TOKEN_SECRET={secret_q} "
            f"cloudflared access ssh --hostname %h"
        )
    else:
        proxy = "ProxyCommand cloudflared access ssh --hostname %h"
    lines.append(f"  {proxy}")
    lines.append("")  # trailing newline so a subsequent block starts cleanly
    return "\n".join(lines)


def strip_existing_block(config_text: str, host_alias: str) -> str:
    """Remove the existing ``Host <alias>`` block from a config text.

    Matches the legacy awk's behaviour: skip starting at ``Host
    <alias>`` line, stop skipping at the next ``Host `` line (which
    is preserved). Idempotent on configs that don't contain the
    block. Unlike the legacy awk, normalizes the result so we never
    leave more than one consecutive blank line at a boundary —
    important when the surrounding bytes are diffed against a
    snapshot.
    """
    out_lines: list[str] = []
    skip = False
    target = f"Host {host_alias}"
    for raw_line in config_text.splitlines():
        stripped = raw_line.strip()
        if stripped == target:
            skip = True
            continue
        # Re-encounter any other `Host ` line ends the skip BEFORE
        # we consume that line — same as the awk: `/^Host / && skip
        # { skip=0 }` runs on the boundary line, then `!skip { print }`
        # prints it.
        if skip and stripped.startswith("Host "):
            skip = False
        if not skip:
            out_lines.append(raw_line)
    # Collapse trailing blanks but preserve one (POSIX text-file convention).
    while len(out_lines) > 1 and out_lines[-1] == "" and out_lines[-2] == "":
        out_lines.pop()
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Side-effect functions
# ---------------------------------------------------------------------------


def configure_ssh(
    spec: SSHConfigSpec,
    *,
    ssh_config_path: Path | None = None,
) -> None:
    """Atomically write/replace the ``Host nexus`` block in ssh-config.

    Algorithm:
    1. Ensure ``~/.ssh`` exists.
    2. Read existing config (empty string if missing).
    3. ``strip_existing_block`` to remove any prior ``Host nexus``.
    4. Append the freshly-rendered block.
    5. Atomic replace via same-dir mktemp + ``os.replace``, mode 0o600.

    Raises :class:`SetupError` when ``spec`` carries no Service Token
    — browser-login is impossible in CI (mirrors deploy.sh's red-box
    abort at L218-227).
    """
    if not spec.has_service_token:
        raise SetupError(
            "configure_ssh: Cloudflare Access Service Token "
            "(CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET) is required. "
            "Browser-login fallback is not supported in CI deployments.",
        )
    # Path.home() resolved here, NOT as a module-level constant —
    # so HOME changes after import (tests, CI scaffolding) take
    # effect immediately. Round-4 PR #524 fix.
    target = ssh_config_path if ssh_config_path is not None else Path.home() / ".ssh" / "config"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    cleaned = strip_existing_block(existing, spec.host_alias)
    new_block = render_ssh_config_block(spec)
    final = (cleaned.rstrip() + "\n" + new_block) if cleaned.strip() else new_block
    # Atomic same-dir tempfile.mkstemp + os.replace (Round-2 PR #524
    # finding): mkstemp gives us a unique name + O_EXCL semantics so
    # we can never collide with a concurrent run, and the kernel
    # creates the file atomically with the file descriptor we hold —
    # no symlink-attack window on a pre-existing path. We then
    # explicitly fchmod to 0o600 to enforce permissions regardless
    # of umask (mkstemp defaults to 0o600 on POSIX, but we belt-and-
    # suspenders).
    import tempfile as _tempfile

    fd, tmp_str = _tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_str)
    fd_owned_by_caller = True
    try:
        # fchmod must run before fdopen takes ownership of the fd.
        # If it raises (e.g. read-only filesystem on the tmp dir),
        # we still own the fd and must close it ourselves to avoid
        # an fd leak across long-running test runs / processes
        # (Round-3 PR #524 finding).
        try:
            os.fchmod(fd, 0o600)
        except Exception:
            os.close(fd)
            fd_owned_by_caller = False
            raise
        # Ownership transfers to the file object below; closing the
        # context manager on success or via the outer except's
        # bubble-up path closes the fd.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            fd_owned_by_caller = False
            f.write(final)
        tmp_path.replace(target)
    except Exception:
        # Cleanup the tmp file on any write failure; bubble up.
        # If we still own the fd (mkstemp succeeded but every
        # subsequent step before fdopen failed), close it.
        if fd_owned_by_caller:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path.exists():
            tmp_path.unlink()
        raise


# Subprocess seam for testing — production calls subprocess.run, tests
# inject a callable that scripts the retry sequence.
SSHProbeRunner = Callable[[str, float], subprocess.CompletedProcess[str]]


def _default_ssh_probe(host_alias: str, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run ``ssh <alias> 'echo ok'`` with a hard ConnectTimeout.

    No ``check=True`` — caller inspects the returncode and decides
    success/retry. ``BatchMode=yes`` ensures we never block waiting
    for an interactive prompt (which would be a CI hang); the loop
    is the only retry mechanism.
    """
    return subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={int(timeout_s)}",
            "-o",
            "BatchMode=yes",
            host_alias,
            "echo ok",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def wait_for_service_token(
    host_alias: str = "nexus",
    *,
    max_retries: int = 6,
    initial_wait_s: float = 10.0,
    backoff_step_s: float = 5.0,
    probe_runner: SSHProbeRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SSHReadinessResult:
    """Poll ssh-with-Service-Token until success or max_retries hit.

    Cloudflare Access needs a few seconds to activate a freshly
    issued Service Token; the initial sleep + linear-backoff loop
    (5/10/15/20/25s = `backoff_step_s` * retry-index) absorbs that
    propagation window without false-failing the deploy.

    ``sleep`` is injectable so tests can fast-forward without
    actually waiting 50+ seconds.
    """
    runner = probe_runner if probe_runner is not None else _default_ssh_probe
    sleep(initial_wait_s)
    last_error = ""
    for attempt in range(1, max_retries + 1):
        completed = runner(host_alias, 15.0)
        if completed.returncode == 0:
            return SSHReadinessResult(succeeded=True, attempts=attempt, last_error="")
        last_error = (completed.stderr or completed.stdout or "")[-2000:].rstrip()
        if attempt < max_retries:
            sleep(backoff_step_s * attempt)
    return SSHReadinessResult(succeeded=False, attempts=max_retries, last_error=last_error)


def wait_for_ssh(
    host_alias: str = "nexus",
    *,
    max_retries: int = 15,
    probe_runner: SSHProbeRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SSHReadinessResult:
    """Poll ssh-readiness with exponential timeout ramp.

    Timeout schedule mirrors deploy.sh L351-360 exactly: 5s for the
    first 3 attempts, 10s for attempts 4-7, 15s for the rest.
    Sleep schedule matches the timeout — operators see the same
    cadence in CI logs as the legacy script.

    Round-2 PR #524 fix: previous version used ``attempt < 3`` /
    ``attempt < 7`` which gave 5s to ONLY the first 2 attempts and
    10s to attempts 3-6 — off-by-one against the legacy bash. The
    legacy bash bumped TIMEOUT *after* the failed attempt's
    increment, so RETRY values 1, 2, AND 3 all stayed at 5s before
    jumping. Fixed with `< 4` / `< 8`.
    """
    runner = probe_runner if probe_runner is not None else _default_ssh_probe
    last_error = ""
    for attempt in range(1, max_retries + 1):
        if attempt < 4:
            timeout_s = 5.0
        elif attempt < 8:
            timeout_s = 10.0
        else:
            timeout_s = 15.0
        completed = runner(host_alias, timeout_s)
        if completed.returncode == 0:
            return SSHReadinessResult(succeeded=True, attempts=attempt, last_error="")
        last_error = (completed.stderr or completed.stdout or "")[-2000:].rstrip()
        if attempt < max_retries:
            sleep(timeout_s)
    return SSHReadinessResult(succeeded=False, attempts=max_retries, last_error=last_error)


def ensure_jq(ssh: SSHClient) -> bool:
    """Install ``jq`` on the remote if not already present.

    Returns ``True`` if ``apt-get install`` actually ran (legacy VM
    bootstrap), ``False`` if the binary was already there (modern
    VMs that have jq baked into cloud-init).

    Raises :class:`subprocess.CalledProcessError` if the install
    fails (caller — orchestrator — maps to red-abort: SFTPGo +
    Kestra register-flow blocks rely on jq, deploy can't continue
    without it).
    """
    check = ssh.run("command -v jq", check=False)
    if check.returncode == 0:
        return False
    install = ssh.run(
        "sudo apt-get update -qq >/dev/null && sudo apt-get install -y -qq jq >/dev/null",
        check=True,
    )
    _ = install
    return True


# Server-side bash for the volume-mount step. Rendered in Python +
# exec'd via ssh.run_script so we control the script as a string
# (snapshot-testable) instead of relying on a heredoc literal.
def _render_volume_mount_script(volume_id: str) -> str:
    """Render the server-side bash that mounts /mnt/nexus-data.

    ``volume_id`` is interpolated into the scsi device-id glob.
    Caller pre-validates it as digits-only; this function does NOT
    re-validate, since it's an internal seam.

    Three-stage device discovery + idempotent fstab + service-dir
    chown — same semantics as deploy.sh L408-454 but with a
    RESULT-line wire format so the Python wrapper can parse the
    outcome.
    """
    vid_q = shlex.quote(volume_id)
    return f"""set -euo pipefail

VOLUME_ID={vid_q}
MOUNT_POINT=/mnt/nexus-data
FSTAB_ADDED=0

mkdir -p "$MOUNT_POINT"

if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    echo "  Volume already mounted at $MOUNT_POINT" >&2
    MOUNTED=1
else
    MOUNTED=0
    # Stage 1: scsi-id discovery (Hetzner block-storage canonical path)
    DEV=$(ls "/dev/disk/by-id/scsi-0HC_Volume_$VOLUME_ID" 2>/dev/null || echo "")
    if [ -n "$DEV" ]; then
        if mount "$DEV" "$MOUNT_POINT" 2>&1; then
            echo "  Volume mounted at $MOUNT_POINT (scsi-id)" >&2
            MOUNTED=1
        fi
    fi
    # Stage 2: automount inspection — kernel may have already mounted it
    if [ "$MOUNTED" = "0" ] && mount | grep -q " $MOUNT_POINT "; then
        echo "  Volume auto-mounted at $MOUNT_POINT (kernel)" >&2
        MOUNTED=1
    fi
    # Stage 3: /dev/sdb fallback for VMs that pre-date scsi-id naming
    if [ "$MOUNTED" = "0" ] && [ -b /dev/sdb ]; then
        if mount /dev/sdb "$MOUNT_POINT" 2>&1; then
            echo "  Volume mounted at $MOUNT_POINT (/dev/sdb fallback)" >&2
            MOUNTED=1
        fi
    fi
fi

# Idempotent fstab entry — only add if not already there. Use scsi-id
# preferentially (stable across kernel reboots), fallback to /dev/sdb.
if ! grep -q "$MOUNT_POINT" /etc/fstab; then
    DEV=$(ls "/dev/disk/by-id/scsi-0HC_Volume_$VOLUME_ID" 2>/dev/null || echo "/dev/sdb")
    echo "$DEV $MOUNT_POINT ext4 defaults,nofail 0 2" >> /etc/fstab
    echo "  fstab entry added" >&2
    FSTAB_ADDED=1
fi

# Service sub-dirs — gated on a successful mount (Round-3 PR #524
# finding). Without the gate, mkdir/chown would land on the
# underlying root filesystem when every mount stage failed,
# silently storing user data on ephemeral storage that vanishes
# on reboot. Better to leave the dirs absent so downstream stacks
# fail fast with a clear "directory not found" error than to fake
# success on the wrong filesystem.
if [ "$MOUNTED" = "1" ]; then
    mkdir -p "$MOUNT_POINT/gitea/repos" "$MOUNT_POINT/gitea/lfs" "$MOUNT_POINT/gitea/db"
    chown -R 1000:1000 "$MOUNT_POINT/gitea/repos" "$MOUNT_POINT/gitea/lfs"
    chown -R 70:70 "$MOUNT_POINT/gitea/db"
fi

echo "RESULT mounted=$MOUNTED fstab_added=$FSTAB_ADDED"
"""


_VOLUME_RESULT_PATTERN = re.compile(
    r"^RESULT mounted=(?P<mounted>[01]) fstab_added=(?P<fstab>[01])$",
    re.MULTILINE,
)


def parse_volume_mount_result(stdout: str) -> tuple[bool, bool] | None:
    """Extract (mounted, fstab_added) from server stdout, or None on
    unparseable RESULT (caller maps to a soft failure)."""
    m = _VOLUME_RESULT_PATTERN.search(stdout)
    if m is None:
        return None
    g = m.groupdict()
    return (g["mounted"] == "1", g["fstab"] == "1")


# Volume_id must be digits — comes from Hetzner as a numeric ID. Reject
# anything else early to keep the rendered bash safe.
_VOLUME_ID_PATTERN = re.compile(r"^[0-9]+$")


def mount_persistent_volume(
    volume_id: str,
    ssh: SSHClient,
) -> VolumeMountResult:
    """Mount the Hetzner persistent volume at /mnt/nexus-data.

    Empty/zero ``volume_id`` (i.e. no persistent volume configured)
    short-circuits to ``mounted=False, detail='skipped'`` — matches
    deploy.sh's ``[ "$PERSISTENT_VOLUME_ID" != "0" ]`` guard.

    Non-empty volume_id is digits-only validated; non-digit input
    raises :class:`SetupError` (deploy.sh's tofu output is always
    numeric, so this is a defence-in-depth check against future
    refactor mistakes).
    """
    if not volume_id or volume_id == "0":
        return VolumeMountResult(mounted=False, fstab_added=False, detail="skipped")
    if not _VOLUME_ID_PATTERN.match(volume_id):
        raise SetupError(
            f"mount_persistent_volume: volume_id {volume_id!r} is not a "
            "positive integer (Hetzner volumes use numeric IDs)",
        )
    script = _render_volume_mount_script(volume_id)
    completed = ssh.run_script(script, check=True)
    # Forward per-step server-side output to local stderr. The remote
    # script writes diagnostics to its own stderr (`>&2`), but
    # ssh.run_script defaults to ``merge_stderr=True``, so they land
    # on completed.stdout interleaved with the RESULT line. We split
    # them: the RESULT wire-format line is parsed by Python below,
    # everything else gets forwarded to local stderr (Modul-1.2
    # Round-4 pattern; docstring corrected in Round-3 PR #524 since
    # "stderr" was misleading — the actual fd is merged stdout).
    for line in completed.stdout.splitlines():
        if not line.startswith("RESULT "):
            sys.stderr.write(line + "\n")
    parsed = parse_volume_mount_result(completed.stdout)
    if parsed is None:
        # Server-side script ran but emitted no parseable RESULT —
        # treat as a soft failure (the operator already saw the
        # forwarded stderr; downstream stacks that don't need the
        # volume can still come up).
        return VolumeMountResult(
            mounted=False,
            fstab_added=False,
            detail="no parseable RESULT",
        )
    mounted, fstab_added = parsed
    detail: Literal["mounted", "fallback-failed"] = "mounted" if mounted else "fallback-failed"
    return VolumeMountResult(mounted=mounted, fstab_added=fstab_added, detail=detail)


# ---------------------------------------------------------------------------
# Wetty SSH-Agent setup (Phase 3 Modul 3.4f, #505) — replaces
# deploy.sh:439-540, the ``[5.5/7]`` block that bootstraps the SSH key
# pair + agent socket Wetty needs to log into the host as the same
# user that runs the docker daemon.
# ---------------------------------------------------------------------------


_WETTY_RESULT_RE = re.compile(
    r"^RESULT_WETTY"
    r" keypair_generated=(?P<keypair_generated>[01])"
    r" pubkey_added=(?P<pubkey_added>[01])"
    r" agent_started=(?P<agent_started>[01])"
    r" key_added_to_agent=(?P<key_added_to_agent>[01])"
    r" auth_sock_written=(?P<auth_sock_written>[01])$",
    re.MULTILINE,
)


def render_wetty_agent_script(
    *,
    key_path: str = "/root/.ssh/id_ed25519_wetty",
    key_comment: str = "wetty-auto-generated",
    agent_socket: str = "/tmp/ssh-agent/agent.sock",  # noqa: S108 — server-side path
    wetty_env_file: str = "/opt/docker-server/stacks/wetty/.env",
) -> str:
    """Render the server-side bash that idempotently bootstraps the
    ed25519 key pair, ssh-agent socket, and SSH_AUTH_SOCK injection
    Wetty needs to log into the host. Mirrors deploy.sh:445-538.

    The script does six numbered steps; only five of them produce a
    0/1 flag in the final RESULT line (step 1 is a precondition that
    always runs and is not reflected in the result):

    1. mkdir + chmod 700 ``~/.ssh`` (silent precondition — not in the
       RESULT line).
    2. ssh-keygen -t ed25519 the key pair if absent. Fail-fast on a
       non-zero ssh-keygen exit OR missing output files (would
       otherwise produce a misleading ``keypair_generated=1`` while
       downstream steps fail silently). → ``keypair_generated``.
    3. Append the public key to ``authorized_keys`` if not already
       present. → ``pubkey_added``.
    4. Start ssh-agent if the socket isn't there or the agent is
       unresponsive (dead-socket detection — the legacy bash had this).
       → ``agent_started``.
    5. ssh-add the key if its fingerprint isn't already loaded.
       → ``key_added_to_agent``.
    6. Strip any prior ``SSH_AUTH_SOCK=`` line from
       ``stacks/wetty/.env`` and re-append the current socket path.
       → ``auth_sock_written`` (always 1 on the success path; the
       env-var line is unconditional).
    """
    key_q = shlex.quote(key_path)
    comment_q = shlex.quote(key_comment)
    sock_q = shlex.quote(agent_socket)
    env_q = shlex.quote(wetty_env_file)
    return f"""set -uo pipefail
KEY_PATH={key_q}
KEY_COMMENT={comment_q}
SOCKET={sock_q}
ENV_FILE={env_q}
KEYPAIR_GEN=0
PUBKEY_ADD=0
AGENT_STARTED=0
KEY_ADDED=0
AUTH_SOCK_WROTE=0

mkdir -p /root/.ssh
chmod 700 /root/.ssh

# Inline-step numbering matches the docstring's "1. mkdir + chmod 700"
# precondition above. The flags below correspond to docstring steps
# 2-6 (steps that produce a 0/1 in RESULT_WETTY).
#
# Step 2: ssh-keygen if absent. Fail-fast (echo RESULT_WETTY with
# keypair_generated=0 + bail) when ssh-keygen reports non-zero OR
# either of the expected output files ($KEY_PATH / $KEY_PATH.pub)
# isn't on disk afterwards. Without this check, downstream steps
# (cat $KEY_PATH.pub, ssh-keygen -lf for fingerprint) would silently
# fail and we'd report a misleading keypair_generated=1 while Wetty
# can't actually SSH.
# Regenerate if EITHER file is missing — not just $KEY_PATH. A stale
# private key with a missing/corrupted .pub (manual cleanup, partial
# write, fs corruption) would otherwise pass the keygen-skip check
# but later `cat "$KEY_PATH.pub"` would yield empty PUBKEY → the
# authorized_keys append silently no-ops (empty grep -F matches the
# whole file → PUBKEY_ADD stays 0) and Wetty can't actually SSH.
# Removing both files first lets ssh-keygen emit a fresh, consistent
# pair (it would refuse to overwrite an existing $KEY_PATH).
if [ ! -f "$KEY_PATH" ] || [ ! -f "$KEY_PATH.pub" ]; then
    if [ -f "$KEY_PATH" ] || [ -f "$KEY_PATH.pub" ]; then
        echo "  ⚠ Wetty keypair half-present (one of $KEY_PATH / .pub missing) — regenerating" >&2
        rm -f "$KEY_PATH" "$KEY_PATH.pub"
    fi
    if ! ssh-keygen -t ed25519 -f "$KEY_PATH" -N '' -C "$KEY_COMMENT" >/dev/null 2>&1; then
        echo "  ⚠ ssh-keygen failed — Wetty will not have a working SSH key" >&2
        echo "RESULT_WETTY keypair_generated=0 pubkey_added=0 agent_started=0 key_added_to_agent=0 auth_sock_written=0"
        exit 0
    fi
    if [ ! -f "$KEY_PATH" ] || [ ! -f "$KEY_PATH.pub" ]; then
        echo "  ⚠ ssh-keygen reported success but key files missing (filesystem issue?)" >&2
        echo "RESULT_WETTY keypair_generated=0 pubkey_added=0 agent_started=0 key_added_to_agent=0 auth_sock_written=0"
        exit 0
    fi
    chmod 600 "$KEY_PATH"
    chmod 644 "$KEY_PATH.pub"
    KEYPAIR_GEN=1
fi

# Step 3: append pubkey to authorized_keys (idempotent — full-line
# fixed-string match so neither a partial duplicate (existing line
# contains $PUBKEY as substring) nor a substring of $PUBKEY (existing
# line that happens to be a substring of the pubkey we're checking)
# can false-positive. `-F` = fixed string (no regex), `-x` = whole-
# line match (the actual invariant the comment claims).
PUBKEY=$(cat "$KEY_PATH.pub")
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
if ! grep -qFx "$PUBKEY" /root/.ssh/authorized_keys 2>/dev/null; then
    printf '%s\\n' "$PUBKEY" >> /root/.ssh/authorized_keys
    PUBKEY_ADD=1
fi

# Step 4: ssh-agent. Two probes: (a) socket file present, (b) agent
# responsive (`ssh-add -l` exit code). A dead-socket from a previous
# crash needs cleaning up before we can start a fresh agent. After
# starting, validate the agent IS responsive — without the validation,
# `ssh-agent` failing silently (`|| true` was needed for the eval +
# subshell case) would still set AGENT_STARTED=1 and we'd report
# success for a broken socket.
mkdir -p "$(dirname "$SOCKET")"
AGENT_OK=0
if [ -S "$SOCKET" ]; then
    SSH_AUTH_SOCK="$SOCKET" ssh-add -l >/dev/null 2>&1 && AGENT_OK=1
    if [ "$AGENT_OK" = "0" ]; then
        # Stale socket — clean up before forking a fresh agent.
        rm -f "$SOCKET"
    fi
fi
if [ "$AGENT_OK" = "0" ]; then
    # `ssh-agent -a SOCKET -s` prints `SSH_AUTH_SOCK=...; SSH_AGENT_PID=...`
    # to stdout for eval; on success the agent forks and listens on
    # SOCKET. Capture the eval's exit AND validate the result.
    if eval "$(ssh-agent -a "$SOCKET" -s)" >/dev/null 2>&1; then
        # Validate: socket file must exist AND the agent must respond.
        # ssh-add -l exit code: 0=keys loaded, 1=no keys but agent OK,
        # 2=can't connect (the failure case we're guarding against).
        if [ -S "$SOCKET" ] && SSH_AUTH_SOCK="$SOCKET" ssh-add -l >/dev/null 2>&1; then
            AGENT_STARTED=1
        else
            ADD_RC=0
            SSH_AUTH_SOCK="$SOCKET" ssh-add -l >/dev/null 2>&1 || ADD_RC=$?
            # rc=1 (no keys) is OK; rc=2 (can't connect) is a real failure.
            if [ "$ADD_RC" = "1" ]; then
                AGENT_STARTED=1
            else
                echo "  ⚠ ssh-agent started but socket isn't responsive — Wetty SSH-Agent setup incomplete" >&2
                echo "RESULT_WETTY keypair_generated=$KEYPAIR_GEN pubkey_added=$PUBKEY_ADD agent_started=0 key_added_to_agent=0 auth_sock_written=0"
                exit 0
            fi
        fi
    else
        echo "  ⚠ ssh-agent failed to start — Wetty SSH-Agent setup incomplete" >&2
        echo "RESULT_WETTY keypair_generated=$KEYPAIR_GEN pubkey_added=$PUBKEY_ADD agent_started=0 key_added_to_agent=0 auth_sock_written=0"
        exit 0
    fi
fi
export SSH_AUTH_SOCK="$SOCKET"

# Step 5: ssh-add the key if its fingerprint isn't already loaded.
# Set KEY_ADDED=1 only on a successful add (legacy form unconditionally
# set it after `|| true`, which masked an actual ssh-add failure).
KEY_FP=$(ssh-keygen -lf "$KEY_PATH" 2>/dev/null | awk '{{print $2}}' || echo "")
KEY_LOADED=0
if [ -n "$KEY_FP" ]; then
    if ssh-add -l 2>/dev/null | grep -qF "$KEY_FP"; then
        KEY_LOADED=1
    fi
fi
if [ "$KEY_LOADED" = "0" ]; then
    if ssh-add "$KEY_PATH" >/dev/null 2>&1; then
        KEY_ADDED=1
    else
        echo "  ⚠ ssh-add failed — Wetty key not loaded into agent" >&2
        # Don't fail-fast: ssh-add can fail for a non-existent key (we
        # already gated above) or transient agent issue. Leave
        # KEY_ADDED=0 so the operator sees the discrepancy in the
        # RESULT_WETTY line; the key file + agent socket still exist
        # so the operator can ssh-add manually if needed.
        :
    fi
fi

# Step 6: write SSH_AUTH_SOCK= to wetty's .env (idempotent — strip
# any prior line first). Set AUTH_SOCK_WROTE=1 only after the append
# succeeds; an mkdir/permission failure shouldn't be reported as
# "written".
if [ -f "$ENV_FILE" ]; then
    if ! sed -i '/^SSH_AUTH_SOCK=/d' "$ENV_FILE" 2>/dev/null; then
        echo "  ⚠ failed to strip existing SSH_AUTH_SOCK= line from $ENV_FILE — Wetty .env may have stale entries" >&2
    fi
fi
if printf 'SSH_AUTH_SOCK=%s\\n' "$SSH_AUTH_SOCK" >> "$ENV_FILE" 2>/dev/null; then
    AUTH_SOCK_WROTE=1
else
    echo "  ⚠ failed to append SSH_AUTH_SOCK= to $ENV_FILE — Wetty container won't see the agent socket" >&2
    echo "RESULT_WETTY keypair_generated=$KEYPAIR_GEN pubkey_added=$PUBKEY_ADD agent_started=$AGENT_STARTED key_added_to_agent=$KEY_ADDED auth_sock_written=0"
    exit 0
fi

echo "RESULT_WETTY keypair_generated=$KEYPAIR_GEN pubkey_added=$PUBKEY_ADD agent_started=$AGENT_STARTED key_added_to_agent=$KEY_ADDED auth_sock_written=$AUTH_SOCK_WROTE"
"""


def parse_wetty_agent_result(stdout: str) -> WettyAgentResult | None:
    """Parse the RESULT_WETTY line from rendered-script stdout. Returns
    None if no parseable line exists (caller treats as soft failure)."""
    match = _WETTY_RESULT_RE.search(stdout)
    if match is None:
        return None
    g = match.groupdict()
    return WettyAgentResult(
        keypair_generated=g["keypair_generated"] == "1",
        pubkey_added=g["pubkey_added"] == "1",
        agent_started=g["agent_started"] == "1",
        key_added_to_agent=g["key_added_to_agent"] == "1",
        auth_sock_written=g["auth_sock_written"] == "1",
    )


def setup_wetty_ssh_agent(
    ssh: SSHClient,
    *,
    key_path: str = "/root/.ssh/id_ed25519_wetty",
    key_comment: str = "wetty-auto-generated",
    agent_socket: str = "/tmp/ssh-agent/agent.sock",  # noqa: S108
    wetty_env_file: str = "/opt/docker-server/stacks/wetty/.env",
) -> WettyAgentResult | None:
    """Render + run the wetty-agent setup script. Returns None on
    unparseable output (soft failure — operator sees the script's
    forwarded stderr; the deploy continues without aborting since
    Wetty is a non-critical UI service).

    ``check=True``: the rendered script ALWAYS terminates with
    ``exit 0`` (fail-fast paths emit a parseable all-zero RESULT line
    + ``exit 0``), so a non-zero returncode here can only mean the
    SSH transport itself broke (rc=255, connection drop, ...). Letting
    ``CalledProcessError`` propagate so the CLI handler maps it to
    rc=2 ("transport failure") instead of silently returning None and
    falling through to the rc=1 "soft fail" branch — caught in
    #530 R4.
    """
    script = render_wetty_agent_script(
        key_path=key_path,
        key_comment=key_comment,
        agent_socket=agent_socket,
        wetty_env_file=wetty_env_file,
    )
    completed = ssh.run_script(script, check=True)
    # Forward non-RESULT diagnostic lines (the script's own ⚠ /
    # ssh-keygen errors etc.) to the local terminal. SSHClient.run_script
    # uses merge_stderr=True by default, so the script's stderr is
    # already folded into completed.stdout — iterating stdout here
    # captures both the rendered bash's `echo … >&2` warnings AND any
    # plain stdout, while skipping the parseable RESULT_WETTY line
    # itself (which the parser below consumes).
    for line in completed.stdout.splitlines():
        if not line.startswith("RESULT_WETTY"):
            sys.stderr.write(line + "\n")
    return parse_wetty_agent_result(completed.stdout)
