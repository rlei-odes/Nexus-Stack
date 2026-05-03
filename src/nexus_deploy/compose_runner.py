"""Parallel ``docker compose up`` runner (Phase 2 Modul 2.2a, #505).

Replaces the 130-line ``ssh nexus "..."`` heredoc in
``scripts/deploy.sh`` (the legacy block walked the enabled-services
list, expanded virtual services to their parent stacks, started each
stack via ``docker compose up -d --build`` in parallel via bash
``&``/``wait``, and verified each container made it into ``docker
ps``). Migration target: the foundation only — the per-service admin-
setup hooks (Wikijs, Dify, Metabase, Superset, LakeFS, OpenMetadata,
Gitea, Filestash, RedPanda) are scoped to Modul 2.2b and ship in a
follow-up PR.

Why server-side bash loop (consistent with infisical.py +
secret_sync.py + seeder.py): one SSH round-trip, bash background-
jobs are proven, the rendered script is testable as a string. Phase 3
(#505 Modul 3.1) replaces the bash rendering wholesale with paramiko
+ asyncio.

Eight rounds of hardening preserved (one regression test per round in
``tests/unit/test_compose_runner.py``):

R1. ``set -euo pipefail`` first executable line.
R2. Per-stack firewall override applied when
    ``docker-compose.firewall.yml`` exists on the server.
R3. Background-jobs + ``wait`` for parallel deploy; failed PIDs
    surface via the per-service exit code.
R4. ``docker ps`` verification — a container that ``compose up``
    "succeeded" but didn't actually start (e.g. immediate exit due
    to bad config) is counted as failed.
R5. Virtual-service deduplication: a parent stack started for one
    virtual service is NOT started a second time when another
    virtual service from the same parent appears.
R6. Deferred services skipped (woodpecker — depends on Gitea OAuth
    credentials, started later in deploy.sh).
R7. ``set -a`` + ``source /opt/docker-server/stacks/.env`` exports
    image-version pins to the compose-up environment.
R8. RESULT line emitted at end with started/failed counts; orchestrator
    parses it instead of grepping stdout for emoji.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from nexus_deploy import _remote

# Hardcoded deploy-config: virtual services, parent-stack mapping, and
# deferred services. These live in deploy.sh today (L1796/L1799/L1821);
# we lift them into module-level constants so the migration is byte-
# identical. Adding/removing entries here is the same amount of effort
# as touching deploy.sh. New stacks that don't fit this pattern just
# don't appear in either set.
_VIRTUAL_SERVICES: frozenset[str] = frozenset({"seaweedfs-filer", "seaweedfs-manager"})
_STACK_PARENTS: dict[str, str] = {
    "seaweedfs-filer": "seaweedfs",
    "seaweedfs-manager": "seaweedfs",
}
_DEFERRED_SERVICES: frozenset[str] = frozenset({"woodpecker"})

# Server-side stacks dir (mirror of deploy.sh's REMOTE_STACKS_DIR).
_REMOTE_STACKS_DIR = "/opt/docker-server/stacks"

# Server-side env file with image-version pins; sourced into the
# compose-up environment via `set -a; source ...; set +a`.
_REMOTE_GLOBAL_ENV = "/opt/docker-server/stacks/.env"

# RESULT-line shape (same wire-format family as infisical / secret_sync
# / seeder — `RESULT key=value key=value`). started/failed are counts;
# the human-readable per-service status reaches the operator via stderr
# warnings forwarded from the remote loop.
_RESULT_PATTERN = re.compile(
    r"^RESULT started=(?P<started>\d+) failed=(?P<failed>\d+)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ComposeUpResult:
    """Counters parsed from the remote ``RESULT`` line.

    ``started`` counts services that both reported ``compose up``
    success AND were observed in ``docker ps`` afterwards (R4
    invariant). ``failed`` counts everything else: compose-up
    non-zero exit, container missing from ``docker ps`` post-up,
    missing ``docker-compose.yml`` for an enabled service.
    """

    started: int
    failed: int

    @property
    def is_success(self) -> bool:
        """True iff zero failures."""
        return self.failed == 0


# ---------------------------------------------------------------------------
# Pure-logic helpers — virtual-service resolution.
# ---------------------------------------------------------------------------


def expand_targets(enabled: list[str]) -> tuple[list[str], list[str]]:
    """Resolve the enabled list into (parent_stacks, leaf_stacks).

    Parent stacks are derived from the ``_STACK_PARENTS`` map for any
    virtual service in ``enabled``. Each parent is added once even if
    multiple virtual children are enabled (R5). Leaf stacks are the
    remaining services minus virtuals, parents-already-included, and
    deferred services (R6).

    Returns the two lists in source order (caller sees the same order
    deploy.sh did, which the operator relies on for log debugging).
    """
    parents: list[str] = []
    seen_parents: set[str] = set()
    for svc in enabled:
        parent = _STACK_PARENTS.get(svc)
        if parent and parent not in seen_parents:
            parents.append(parent)
            seen_parents.add(parent)

    leaves: list[str] = []
    seen_leaves: set[str] = set()
    for svc in enabled:
        if svc in _VIRTUAL_SERVICES:
            continue
        if svc in seen_parents:
            continue  # already covered as a parent above
        if svc in _DEFERRED_SERVICES:
            continue
        if svc in seen_leaves:
            continue
        leaves.append(svc)
        seen_leaves.add(svc)

    return parents, leaves


# ---------------------------------------------------------------------------
# Bash rendering — produces the server-side script that
# `_remote.ssh_run_script` will exec via stdin.
# ---------------------------------------------------------------------------


def render_remote_script(
    *,
    parents: list[str],
    leaves: list[str],
    dify_storage_prep: bool = False,
    stacks_dir: str = _REMOTE_STACKS_DIR,
    global_env: str = _REMOTE_GLOBAL_ENV,
) -> str:
    """Render the bash that does parallel ``docker compose up`` + verify.

    All inputs are shlex-quoted. Service names land in a bash array
    (one per line, quoted) so spaces / special chars in a hypothetical
    future stack name can't break the loop.

    The script:
      1. ``set -euo pipefail``, ``set -a``+source the global env
         (image-version pins).
      2. Pre-deploy hooks (e.g. Dify storage perms) gated on flags.
      3. Spawn ``docker compose up -d --build`` in the background
         for every parent stack AND every leaf stack — PIDs from
         both tiers accumulate in a single ``PIDS`` array, no
         barrier between them. (Acceptable because parents and
         leaves don't share docker-compose YAML or container
         dependencies in practice — the parent/leaf split is just
         a virtual-service-mapping convention, not a startup-order
         constraint.) Each leaf invocation also applies
         ``-f docker-compose.firewall.yml`` when present on disk.
      4. ``wait`` each PID, then verify the container is in
         ``docker ps`` (R4 — exact-anchor grep). Per-service ✓
         lines go to stdout; ✗ lines go to stderr (so workflow-
         log filtering can split happy-path noise from real
         errors).
      5. Emit the RESULT line on stdout.
    """
    stacks_q = shlex.quote(stacks_dir)
    env_q = shlex.quote(global_env)
    parents_q = " ".join(shlex.quote(p) for p in parents)
    leaves_q = " ".join(shlex.quote(le) for le in leaves)

    dify_block = ""
    if dify_storage_prep:
        # Mirror deploy.sh:1823-1827 — Dify API/worker run as uid 1001,
        # storage + plugins dirs need ownership match. Idempotent.
        dify_block = """
