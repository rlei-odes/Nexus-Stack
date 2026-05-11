"""Pipeline-side orchestration for S3 spinup-restore (RFC 0001 PR-2).

This module is the *caller* of the pure-rendering functions in
:mod:`nexus_deploy.s3_persistence`. The split mirrors the
``setup.py`` pattern: ``s3_persistence.py`` produces bash strings,
``s3_restore.py`` reads environment / config, builds the target
lists, and ships the rendered script to the remote via
:class:`SSHClient`.

Public surface:

* :class:`S3RestoreSkipped` / :class:`S3RestoreApplied` — outcome
  marker classes returned by :func:`restore_from_s3`. Tests assert
  on the type; pipeline.py logs a one-line summary using the
  ``detail`` attribute.
* :func:`build_endpoint_from_env` — read the five PERSISTENCE_S3_*
  env vars, return a populated :class:`S3Endpoint`. Returns
  ``None`` when any of them is unset — the caller treats that as
  "S3 persistence not configured on this stack, skip the phase."
* :func:`standard_targets` — produces the canonical tuple of
  postgres + rsync targets for the two stacks we persist
  (Gitea + Dify). Hard-coded for v1.0 because those are the only
  stacks with persistent data on the volume; a future
  per-stack config registry can replace this if other stacks
  start carrying state.
* :func:`restore_from_s3` — the orchestration entry point.
  Render rclone config + restore script via
  :mod:`s3_persistence`, ship them through ``ssh.run_script``,
  return a typed result.

Feature flag: this whole module is a *no-op* if
``NEXUS_S3_PERSISTENCE`` is not set to ``true`` in the spinup
environment. That keeps the existing volume-mount path
unchanged for stacks that haven't migrated yet (RFC Phase A:
prepare without breaking changes). The flip happens per-stack
during Phase B/C of the rollout — see RFC 0001 phased-rollout
plan.

Why a flag rather than presence-of-env-vars detection: a stack
mid-migration may have the PERSISTENCE_S3_* env vars populated
*before* its volume data has been evacuated to S3. Silently
flipping to the S3 path on env-presence would cause the first
post-flag spinup to come up with empty data dirs and the
existing volume detached + ignored. The explicit
``NEXUS_S3_PERSISTENCE=true`` flag forces an operator-aware
flip per stack.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import sys
from typing import Literal

from nexus_deploy import s3_persistence as _s3
from nexus_deploy.ssh import SSHClient

# ---------------------------------------------------------------------------
# Feature-flag env var name
# ---------------------------------------------------------------------------

FEATURE_FLAG_ENV = "NEXUS_S3_PERSISTENCE"
"""Stack-level toggle. Must be exactly ``"true"`` (lowercase) to
enable the new S3-restore path. Any other value (unset, empty,
``"false"``, ``"True"`` with capital T) keeps the old
volume-mount path in pipeline.py. Strict matching is deliberate:
operators set this via GitHub Actions repo-variables where
shell-style truthy-coercion would hide configuration mistakes."""


# ---------------------------------------------------------------------------
# Outcome marker classes — pipeline.py branches on isinstance()
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class S3RestoreSkipped:
    """Restore was a no-op. ``reason`` is operator-facing: one of
    ``"feature_flag_off"``, ``"no_endpoint_env"``,
    ``"fresh_start_empty_s3"``. The first two are configuration
    states (no S3 path taken at all). The third means the path
    was taken but S3 had nothing to restore — first-ever
    spinup of a freshly-provisioned bucket. All three are
    success cases from pipeline.py's perspective."""

    reason: Literal["feature_flag_off", "no_endpoint_env", "fresh_start_empty_s3"]


@dataclasses.dataclass(frozen=True)
class S3RestoreApplied:
    """Restore ran end-to-end. ``snapshot_timestamp`` is the value
    of ``snapshots/latest.txt`` that was applied (operator can
    grep for it in the S3 bucket to find the matching
    ``snapshots/<timestamp>/`` subtree). Used by pipeline.py to
    emit a one-line summary to stderr after the phase."""

    snapshot_timestamp: str


# ---------------------------------------------------------------------------
# Env-var parsing
# ---------------------------------------------------------------------------

