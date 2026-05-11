"""Tests for nexus_deploy.s3_persistence (RFC 0001 foundation).

Pure-rendering tests: we assert on the bash text and the manifest
JSON, no subprocess calls. The remote execution path is covered
separately in pipeline.py once it's wired up.

Coverage focus areas:

* :class:`S3Endpoint` charset gating — every malformed value is
  rejected at construction time, with a message that names the
  offending field.
* :class:`PostgresDumpTarget` charset gating — container,
  database and user identifiers all validated against shapes
  that are safe to interpolate into the rendered bash + SQL.
* Manifest round-trip — ``to_json`` → ``from_json`` is identity
  for valid input; corrupt input (bad JSON, wrong root type,
  unknown version, malformed components) raises
  :class:`S3PersistenceError` with a useful message rather than
  a confusing ``KeyError`` from indexing into bad data.
* Snapshot script invariants — required structure (``set -euo
  pipefail``, ordered phases), atomicity gate (``rclone check``
  exit code captured via ``PIPESTATUS`` so a check-itself failure
  isn't masked by ``|| true``), no shell injection from any
  interpolated value.
* Restore script invariants — graceful handling of the empty-S3
  case (fresh-start branch), drop+recreate around pg_restore,
  filesystem-before-postgres ordering, ``snapshots/latest.txt``
  shape validation.
* ``bash -n`` syntax check — the rendered scripts parse cleanly
  with bash's no-execute mode. Catches dangling heredocs,
  unmatched quotes etc. — bugs that don't surface in
  string-equality tests but break at runtime on the server. Full
  exec-with-stubs smoke tests are deferred to the pipeline-
  integration PR where the SSHClient runner is plumbed in.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus_deploy.s3_persistence import (
    RCLONE_PROFILE,
    ComponentSnapshot,
    PostgresDumpTarget,
    RsyncTarget,
    S3Endpoint,
    S3PersistenceError,
    SnapshotManifest,
    manifest_for_components,
    render_rclone_config,
    render_restore_script,
    render_snapshot_script,
)


def _bash_can_be_invoked() -> bool:
    return shutil.which("bash") is not None


# ---------------------------------------------------------------------------
# S3Endpoint validation
# ---------------------------------------------------------------------------


def test_s3endpoint_accepts_canonical_r2_values() -> None:
    """Smoke: the constructor doesn't reject a real R2 config."""
    e = S3Endpoint(
        endpoint="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        access_key="ABCDEFG1234567890",
        secret_key="abc123XYZ+/=_-",
        bucket="nexus-stefan-hslu",
    )
    assert e.region == "auto"
    assert e.bucket == "nexus-stefan-hslu"


def test_s3endpoint_accepts_canonical_hetzner_values() -> None:
    """Smoke: the module is endpoint-agnostic; a Hetzner Object
    Storage config also passes the gate (used by future migration
    tooling, not the v1.0 steady state)."""
    e = S3Endpoint(
        endpoint="https://fsn1.your-objectstorage.com",
        region="fsn1",
        access_key="ABCDEFG1234567890",
        secret_key="abc123XYZ+/=_-",
        bucket="nexus-stefan-hslu",
    )
    assert e.region == "fsn1"
    assert e.bucket == "nexus-stefan-hslu"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "abc123.r2.cloudflarestorage.com"),  # missing scheme
        ("endpoint", "ftp://abc123.r2.cloudflarestorage.com"),  # wrong scheme
    ],
)
def test_s3endpoint_rejects_non_http_endpoint(field: str, value: str) -> None:
    """Non-HTTP endpoints get caught — common copy-paste error
    where someone pastes the bucket name into the endpoint slot."""
    kwargs = {
        "endpoint": "https://abc123.r2.cloudflarestorage.com",
        "region": "fsn1",
        "access_key": "AKIAEXAMPLE",
        "secret_key": "secret123",
        "bucket": "nexus-test",
    }
    kwargs[field] = value
    with pytest.raises(S3PersistenceError, match="must start with http"):
        S3Endpoint(**kwargs)


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("region", "fsn1; rm -rf /", "region"),
        ("region", "FSN1", "region"),  # uppercase rejected
        ("bucket", "nexus stack", "bucket"),  # space rejected
        ("bucket", "ab", "bucket"),  # too short
        ("access_key", "key with spaces", "access_key"),
        ("secret_key", "secret with $bash", "secret_key"),
    ],
)
def test_s3endpoint_rejects_unsafe_charset(field: str, value: str, fragment: str) -> None:
    """Any value that could break out of bash interpolation gets
    caught at the constructor — the rendered script never sees an
    unsafe value."""
    kwargs = {
        "endpoint": "https://abc123.r2.cloudflarestorage.com",
        "region": "fsn1",
        "access_key": "AKIAEXAMPLE",
        "secret_key": "secret123",
        "bucket": "nexus-test",
    }
    kwargs[field] = value
    with pytest.raises(S3PersistenceError, match=fragment):
        S3Endpoint(**kwargs)


