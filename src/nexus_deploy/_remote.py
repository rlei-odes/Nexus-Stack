"""Subprocess primitives for talking to the nexus server (Phase 1, #505).

Temporary scaffolding while the migration is in progress: these are
plain ``subprocess.run`` wrappers around ``ssh nexus <cmd>`` and
``rsync … nexus:…``, mirroring the bash-side patterns one-to-one so the
strangler-fig handoff doesn't change network behaviour.

Phase 3 (#505 Modul 3.1) replaces this with ``nexus_deploy.ssh.SSHClient``
— a paramiko-backed client with persistent connection, port-forwarding,
and proper SFTP. Until then, every consumer here uses the system ``ssh``
config alias `nexus` (which the spin-up workflow's "Setup SSH config"
step writes), so anything that works in deploy.sh works here too.

Tests mock ``subprocess.run`` directly. There are no integration tests
in this module — those land with the paramiko refactor in Phase 3.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Default subprocess timeouts. The bash side has no timeouts at all,
# but Python's subprocess is more forgiving about hung remote calls
# when we add an upper bound. Generous defaults so a slow Hetzner
# control-plane spin-up (creds rotation, first cold start) doesn't
# trip them.
_DEFAULT_TIMEOUT_S = 120


def ssh_run(
    cmd: str,
    *,
    host: str = "nexus",
    check: bool = True,
    timeout: float = _DEFAULT_TIMEOUT_S,
    merge_stderr: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a single command on the nexus server via the local ssh-config alias.

    Equivalent to::

        ssh nexus "<cmd>"        # merge_stderr=True (default)
        ssh nexus "<cmd>" 2>&1   # bash equivalent of the default

    With ``merge_stderr=True`` (default) stderr is folded into stdout
    in the returned ``CompletedProcess`` — parity with deploy.sh's
    ``ssh nexus "..." 2>&1`` pattern. With ``merge_stderr=False``
    stdout and stderr are captured into separate fields on the
    CompletedProcess. Either way the streams are captured (we don't
    let them flow to the local terminal — long stderr tails on a
    failing curl loop would clutter the deploy log; callers that want
    that should print ``result.stderr`` themselves).
    """
    # Don't use `capture_output=True` here: it sets stdout=PIPE+stderr=PIPE
    # internally, and combining it with an explicit `stderr=...` raises
    # ValueError("stderr and capture_output may not both be used"). We
    # need explicit stderr control (STDOUT-merging in the default case)
    # so we set both pipes ourselves.
    return subprocess.run(
        ["ssh", host, cmd],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def rsync_to_remote(
    local: Path,
    remote: str,
    *,
    delete: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Push a local directory to the nexus server via rsync.

    ``remote`` follows rsync syntax (e.g. ``"nexus:/tmp/infisical-push/"``);
    the alias resolves through the same ssh config as ``ssh_run``. The
    trailing slash on ``local`` is auto-appended so rsync uploads the
    directory's CONTENTS, matching deploy.sh's
    ``rsync -aq --delete "$PUSH_DIR/" "nexus:/tmp/infisical-push/"``.

    ``delete=True`` clears destination paths that don't exist locally —
    used when the local dir is the canonical source-of-truth for that
    remote location.
    """
    src = f"{local}/" if not str(local).endswith("/") else str(local)
    args = ["rsync", "-aq"]
    if delete:
        args.append("--delete")
    args += [src, remote]
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