mkdir -p /mnt/nexus-data/dify/storage /mnt/nexus-data/dify/plugins
chown -R 1001:1001 /mnt/nexus-data/dify/storage /mnt/nexus-data/dify/plugins
"""

    return f"""set -euo pipefail

STACKS_DIR={stacks_q}
GLOBAL_ENV={env_q}

# Source image-version pins so compose-up sees the right tags.
if [ -f "$GLOBAL_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$GLOBAL_ENV"
    set +a
fi

PARENTS=({parents_q})
LEAVES=({leaves_q})
{dify_block}
STARTED=0
FAILED=0
PIDS=()
NAMES=()

# 1. Start parent stacks first (parallel).
for svc in "${{PARENTS[@]}}"; do
    if [ -f "$STACKS_DIR/$svc/docker-compose.yml" ]; then
        ( cd "$STACKS_DIR/$svc" && docker compose up -d --build 2>&1 ) &
        PIDS+=($!)
        NAMES+=("$svc")
    else
        echo "  ⚠ docker-compose.yml missing for parent $svc" >&2
        FAILED=$((FAILED+1))
    fi
done

# 2. Start leaf stacks in parallel, applying firewall override if present.
for svc in "${{LEAVES[@]}}"; do
    if [ ! -f "$STACKS_DIR/$svc/docker-compose.yml" ]; then
        echo "  ⚠ docker-compose.yml missing for $svc" >&2
        FAILED=$((FAILED+1))
        continue
    fi
    if [ -f "$STACKS_DIR/$svc/docker-compose.firewall.yml" ]; then
        ( cd "$STACKS_DIR/$svc" && docker compose -f docker-compose.yml -f docker-compose.firewall.yml up -d --build 2>&1 ) &
    else
        ( cd "$STACKS_DIR/$svc" && docker compose up -d --build 2>&1 ) &
    fi
    PIDS+=($!)
    NAMES+=("$svc")
done

# 3. Wait for all PIDs, verify container is running for each.
for i in "${{!PIDS[@]}}"; do
    pid=${{PIDS[$i]}}
    name=${{NAMES[$i]}}
    if wait "$pid"; then
        # docker ps -f name=^foo$ excludes substring matches (e.g. "foo-bar" wouldn't match "^foo$")
        if docker ps --format '{{{{.Names}}}}' | grep -q "^${{name}}$"; then
            STARTED=$((STARTED+1))
            echo "  ✓ $name started and running"
        else
            FAILED=$((FAILED+1))
            echo "  ✗ $name compose up succeeded but container not in 'docker ps'" >&2
        fi
    else
        rc=$?
        FAILED=$((FAILED+1))
        echo "  ✗ $name compose up failed (rc=$rc)" >&2
    fi
done

echo "RESULT started=$STARTED failed=$FAILED"
"""


def parse_result(stdout: str) -> ComposeUpResult | None:
    """Extract the ``RESULT`` line from remote stdout.

    Returns None if no parseable RESULT line — caller (CLI) maps that
    to the same hard-failure path missing-stdin would.
    """
    match = _RESULT_PATTERN.search(stdout)
    if match is None:
        return None
    g = match.groupdict()
    return ComposeUpResult(started=int(g["started"]), failed=int(g["failed"]))


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


ScriptRunner = Callable[[str], subprocess.CompletedProcess[str]]


def run_compose_up(
    enabled: list[str],
    *,
    dify_storage_prep: bool | None = None,
    script_runner: ScriptRunner | None = None,
) -> ComposeUpResult:
    """Render → exec → parse.

    ``dify_storage_prep`` defaults to True iff ``"dify"`` is in
    ``enabled`` — caller can override (tests pass False to skip the
    chown block, production lets the default fire).

    Returns ``ComposeUpResult(started=0, failed=len(enabled))`` if the
    remote script produced no parseable RESULT line — same defensive
    parse pattern as seeder.py / secret_sync.py.

    ``script_runner`` is a dependency-injection seam for tests;
    production callers leave it None.
    """
    parents, leaves = expand_targets(enabled)
    actual_dify = dify_storage_prep if dify_storage_prep is not None else "dify" in enabled

    script = render_remote_script(parents=parents, leaves=leaves, dify_storage_prep=actual_dify)

    run_script = script_runner or (lambda s: _remote.ssh_run_script(s))
    completed = run_script(script)

    # Forward remote per-service ✓/✗ + warnings to local stderr (Modul-1.2
    # Round-4 pattern). The RESULT wire-format line is stripped.
    for line in completed.stdout.splitlines():
        if not line.startswith("RESULT "):
            sys.stderr.write(line + "\n")

    result = parse_result(completed.stdout)
    if result is None:
        # No RESULT — count every requested service as failed (mirrors
        # seeder.py's assumption that none of them landed).
        return ComposeUpResult(started=0, failed=len(parents) + len(leaves))
    return result