# ---------------------------------------------------------------------------
# rclone config
# ---------------------------------------------------------------------------


def test_render_rclone_config_emits_full_profile_block() -> None:
    """The block contains every key rclone needs to authenticate
    against R2. No accidental ``env_auth = true`` (which would
    silently fall back to ambient AWS env vars). The
    ``provider = Cloudflare`` switch is what tells rclone to
    apply R2-specific quirks."""
    e = S3Endpoint(
        endpoint="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        access_key="AKIA1234",
        secret_key="secret/key+abc=",
        bucket="nexus-stefan-hslu",
    )
    config = render_rclone_config(e)

    assert config.startswith(f"[{RCLONE_PROFILE}]\n")
    assert "type = s3\n" in config
    assert "provider = Cloudflare\n" in config
    assert "env_auth = false\n" in config
    assert "access_key_id = AKIA1234\n" in config
    assert "secret_access_key = secret/key+abc=\n" in config
    assert "endpoint = https://abc123.r2.cloudflarestorage.com\n" in config
    assert "region = auto\n" in config
    assert "acl = private\n" in config


def test_render_rclone_config_uses_module_level_profile_name() -> None:
    """Regression: render and script use the SAME profile name.
    Hardcoded literal would be fine but a constant means a future
    rename can't drift between the two render functions."""
    e = S3Endpoint(
        endpoint="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        access_key="AKIA",
        secret_key="secret",
        bucket="nexus-test",
    )
    config = render_rclone_config(e)
    snapshot = render_snapshot_script(
        endpoint=e,
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(),
        rsync_targets=(),
    )
    assert f"[{RCLONE_PROFILE}]" in config
    assert f"{RCLONE_PROFILE}:" in snapshot


# ---------------------------------------------------------------------------
# Manifest serialisation
# ---------------------------------------------------------------------------


def test_manifest_round_trip_is_identity() -> None:
    """to_json → from_json preserves every component."""
    original = SnapshotManifest(
        version=1,
        created_at="2026-05-10T20:00:00Z",
        stack="nexus-stefan-hslu",
        template_version="v0.56.0",
        components=(
            ComponentSnapshot(
                name="gitea-repos", path="gitea/repos", size_bytes=1024, sha256="abc123"
            ),
            ComponentSnapshot(
                name="dify-storage", path="dify/storage", size_bytes=2048, sha256="def456"
            ),
        ),
    )
    parsed = SnapshotManifest.from_json(original.to_json())
    assert parsed == original


def test_manifest_to_json_is_deterministic() -> None:
    """Sorted keys → same bytes for the same input. Important for
    rclone-check ETag stability across re-renders."""
    m = SnapshotManifest(stack="x", template_version="y", components=())
    assert m.to_json() == m.to_json()


def test_manifest_from_json_rejects_unknown_version() -> None:
    """A future v2 manifest read by a v1 client should hard-fail
    rather than silently truncate fields."""
    raw = json.dumps({"version": 2, "components": []})
    with pytest.raises(S3PersistenceError, match=r"version 2 .* not supported"):
        SnapshotManifest.from_json(raw)


def test_manifest_from_json_rejects_non_object_root() -> None:
    raw = "[]"
    with pytest.raises(S3PersistenceError, match="root must be an object"):
        SnapshotManifest.from_json(raw)


def test_manifest_from_json_rejects_invalid_json() -> None:
    with pytest.raises(S3PersistenceError, match="not valid JSON"):
        SnapshotManifest.from_json("{ not json")


def test_manifest_from_json_rejects_non_dict_component() -> None:
    """A component that's not a dict (e.g. a stray string) must
    surface as :class:`S3PersistenceError` with an actionable
    message, not a confusing TypeError. Promised in the
    docstring; the previous implementation indexed straight in
    and raised ``TypeError: string indices must be integers``."""
    raw = json.dumps({"version": 1, "components": ["not-a-dict"]})
    with pytest.raises(S3PersistenceError, match=r"components\[0\] must be an object"):
        SnapshotManifest.from_json(raw)