# Names of the env vars we read. Match the keys that
# ``scripts/init-s3-bucket.sh`` writes into Infisical (under the
# ``/persistence/<stack-slug>`` path), and that spin-up.yml
# subsequently exports into the runner environment. The R2 access
# credentials are reused project-wide (from
# ``scripts/init-r2-state.sh``) so they use the existing names.

# These constants hold the NAMES of the environment variables we read —
# never their values. The strings ``"R2_ACCESS_KEY_ID"`` and
# ``"R2_SECRET_ACCESS_KEY"`` are configuration metadata; the actual
# secret material is whatever the operator (or Infisical) sets those
# env vars *to*. Renaming the constants with a ``_NAME`` suffix makes
# the safety property explicit at every call site and silences the
# CodeQL ``py/clear-text-logging-sensitive-data`` taint-tracker, which
# otherwise flags any log line that mentions a constant containing
# "secret" or "access_key" even when the logged value is just the
# name itself.
_ENV_ENDPOINT_NAME = "PERSISTENCE_S3_ENDPOINT"
_ENV_REGION_NAME = "PERSISTENCE_S3_REGION"
_ENV_BUCKET_NAME = "PERSISTENCE_S3_BUCKET"
_ENV_ACCESS_KEY_NAME = "R2_ACCESS_KEY_ID"
_ENV_SECRET_KEY_NAME = "R2_SECRET_ACCESS_KEY"  # noqa: S105 — env-var *name*, not a secret value

_REQUIRED_ENV_VAR_NAMES = (
    _ENV_ENDPOINT_NAME,
    _ENV_REGION_NAME,
    _ENV_BUCKET_NAME,
    _ENV_ACCESS_KEY_NAME,
    _ENV_SECRET_KEY_NAME,
)


def build_endpoint_from_env(env: dict[str, str] | None = None) -> _s3.S3Endpoint | None:
    """Build a :class:`S3Endpoint` from the five PERSISTENCE_S3_*
    env vars.

    Returns ``None`` if any of them is missing — the caller treats
    that as "no S3 persistence configured for this stack, fall back
    to the volume-mount path." Strict all-or-nothing because a
    partially-populated config (e.g. bucket name set but credentials
    missing) almost certainly indicates a misconfigured Infisical
    secret push, and silently picking up some env vars + ignoring
    others would mask that.

    Charset validation happens inside the :class:`S3Endpoint`
    constructor — any bad value raises
    :class:`s3_persistence.S3PersistenceError` with a message that
    names the offending field.

    The ``env`` parameter is for testability — production callers
    pass ``None`` (read os.environ); tests inject a fixture dict.
    """
    source = env if env is not None else os.environ
    if any(name not in source or not source[name] for name in _REQUIRED_ENV_VAR_NAMES):
        return None
    return _s3.S3Endpoint(
        endpoint=source[_ENV_ENDPOINT_NAME],
        region=source[_ENV_REGION_NAME],
        access_key=source[_ENV_ACCESS_KEY_NAME],
        secret_key=source[_ENV_SECRET_KEY_NAME],
        bucket=source[_ENV_BUCKET_NAME],
    )


