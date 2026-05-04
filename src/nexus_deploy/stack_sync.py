"""Per-stack rsync + disabled-stack cleanup (Phase 3 Modul 3.3, #505).

Replaces ``scripts/deploy.sh`` lines 1401-1444 (~45 LoC bash):

* Per-stack rsync loop — for every entry in ``$ENABLED_SERVICES``,
  rsync ``stacks/<svc>/`` → ``nexus:/opt/docker-server/stacks/<svc>/``.
  Missing local stack folders produce a yellow warning and are skipped
  (deploy.sh ``[ -d "$STACKS_DIR/$service" ]`` check).
* Disabled-stack cleanup — server-side bash loop that walks
  ``/opt/docker-server/stacks/*/``, ``docker compose down`` + ``rm -rf``
  any folder NOT in ``$ENABLED_SERVICES``. Idempotent on re-run.

Two transports, mirroring the rest of the migration:

1. **Local rsync** (one subprocess per service) via
   :func:`_remote.rsync_to_remote`. Each service rsyncs independently
   so a single failure doesn't abort the rest of the loop — matches
   deploy.sh's serial loop semantics (a non-zero rsync exit on one
   stack would have triggered ``set -e`` and aborted the whole script;
   the migration is STRICTLY-MORE-FORGIVING here, partial-failure
   continues with a per-service ``failed`` counter).
2. **Server-side cleanup script** via :func:`_remote.ssh_run_script`
   (stdin) — one ssh round-trip. The list of enabled service names
   is interpolated as a single ``shlex.quote``'d bash string literal
   and matched line-exactly with ``grep -qFx`` (defence in depth: a
   hypothetical service name containing regex metachars couldn't
   false-positive against a similar folder name on the server).

Path safety (R5 from prior modules): every service name passes
``^[A-Za-z0-9._-]+$`` before being interpolated into either the
rsync remote target OR the server-side bash. A name that fails
validation is recorded as ``RsyncResult(status='failed', detail='unsafe name')``
and excluded from the cleanup script's enabled-set, so the cleanup
loop will (correctly) treat it as disabled and remove any folder
that happens to match — there is no "safe" interpretation of an
unsafe name. Operators see the warning and can fix the upstream
``$ENABLED_SERVICES`` list.

Phase 3 Modul 3.4 (#505 orchestrator) calls into here once the
rest of the deploy pipeline lives in Python. Until then, deploy.sh
invokes ``python -m nexus_deploy stack-sync --enabled <csv>``.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nexus_deploy import _remote

# Mirrors deploy.sh:20 ``REMOTE_STACKS_DIR`` — the canonical location
# on the nexus server where every stack's ``docker-compose.yml``
# lives. Adjacent stacks live as sibling folders under here.
_REMOTE_STACKS_DIR = "/opt/docker-server/stacks"

# Path-safety regex (R5 invariant): every service name must match
# this before we interpolate it into a shell command, an rsync remote
# spec, or a server-side bash loop. ``[A-Za-z0-9._-]`` is the same
# allow-list seeder.py uses for repo-path segments and gitea.py uses
# for URL segments — broad enough for every existing stack name in
# the repo (jupyter, marimo, seaweedfs-filer, …) but tight enough
# that no shell metachar, newline, or whitespace can sneak through.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# RESULT-line shape from the cleanup script — same wire-format family
# as compose_runner / seeder / secret_sync.
_RESULT_PATTERN = re.compile(
    r"^RESULT stopped=(?P<stopped>\d+) removed=(?P<removed>\d+) failed=(?P<failed>\d+)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class RsyncResult:
    """Per-service rsync outcome.

    ``status`` values:

    * ``synced`` — rsync exited 0; the service's stacks/<svc>/ dir
      now matches the server's copy.
    * ``missing-local`` — local stacks/<svc>/ does NOT exist; deploy.sh
      yellow-warned and continued. Mirrors the legacy ``[ -d ... ]``
      branch.
    * ``failed`` — rsync exited non-zero (transport problem, permission
      issue, broken pipe), OR the service name failed path-safety.
      ``detail`` carries the rc / reason for the operator log.
    """

    service: str
    status: Literal["synced", "missing-local", "failed"]
    detail: str = ""


@dataclass(frozen=True)
class CleanupResult:
    """Counters from the server-side cleanup loop.

    ``stopped`` counts services where ``docker compose down`` ran
    successfully (their ``docker-compose.yml`` was present).
    ``removed`` counts service folders that ``rm -rf`` cleared. Both
    are strictly per-disabled-service — already-missing folders aren't
    touched. ``failed`` is the count of disabled services where either
    ``compose down`` OR ``rm -rf`` exited non-zero. A disabled service
    with no compose.yml that ``rm -rf`` succeeds on increments
    ``removed`` only.
    """

    stopped: int
    removed: int
    failed: int

    @property
    def is_success(self) -> bool:
        """True iff zero failures."""
        return self.failed == 0


@dataclass(frozen=True)
class StackSyncResult:
    """Aggregate of the rsync loop + cleanup step.

    ``cleanup`` is ``None`` when the cleanup step never ran (e.g.
    transport failure raising before script execution) OR the script
    ran but produced no parseable RESULT line — caller (CLI) maps
    both to rc=2.
    """

    rsync: tuple[RsyncResult, ...]
    cleanup: CleanupResult | None

    @property
    def synced(self) -> int:
        return sum(1 for r in self.rsync if r.status == "synced")

    @property
    def missing(self) -> int:
        return sum(1 for r in self.rsync if r.status == "missing-local")

    @property
    def failed_rsync(self) -> int:
        return sum(1 for r in self.rsync if r.status == "failed")

    @property
    def is_success(self) -> bool:
        if self.failed_rsync > 0:
            return False
        if self.cleanup is None:
            return False
        return self.cleanup.is_success


# ---------------------------------------------------------------------------
# Pure logic — name validation + cleanup-script rendering.
# ---------------------------------------------------------------------------


def _is_safe_name(name: str) -> bool:
    """True iff ``name`` matches the allow-list regex.

    Wraps the regex in a function so future tightening (e.g. minimum
    length, deny-list of reserved bash keywords) lives in one place.
    """
    return bool(_SAFE_NAME.match(name))


def render_cleanup_script(
    enabled: list[str],
    *,
    stacks_dir: str = _REMOTE_STACKS_DIR,
) -> str:
    """Render the server-side bash that removes disabled stacks.

    The enabled list is interpolated as a single ``shlex.quote``'d
    multi-line string literal; ``grep -qFx`` matches the current
    folder name against it line-exactly. ``-F`` treats the pattern
    as a fixed string (regex metachars in a hypothetical future stack
    name don't matter), ``-x`` requires the entire line to match (so
    "jupyter" doesn't false-positive against "jupyter-old"), ``--``
    terminates options so a name starting with ``-`` doesn't get
    parsed as a flag.

    Caller MUST pre-validate names with :func:`_is_safe_name` —
    rendering does not re-check, and an unsafe name reaching this
    function is a programming error. The CLI / orchestrator is the
    last line of defence; this module's tests pin the name-validation
    contract via :func:`run_stack_sync`.
    """
    stacks_q = shlex.quote(stacks_dir)
    enabled_blob = shlex.quote("\n".join(enabled))

    return f"""set -euo pipefail

STACKS_DIR={stacks_q}
ENABLED_LIST={enabled_blob}

STOPPED=0
REMOVED=0
FAILED=0

for stack_dir in "$STACKS_DIR"/*/; do
    [ -d "$stack_dir" ] || continue
    name=$(basename "$stack_dir")
    if printf '%s\\n' "$ENABLED_LIST" | grep -qFx -- "$name"; then
        continue
    fi
    if [ -f "${{stack_dir}}docker-compose.yml" ]; then
        echo "  Stopping $name (disabled)..." >&2
        if ( cd "$stack_dir" && docker compose down 2>/dev/null ); then
            STOPPED=$((STOPPED+1))
        else
            echo "  ⚠ docker compose down failed for $name" >&2
            FAILED=$((FAILED+1))
            continue
        fi
    fi
    echo "  Removing $name stack folder..." >&2
    if rm -rf "$stack_dir"; then
        REMOVED=$((REMOVED+1))
    else
        echo "  ⚠ rm -rf failed for $name" >&2
        FAILED=$((FAILED+1))
    fi
done

echo "RESULT stopped=$STOPPED removed=$REMOVED failed=$FAILED"
"""


def parse_cleanup_result(stdout: str) -> CleanupResult | None:
    """Extract the ``RESULT`` line from the cleanup-script stdout.

    Returns None if no parseable RESULT — caller treats that as a
    hard transport failure (same defensive parse as compose_runner).
    """
    match = _RESULT_PATTERN.search(stdout)
    if match is None:
        return None
    g = match.groupdict()
    return CleanupResult(
        stopped=int(g["stopped"]),
        removed=int(g["removed"]),
        failed=int(g["failed"]),
    )


# ---------------------------------------------------------------------------
# Side-effect functions — rsync loop + cleanup orchestration.
# ---------------------------------------------------------------------------


RsyncRunner = Callable[[Path, str], subprocess.CompletedProcess[str]]
ScriptRunner = Callable[[str], subprocess.CompletedProcess[str]]


def rsync_enabled_stacks(
    local_stacks_dir: Path,
    enabled: list[str],
    *,
    rsync_runner: RsyncRunner | None = None,
    remote_stacks_dir: str = _REMOTE_STACKS_DIR,
    host: str = "nexus",
) -> tuple[RsyncResult, ...]:
    """Rsync each enabled service's local stack folder to the server.

    One rsync subprocess per service. A failed rsync produces a
    ``failed`` RsyncResult with the rc in ``detail``; the loop
    continues for the remaining services (STRICTLY-MORE-FORGIVING
    than deploy.sh's ``set -e`` abort-on-first-error semantics).

    ``rsync_runner`` is a dependency-injection seam for tests.
    Production callers leave it None and get :func:`_remote.rsync_to_remote`,
    which wraps ``rsync -aq`` with capture_output. ``-aq`` (instead
    of deploy.sh's ``-av``) is a deliberate divergence: rsync's
    per-file output for an entire stack tree (n8n's node_modules
    alone is thousands of files) would dominate the deploy log;
    capture_output gives us the diagnostic on failure without the
    happy-path noise.
    """
    runner = rsync_runner or (lambda local, remote: _remote.rsync_to_remote(local, remote))
    results: list[RsyncResult] = []
    for svc in enabled:
        if not _is_safe_name(svc):
            results.append(
                RsyncResult(
                    service=svc,
                    status="failed",
                    detail="unsafe name (must match [A-Za-z0-9._-]+)",
                ),
            )
            continue
        local = local_stacks_dir / svc
        if not local.is_dir():
            results.append(RsyncResult(service=svc, status="missing-local"))
            continue
        try:
            runner(local, f"{host}:{remote_stacks_dir}/{svc}/")
        except subprocess.CalledProcessError as exc:
            # rsync's stderr for a stack-dir push contains only file
            # paths + permission errors — no secrets. Surfacing the
            # rc gives operators something to grep for; the captured
            # stderr is left in CalledProcessError but not echoed
            # here (the module-level orchestrator forwards it).
            results.append(
                RsyncResult(service=svc, status="failed", detail=f"rsync rc={exc.returncode}"),
            )
            continue
        results.append(RsyncResult(service=svc, status="synced"))
    return tuple(results)


def cleanup_disabled_stacks(
    enabled: list[str],
    *,
    script_runner: ScriptRunner | None = None,
    remote_stacks_dir: str = _REMOTE_STACKS_DIR,
) -> CleanupResult | None:
    """Render → exec → parse the server-side cleanup script.

    Filters ``enabled`` through :func:`_is_safe_name` before rendering
    — an unsafe name is silently dropped from the enabled set, which
    means any folder matching that unsafe name on the server WILL be
    removed (the safe-by-default behaviour: an unsafe name in the
    enabled list is itself a configuration error, not a hint to
    preserve folders that match it).

    Returns None on transport failure or unparseable RESULT line.
    """
    safe_enabled = [s for s in enabled if _is_safe_name(s)]
    script = render_cleanup_script(safe_enabled, stacks_dir=remote_stacks_dir)
    runner = script_runner or (lambda s: _remote.ssh_run_script(s))
    completed = runner(script)

    # Forward per-stack diagnostics ("Stopping foo...", "Removing
    # foo...") to local stderr — same Modul-1.2-Round-4 pattern that
    # compose_runner/seeder use.
    for line in completed.stdout.splitlines():
        if not line.startswith("RESULT "):
            sys.stderr.write(line + "\n")

    return parse_cleanup_result(completed.stdout)


def run_stack_sync(
    local_stacks_dir: Path,
    enabled: list[str],
    *,
    rsync_runner: RsyncRunner | None = None,
    script_runner: ScriptRunner | None = None,
    remote_stacks_dir: str = _REMOTE_STACKS_DIR,
    host: str = "nexus",
) -> StackSyncResult:
    """End-to-end orchestrator: rsync each enabled stack, then cleanup
    disabled ones.

    Order matters: rsync runs FIRST so an enabled service that's
    already-disabled-on-the-server gets its compose.yml back before
    the cleanup loop has a chance to see it as "not in enabled" (it
    IS in enabled). The cleanup loop's enabled-list and the rsync
    loop's enabled-list are the same — there's no race window.
    """
    rsync_results = rsync_enabled_stacks(
        local_stacks_dir,
        enabled,
        rsync_runner=rsync_runner,
        remote_stacks_dir=remote_stacks_dir,
        host=host,
    )
    cleanup = cleanup_disabled_stacks(
        enabled,
        script_runner=script_runner,
        remote_stacks_dir=remote_stacks_dir,
    )
    return StackSyncResult(rsync=rsync_results, cleanup=cleanup)