def test_manifest_from_json_rejects_component_missing_keys() -> None:
    """A component dict missing one of the four required keys is
    a manifest corruption — caller needs an actionable error,
    not a KeyError stack trace."""
    raw = json.dumps(
        {
            "version": 1,
            "components": [{"name": "x", "path": "x"}],  # no size_bytes/sha256
        }
    )
    with pytest.raises(S3PersistenceError, match=r"missing required key"):
        SnapshotManifest.from_json(raw)


def test_manifest_from_json_rejects_component_with_bad_size() -> None:
    """``size_bytes`` must be coerce-able to int. A string like
    'banana' would have raised ValueError mid-parse with the old
    code; we now wrap it to S3PersistenceError."""
    raw = json.dumps(
        {
            "version": 1,
            "components": [
                {
                    "name": "x",
                    "path": "x",
                    "size_bytes": "banana",  # invalid
                    "sha256": "abc",
                }
            ],
        }
    )
    with pytest.raises(S3PersistenceError, match="bad value"):
        SnapshotManifest.from_json(raw)


# ---------------------------------------------------------------------------
# PostgresDumpTarget charset gating (added in PR review round 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        # container — docker-service-name shape (Hetzner-region regex)
        ("container", "Gitea_DB", "container"),  # underscore + uppercase
        ("container", "gitea db", "container"),  # space
        ("container", "gitea;rm", "container"),  # injection attempt
        # database / user — strict PG identifier
        ("database", "drop table users", "database"),
        ("database", "1abc", "database"),  # leading digit
        ("database", 'gitea"', "database"),  # quote injection
        ("user", "user;DROP", "user"),
        ("user", "user with space", "user"),
    ],
)
def test_postgres_dump_target_rejects_unsafe_identifiers(
    field: str, value: str, fragment: str
) -> None:
    """Every value that could break out of bash interpolation OR
    SQL identifier interpolation must be rejected at construction
    time. The rendered bash + SQL never sees an unsafe value."""
    kwargs = {"container": "gitea-db", "database": "gitea", "user": "nexus-gitea"}
    kwargs[field] = value
    with pytest.raises(S3PersistenceError, match=fragment):
        PostgresDumpTarget(**kwargs)


def test_postgres_dump_target_accepts_canonical_values() -> None:
    """Smoke: real-world configs (``gitea-db`` / ``gitea`` /
    ``nexus-gitea``) pass the gate."""
    PostgresDumpTarget(container="gitea-db", database="gitea", user="nexus-gitea")
    PostgresDumpTarget(container="dify-db", database="dify", user="nexus_dify")
    PostgresDumpTarget(container="x-db-2", database="db_v2", user="role_admin")


# ---------------------------------------------------------------------------
# RsyncTarget charset gating (Copilot round-3 #3216323836 / #3216323852)
# ---------------------------------------------------------------------------


def test_rsync_target_accepts_canonical_values() -> None:
    """Smoke: the typical nexus-data layout passes the gate."""
    RsyncTarget(
        name="gitea-repos", local_path="/var/lib/nexus-data/gitea/repos", s3_subpath="gitea/repos"
    )
    RsyncTarget(
        name="dify-storage",
        local_path="/var/lib/nexus-data/dify/storage",
        s3_subpath="dify/storage",
    )


@pytest.mark.parametrize(
    "subpath",
    [
        "gitea/$(rm -rf /)",  # command substitution
        "gitea/`whoami`",  # backticks
        "gitea/repos with space",
        "/gitea/repos",  # leading slash
        "gitea/repos/",  # trailing slash
        "../gitea",  # parent ref
        "gitea/../etc",  # parent ref middle
        'gitea/"injection',  # quote
    ],
)
def test_rsync_target_rejects_unsafe_s3_subpath(subpath: str) -> None:
    """``s3_subpath`` is interpolated into double-quoted bash strings
    where ``shlex.quote`` doesn't help. Constructor must catch every
    shape that would corrupt the rendered bash or escape the bucket
    path."""
    with pytest.raises(S3PersistenceError, match="s3_subpath"):
        RsyncTarget(name="x", local_path="/var/lib/nexus-data/x", s3_subpath=subpath)


def test_rsync_target_rejects_relative_local_path() -> None:
    with pytest.raises(S3PersistenceError, match="local_path must be absolute"):
        RsyncTarget(name="x", local_path="relative/path", s3_subpath="x")