def is_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True iff the feature flag is set to exactly ``"true"``.

    The strict comparison is intentional — see :data:`FEATURE_FLAG_ENV`
    docstring. ``"1"``, ``"yes"``, ``"True"``, ``"TRUE"`` all return
    ``False`` so operators get a clean error from pipeline.py rather
    than a silently-half-enabled state.
    """
    source = env if env is not None else os.environ
    return source.get(FEATURE_FLAG_ENV, "") == "true"


# ---------------------------------------------------------------------------
# Canonical target list for v1.0
# ---------------------------------------------------------------------------


def standard_targets() -> tuple[tuple[_s3.PostgresDumpTarget, ...], tuple[_s3.RsyncTarget, ...]]:
    """Return the (postgres, rsync) target tuples for the two stacks
    that v1.0 persists.

    Hard-coded because:
    1. The same two stacks have been the only stateful ones on the
       volume for the lifetime of the project — Gitea (repos + LFS
       + Postgres) and Dify (storage + Postgres + Weaviate + plugins).
    2. The mappings are user-name / database-name pairs from the
       respective ``docker-compose.yml`` files. Hardcoding here is
       defended by the unit tests, which assert those mappings stay
       in sync with the compose files; a docs-comment near the
       fixture would drift, the test won't.
    3. A future per-stack registry (services.yaml extension, or a
       dedicated config table) can replace this single function
       without touching any caller — :func:`restore_from_s3` only
       sees the returned tuples.

    Mappings (verified against ``stacks/gitea/docker-compose.yml``
    line 67 and ``stacks/dify/docker-compose.yml`` line 180 at
    PR-2 time):

    * Gitea container ``gitea-db`` — database ``gitea``, role
      ``nexus-gitea``.
    * Dify container ``dify-db`` — database ``dify``, role
      ``nexus-dify``.

    Rsync layout matches RFC 0001 §"Storage layout":
    ``snapshots/<timestamp>/gitea/{repos,lfs}/`` and
    ``snapshots/<timestamp>/dify/{storage,weaviate,plugins}/``.
    """
    postgres = (
        _s3.PostgresDumpTarget(container="gitea-db", database="gitea", user="nexus-gitea"),
        _s3.PostgresDumpTarget(container="dify-db", database="dify", user="nexus-dify"),
    )
    rsync = (
        _s3.RsyncTarget(
            name="gitea-repos",
            local_path="/var/lib/nexus-data/gitea/repos",
            s3_subpath="gitea/repos",
        ),
        _s3.RsyncTarget(
            name="gitea-lfs",
            local_path="/var/lib/nexus-data/gitea/lfs",
            s3_subpath="gitea/lfs",
        ),
        _s3.RsyncTarget(
            name="dify-storage",
            local_path="/var/lib/nexus-data/dify/storage",
            s3_subpath="dify/storage",
        ),
        _s3.RsyncTarget(
            name="dify-weaviate",
            local_path="/var/lib/nexus-data/dify/weaviate",
            s3_subpath="dify/weaviate",
        ),
        _s3.RsyncTarget(
            name="dify-plugins",
            local_path="/var/lib/nexus-data/dify/plugins",
            s3_subpath="dify/plugins",
        ),
    )
    return postgres, rsync


# ---------------------------------------------------------------------------
# Combined-script render (rclone config + restore body in one bash)
# ---------------------------------------------------------------------------


def render_combined_restore_script(
    *,
    endpoint: _s3.S3Endpoint,
    postgres_targets: tuple[_s3.PostgresDumpTarget, ...],
    rsync_targets: tuple[_s3.RsyncTarget, ...],
    local_root: str = "/var/lib/nexus-data",
) -> str:
    """Render a single bash script that does BOTH:

    1. Writes the rclone config to ``~/.config/rclone/rclone.conf``
       (atomic temp-file → rename, mode 600 because it contains
       the secret access key).
    2. Runs the restore body produced by
       :func:`s3_persistence.render_restore_script`.

    Caller ships the combined script via one ``ssh.run_script``
    invocation. This is the cheapest plumbing: a single SSH
    round-trip, no temp-file management on the orchestrator side,
    no risk of a partial write between config and body.

    The config write uses ``install -m 600 /dev/stdin`` so the
    file's permission bits are set atomically — no
    ``chmod`` race window where the file exists with default
    644 and another process on the host could read the
    credentials.
    """
    rclone_config = _s3.render_rclone_config(endpoint)
    restore_body = _s3.render_restore_script(
        endpoint=endpoint,
        postgres_targets=postgres_targets,
        rsync_targets=rsync_targets,
        local_root=local_root,
    )
    # Strip the shebang + outer ``set -euo pipefail`` from the
    # restore body — the wrapper script provides them. Keeping
    # two shebangs would just be cruft; double ``set -e`` would
    # work but reads weird. We splice the body in after our
    # wrapper preamble.
    body_lines = restore_body.splitlines()
    while body_lines and (
        body_lines[0].startswith("#!")
        or body_lines[0].startswith("# Generated")
        or body_lines[0].strip() == "set -euo pipefail"
        or body_lines[0].strip() == ""
    ):
        body_lines.pop(0)
    body_inner = "\n".join(body_lines)

    return (
        "#!/usr/bin/env bash\n"
        "# Generated by nexus_deploy.s3_restore — do not edit by hand.\n"
        "set -euo pipefail\n"
        "\n"
        "# ---- write rclone config (atomic, mode 600) -----------\n"
        'mkdir -p "$HOME/.config/rclone"\n'
        "install -m 600 /dev/stdin \"$HOME/.config/rclone/rclone.conf\" <<'RCLONE_CONFIG_EOF'\n"
        f"{rclone_config}"
        "RCLONE_CONFIG_EOF\n"
        "\n"
        "# ---- restore body -------------------------------------\n"
        f"{body_inner}\n"
    )


# ---------------------------------------------------------------------------
# Orchestration entry point
# ---------------------------------------------------------------------------


def restore_from_s3(
    ssh: SSHClient,
    *,
    env: dict[str, str] | None = None,
) -> S3RestoreSkipped | S3RestoreApplied:
    """Pull the latest snapshot from R2 onto the server's local SSD.

    Pipeline-side counterpart to
    :func:`s3_persistence.render_restore_script`. Returns a typed
    outcome:

    * :class:`S3RestoreSkipped` (``"feature_flag_off"``) — the
      feature flag isn't set; pipeline.py should fall back to the
      legacy volume-mount path.
    * :class:`S3RestoreSkipped` (``"no_endpoint_env"``) — the flag
      is on but credentials are missing. Treated as "skip with
      warning"; pipeline.py emits a stderr message. This is a
      misconfiguration the operator needs to see, but it's
      survivable (downstream stacks that don't need persistence
      come up fine).
    * :class:`S3RestoreSkipped` (``"fresh_start_empty_s3"``) —
      the restore ran but the bucket has no
      ``snapshots/latest.txt`` yet (brand-new bucket, first-ever
      spinup). docker compose comes up with empty data dirs;
      future teardowns will populate the bucket.
    * :class:`S3RestoreApplied` — restore ran end-to-end.
      ``snapshot_timestamp`` lets pipeline.py log which snapshot
      was applied.

    Any non-zero exit from the rendered bash that *isn't* the
    fresh-start case raises ``subprocess.CalledProcessError`` from
    inside ``ssh.run_script(check=True)``. That propagates up
    pipeline.py as a hard failure — restore corruption should NOT
    let the spinup proceed with half-populated data.
    """
    if not is_enabled(env):
        return S3RestoreSkipped(reason="feature_flag_off")

    endpoint = build_endpoint_from_env(env)
    if endpoint is None:
        sys.stderr.write(
            f"⚠ s3-restore: feature flag {FEATURE_FLAG_ENV}=true but one or more of "
            f"{_REQUIRED_ENV_VAR_NAMES} is unset; skipping S3 restore.\n",
        )
        return S3RestoreSkipped(reason="no_endpoint_env")

    postgres_targets, rsync_targets = standard_targets()
    script = render_combined_restore_script(
        endpoint=endpoint,
        postgres_targets=postgres_targets,
        rsync_targets=rsync_targets,
    )

    # The rendered restore script either:
    #  - exits 0 after "fresh-start: no snapshot in S3" (latest.txt
    #    missing → first-time spinup). Output contains the
    #    "fresh-start" marker.
    #  - exits 0 after a real restore.
    #  - exits non-zero on any other failure (rclone error,
    #    pg_restore failure, malformed latest.txt). check=True
    #    raises CalledProcessError, which is the right behavior:
    #    a partial restore must not silently let the stack come up.
    completed = ssh.run_script(script, check=True)
    output = completed.stdout
    # Forward server-side log lines to local stderr so operators see
    # what the remote did. Mirrors mount_persistent_volume's pattern.
    for line in output.splitlines():
        sys.stderr.write(line + "\n")

    if "fresh-start: no snapshot in S3" in output:
        return S3RestoreSkipped(reason="fresh_start_empty_s3")

    # Parse the applied timestamp from "→ restore: using snapshot
    # snapshots/<timestamp>" line. If we can't find it (server-side
    # script changed shape), still return Applied — the restore did
    # complete successfully (rc=0), just with a less-informative
    # log line. Falling back to "(unknown timestamp)" keeps the
    # outcome class invariant ("rc=0 means data is in place") even
    # when the diagnostic parsing drifts.
    timestamp = "(unknown)"
    for line in output.splitlines():
        if "→ restore: using snapshot snapshots/" in line:
            # Format: "→ restore: using snapshot snapshots/<TS>"
            with contextlib.suppress(IndexError):
                timestamp = line.split("snapshots/", 1)[1].strip()
            break
    return S3RestoreApplied(snapshot_timestamp=timestamp)