# ---------------------------------------------------------------------------
# S3Endpoint endpoint-URL charset gating (Copilot round-3 #3216323864)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fsn1.your-objectstorage.com\nextra_key = injection",
        "https://fsn1 .your-objectstorage.com",  # space
        "https://fsn1.your-objectstorage.com\r\n",
        "https://fsn1.your-objectstorage.com\t",
    ],
)
def test_s3endpoint_rejects_endpoint_with_whitespace_or_newlines(endpoint: str) -> None:
    """Whitespace/newlines in the endpoint URL would corrupt the
    rendered rclone config (split the value across multiple keys,
    inject extra config lines). Regression for Copilot round-3
    #3216323864."""
    with pytest.raises(S3PersistenceError, match="corrupt the rendered rclone config"):
        S3Endpoint(
            endpoint=endpoint,
            region="auto",
            access_key="AKIA",
            secret_key="secret",
            bucket="nexus-test",
        )


def test_manifest_for_components_helper_sorts_components() -> None:
    """Components map → sorted ComponentSnapshot tuple. Sorting
    matters for deterministic manifest bytes regardless of the
    order callers populate the map in."""
    m = manifest_for_components(
        stack="nexus-test",
        template_version="v0.56.0",
        created_at="2026-05-11T04:00:00Z",
        components={
            "z-stack": (10, "z-hash"),
            "a-stack": (20, "a-hash"),
        },
    )
    assert [c.name for c in m.components] == ["a-stack", "z-stack"]


def test_manifest_for_components_propagates_created_at() -> None:
    """Regression: ``created_at`` is now an actual parameter (was
    previously a no-op ``timestamp`` arg that the helper ignored).
    The value must land on the manifest's ``created_at`` field."""
    m = manifest_for_components(
        stack="nexus-test",
        template_version="v0.56.0",
        created_at="2026-05-11T04:00:00Z",
        components={},
    )
    assert m.created_at == "2026-05-11T04:00:00Z"


# ---------------------------------------------------------------------------
# Snapshot script structure
# ---------------------------------------------------------------------------


def _endpoint() -> S3Endpoint:
    return S3Endpoint(
        endpoint="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        access_key="AKIA1234",
        secret_key="secret123",
        bucket="nexus-test",
    )


def test_snapshot_script_has_bash_safety_pragmas() -> None:
    """Every rendered script must start with the standard bash
    pragmas — silent failure mid-snapshot would leave inconsistent
    S3 state."""
    script = render_snapshot_script(
        endpoint=_endpoint(),
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(),
        rsync_targets=(),
    )
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script


def test_snapshot_script_orders_phases_correctly() -> None:
    """The phase order is the atomicity contract: stop → dump →
    upload → verify → point latest. Reorder would silently break
    the guarantee that ``snapshots/latest.txt`` only updates after
    upload succeeded.

    We use ``docker compose stop`` (graceful 10s drain), not
    ``pause`` (SIGSTOP via cgroup freezer, hard-kills in-flight
    writes mid-transaction).
    """
    script = render_snapshot_script(
        endpoint=_endpoint(),
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(
            PostgresDumpTarget(container="gitea-db", database="gitea", user="nexus-gitea"),
        ),
        rsync_targets=(
            RsyncTarget(
                name="gitea-repos",
                local_path="/var/lib/nexus-data/gitea/repos",
                s3_subpath="gitea/repos",
            ),
        ),
        stop_compose_files=("/opt/docker-server/stacks/gitea/docker-compose.yml",),
    )
    # Locate each phase via a stable substring + assert ordering.
    stop_pos = script.find("compose -f")
    dump_pos = script.find("pg_dump")
    upload_pos = script.find("rclone sync")
    check_pos = script.find("rclone check")
    latest_pos = script.find("snapshots/latest.txt")
    assert stop_pos < dump_pos < upload_pos < check_pos < latest_pos
    # Regression: stop, not pause, AND no `... || echo` blanket
    # error-swallowing (per CLAUDE.md "Never silently swallow
    # errors in critical operations"). The current implementation
    # uses ``if docker compose ps -q ... then stop`` so a genuine
    # `stop` failure bubbles via ``set -e``.
    assert "docker compose -f" in script
    assert "stop" in script  # rendered command verb is `stop`
    assert "pause" not in script
    # No blanket "|| echo non-fatal" masking on the stop step.
    stop_block = script.split("→ snapshot: stopping compose stacks")[1].split("→ snapshot:")[0]
    assert "stop || echo" not in stop_block


def test_snapshot_script_omits_compose_stop_when_no_files_passed() -> None:
    """No compose files passed → no docker compose calls at all.
    Avoids the ``no compose files`` echo-only no-op block."""
    script = render_snapshot_script(
        endpoint=_endpoint(),
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(),
        rsync_targets=(),
    )
    assert "docker compose" not in script


def test_snapshot_script_omits_postgres_phase_when_no_targets() -> None:
    """A compose-only stack (no Postgres) shouldn't render the
    pg_dump block — and shouldn't try to upload an empty dump
    directory either."""
    script = render_snapshot_script(
        endpoint=_endpoint(),
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(),
        rsync_targets=(RsyncTarget(name="r", local_path="/var/lib/nexus-data/x", s3_subpath="x"),),
    )
    assert "pg_dump" not in script
    assert "uploading postgres dumps" not in script


def test_snapshot_script_rejects_unsafe_stack_slug() -> None:
    with pytest.raises(S3PersistenceError, match="stack_slug"):
        render_snapshot_script(
            endpoint=_endpoint(),
            stack_slug="nexus stack with spaces",
            template_version="v",
            timestamp="t",
            postgres_targets=(),
            rsync_targets=(),
        )


def test_snapshot_script_rejects_unsafe_timestamp() -> None:
    """A timestamp containing shell metacharacters could break out
    of the rendered ``$TIMESTAMP=...`` interpolation."""
    with pytest.raises(S3PersistenceError, match="timestamp"):
        render_snapshot_script(
            endpoint=_endpoint(),
            stack_slug="nexus-test",
            template_version="v0.56.0",
            timestamp="2026-05-10T20:00:00Z",  # colons rejected
            postgres_targets=(),
            rsync_targets=(),
        )


def test_snapshot_script_atomicity_gate_distinguishes_two_failure_modes() -> None:
    """The atomicity gate must distinguish (a) rclone-check itself
    erroring (auth/network/quota) from (b) drift found via the
    pipe-grep on rclone-check's --combined output. Both rcs are
    captured via ``PIPESTATUS`` so neither can be silently masked
    by ``|| true``."""
    script = render_snapshot_script(
        endpoint=_endpoint(),
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(),
        rsync_targets=(),
    )
    assert "rclone check" in script
    # Both PIPESTATUS captures must be present in the verify_one helper.
    # Pipeline is `rclone | tee | grep`, so PIPESTATUS indexes are:
    #   [0] = rclone (the integrity check), [1] = tee (always 0),
    #   [2] = grep (0 = drift markers found, 1 = clean).
    # An earlier revision had drift_rc=[1] (tee) which made the gate
    # report "drift" on every snapshot — locking in [2] (grep) here
    # is the regression test.
    assert "rclone_rc=${PIPESTATUS[0]}" in script
    assert "drift_rc=${PIPESTATUS[2]}" in script
    # And both abort messages must distinguish the two modes.
    assert "snapshot-failed: rclone check ${label} errored" in script
    assert "snapshot-failed: rclone check ${label} found drift" in script


def test_snapshot_script_verifies_every_rsync_target() -> None:
    """The verify gate must run an rclone check for ``$WORKDIR``
    (manifest + postgres dumps) AND one per ``RsyncTarget`` — the
    filesystem trees uploaded via ``rclone sync {local} {dst}``
    are NOT in $WORKDIR, so a $WORKDIR-only check would have left
    the bulk of the persisted state unverified.

    Regression for Copilot round-3 #3216323822."""
    script = render_snapshot_script(
        endpoint=_endpoint(),
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(),
        rsync_targets=(
            RsyncTarget(
                name="gitea-repos",
                local_path="/var/lib/nexus-data/gitea/repos",
                s3_subpath="gitea/repos",
            ),
            RsyncTarget(
                name="dify-storage",
                local_path="/var/lib/nexus-data/dify/storage",
                s3_subpath="dify/storage",
            ),
        ),
    )
    # Workdir verify (manifest + postgres dumps)
    assert 'verify_one "$WORKDIR" "$BUCKET/$SNAPSHOT_PREFIX" "workdir(manifest+postgres)"' in script
    # Plus per-rsync-target verify
    assert "verify_one /var/lib/nexus-data/gitea/repos" in script
    assert "verify_one /var/lib/nexus-data/dify/storage" in script
    # Final gate before pointing snapshots/latest
    assert 'if [ "$verify_failed" -ne 0 ]; then' in script
    assert "not pointing snapshots/latest at $TIMESTAMP" in script


# ---------------------------------------------------------------------------
# Restore script structure
# ---------------------------------------------------------------------------


def test_restore_script_has_bash_safety_pragmas() -> None:
    script = render_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(),
        rsync_targets=(),
    )
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script


def test_restore_script_handles_empty_s3_gracefully() -> None:
    """First-time spinup → no snapshot in S3 → script must exit
    0, not blow up. The pipeline then proceeds with a clean
    docker-compose-up just like a brand-new install."""
    script = render_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(),
        rsync_targets=(),
    )
    assert "fresh-start" in script
    assert "exit 0" in script


def test_restore_script_drops_database_before_pg_restore() -> None:
    """A restore against a running Postgres with existing rows
    would conflict on PK; the drop+recreate keeps the pg_restore
    deterministic.

    SQL identifiers are now ALWAYS double-quoted because real role
    names use hyphens (``nexus-gitea``) which would be invalid as
    unquoted SQL — the previous unquoted form would have produced
    ``OWNER nexus-gitea`` which Postgres rejects as a syntax error.
    """
    script = render_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(
            PostgresDumpTarget(container="gitea-db", database="gitea", user="nexus-gitea"),
        ),
        rsync_targets=(),
    )
    assert 'DROP DATABASE IF EXISTS "gitea"' in script
    assert 'CREATE DATABASE "gitea" OWNER "nexus-gitea"' in script
    # CLI args don't need SQL quoting — pg_restore's -U/-d take plain
    # values via argv, not embedded SQL.
    assert "pg_restore -U nexus-gitea -d gitea" in script


def test_restore_script_pulls_filesystem_trees_before_postgres() -> None:
    """Order matters: restore the FS first (in case any postgres
    init script reads a config file from the FS), THEN pg_restore.
    Reversing this ordering would race on first start."""
    script = render_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(
            PostgresDumpTarget(container="gitea-db", database="gitea", user="nexus-gitea"),
        ),
        rsync_targets=(
            RsyncTarget(
                name="r", local_path="/var/lib/nexus-data/gitea/repos", s3_subpath="gitea/repos"
            ),
        ),
    )
    fs_pos = script.find("pulling filesystem trees")
    pg_pos = script.find("pulling postgres dumps")
    assert 0 < fs_pos < pg_pos


def test_restore_script_validates_timestamp_from_s3() -> None:
    """``snapshots/latest.txt`` is operator-influenced (an admin
    could in theory write it) — the script must validate the
    contents before substituting it into a path."""
    script = render_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(),
        rsync_targets=(),
    )
    assert '[[ ! "$TIMESTAMP" =~ ^[0-9A-Za-z_-]+$ ]]' in script
    assert "restore-failed: latest.txt has invalid timestamp" in script


# ---------------------------------------------------------------------------
# Smoke: the rendered scripts are syntactically valid bash
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _bash_can_be_invoked(), reason="bash not available")
def test_rendered_snapshot_script_is_syntactically_valid_bash(tmp_path: Path) -> None:
    """``bash -n`` parses the rendered text without complaint.
    Catches dangling heredocs, unmatched quotes, etc. — the kind
    of bug that doesn't show up in a string-comparison test but
    breaks at runtime on the server."""
    script = render_snapshot_script(
        endpoint=_endpoint(),
        stack_slug="nexus-test",
        template_version="v0.56.0",
        timestamp="20260510T120000Z",
        postgres_targets=(
            PostgresDumpTarget(container="gitea-db", database="gitea", user="nexus-gitea"),
        ),
        rsync_targets=(
            RsyncTarget(name="r", local_path="/var/lib/nexus-data/gitea", s3_subpath="gitea"),
        ),
        stop_compose_files=("/opt/docker-server/stacks/gitea/docker-compose.yml",),
    )
    script_path = tmp_path / "snapshot.sh"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


@pytest.mark.skipif(not _bash_can_be_invoked(), reason="bash not available")
def test_rendered_restore_script_is_syntactically_valid_bash(tmp_path: Path) -> None:
    script = render_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(
            PostgresDumpTarget(container="gitea-db", database="gitea", user="nexus-gitea"),
        ),
        rsync_targets=(
            RsyncTarget(name="r", local_path="/var/lib/nexus-data/gitea", s3_subpath="gitea"),
        ),
    )
    script_path = tmp_path / "restore.sh"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"
