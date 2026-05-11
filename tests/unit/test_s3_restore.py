"""Tests for nexus_deploy.s3_restore (RFC 0001 PR-2 pipeline-side).

Pipeline-side orchestration tests. ``s3_persistence.py`` is the
pure-rendering module already covered by ``test_s3_persistence.py``;
this file tests the *caller* — env-var parsing, feature-flag
gating, target-list correctness, the combined-script render shape,
and the orchestration outcome classes.

Coverage focus:

* :func:`build_endpoint_from_env` — all five env vars required;
  any missing → ``None``; charset gating inherited from
  :class:`S3Endpoint`.
* :func:`is_enabled` — strict ``"true"`` match, rejects ``"1"`` /
  ``"True"`` / ``"yes"``.
* :func:`standard_targets` — canonical fixture for v1.0 stacks;
  asserts the user/db identifiers match the values in
  ``stacks/{gitea,dify}/docker-compose.yml`` so a future
  POSTGRES_USER rename breaks the test (and gets caught here
  rather than at runtime).
* :func:`render_combined_restore_script` — single combined script
  contains both the rclone config write and the restore body, in
  that order, with ``mode 600`` on the config file.
* :func:`restore_from_s3` — outcome-class dispatch using a fake
  SSHClient.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from nexus_deploy import s3_persistence as _s3
from nexus_deploy.s3_restore import (
    FEATURE_FLAG_ENV,
    S3RestoreApplied,
    S3RestoreSkipped,
    build_endpoint_from_env,
    is_enabled,
    render_combined_restore_script,
    restore_from_s3,
    standard_targets,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _good_env() -> dict[str, str]:
    """Canonical "all five env vars populated" map used by most
    tests. R2-style endpoint per the v1.0 default."""
    return {
        "NEXUS_S3_PERSISTENCE": "true",
        "PERSISTENCE_S3_ENDPOINT": "https://abc123.r2.cloudflarestorage.com",
        "PERSISTENCE_S3_REGION": "auto",
        "PERSISTENCE_S3_BUCKET": "nexus-stefan-hslu",
        "R2_ACCESS_KEY_ID": "AKIA1234",
        "R2_SECRET_ACCESS_KEY": "secret/key+abc=",
    }


# ---------------------------------------------------------------------------
# Feature-flag gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["true"],
)
def test_is_enabled_accepts_lowercase_true_only(value: str) -> None:
    """Strict ``"true"`` match — ensures operators can't get a
    half-enabled state from shell-truthy variants."""
    assert is_enabled({FEATURE_FLAG_ENV: value}) is True


@pytest.mark.parametrize(
    "value",
    ["", "True", "TRUE", "1", "yes", "Y", "false", "off"],
)
def test_is_enabled_rejects_non_canonical_truthy(value: str) -> None:
    """Anything that isn't literal ``"true"`` returns False —
    intentionally strict so a misspelled env var produces a
    clean skip rather than a silent-flip."""
    assert is_enabled({FEATURE_FLAG_ENV: value}) is False


def test_is_enabled_returns_false_when_var_missing() -> None:
    assert is_enabled({}) is False


# ---------------------------------------------------------------------------
# Env-var parsing
# ---------------------------------------------------------------------------


def test_build_endpoint_from_env_happy_path() -> None:
    """All five env vars present → populated :class:`S3Endpoint`."""
    endpoint = build_endpoint_from_env(_good_env())
    assert endpoint is not None
    assert endpoint.bucket == "nexus-stefan-hslu"
    assert endpoint.region == "auto"
    assert endpoint.endpoint == "https://abc123.r2.cloudflarestorage.com"


@pytest.mark.parametrize(
    "missing_var",
    [
        "PERSISTENCE_S3_ENDPOINT",
        "PERSISTENCE_S3_REGION",
        "PERSISTENCE_S3_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ],
)
def test_build_endpoint_from_env_returns_none_on_any_missing(missing_var: str) -> None:
    """Strict all-or-nothing — any missing env var returns ``None``
    so a half-configured stack falls back to the legacy path
    instead of silently picking up only some of the values."""
    env = _good_env()
    del env[missing_var]
    assert build_endpoint_from_env(env) is None


@pytest.mark.parametrize(
    "missing_var",
    [
        "PERSISTENCE_S3_ENDPOINT",
        "PERSISTENCE_S3_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ],
)
def test_build_endpoint_from_env_treats_empty_as_missing(missing_var: str) -> None:
    """Empty-string env var is the same as missing — guards against
    operators accidentally setting ``EXPORT VAR=`` (no value)."""
    env = _good_env()
    env[missing_var] = ""
    assert build_endpoint_from_env(env) is None


def test_build_endpoint_from_env_charset_error_bubbles_up() -> None:
    """If env contains a value that fails the S3Endpoint constructor
    charset gate, the error propagates so the operator sees what
    they misconfigured — we don't quietly swallow it as None."""
    env = _good_env()
    env["PERSISTENCE_S3_BUCKET"] = "Bad Bucket With Spaces"
    with pytest.raises(_s3.S3PersistenceError, match="bucket"):
        build_endpoint_from_env(env)


# ---------------------------------------------------------------------------
# Canonical targets
# ---------------------------------------------------------------------------


def test_standard_targets_returns_canonical_pair() -> None:
    """Smoke + locks the v1.0 fixture. If the
    ``stacks/{gitea,dify}/docker-compose.yml`` files change the
    POSTGRES_USER or POSTGRES_DB values, this test starts failing
    and a future maintainer knows to align the fixture."""
    postgres, rsync = standard_targets()
    pg_by_container = {p.container: p for p in postgres}

    # Gitea — matches stacks/gitea/docker-compose.yml line 67/68
    assert pg_by_container["gitea-db"].database == "gitea"
    assert pg_by_container["gitea-db"].user == "nexus-gitea"
    # Dify — matches stacks/dify/docker-compose.yml line 180/182
    assert pg_by_container["dify-db"].database == "dify"
    assert pg_by_container["dify-db"].user == "nexus-dify"

    rsync_by_name = {r.name: r for r in rsync}
    # All five subdirs the storage-layout section of RFC 0001 lists
    for required in ("gitea-repos", "gitea-lfs", "dify-storage", "dify-weaviate", "dify-plugins"):
        assert required in rsync_by_name, f"missing required rsync target: {required}"

    # db/ and redis/ subdirs deliberately NOT in the list — they're
    # captured via pg_dump / regeneratable. Regression for the
    # "exclude" theme that's appeared in multiple Copilot rounds.
    for name in rsync_by_name:
        assert not name.endswith("-db"), f"db/ subdir wrongly in rsync list: {name}"
        assert "redis" not in name, f"redis/ subdir wrongly in rsync list: {name}"


def test_standard_targets_s3_subpaths_match_rfc_layout() -> None:
    """The S3 subpaths must match RFC 0001's storage-layout
    section: ``gitea/repos``, ``gitea/lfs``, ``dify/storage``,
    ``dify/weaviate``, ``dify/plugins``. Mismatch would put data
    under the wrong prefix and break restore."""
    _, rsync = standard_targets()
    sub_by_name = {r.name: r.s3_subpath for r in rsync}
    assert sub_by_name["gitea-repos"] == "gitea/repos"
    assert sub_by_name["gitea-lfs"] == "gitea/lfs"
    assert sub_by_name["dify-storage"] == "dify/storage"
    assert sub_by_name["dify-weaviate"] == "dify/weaviate"
    assert sub_by_name["dify-plugins"] == "dify/plugins"


# ---------------------------------------------------------------------------
# Combined-script render
# ---------------------------------------------------------------------------


def _endpoint() -> _s3.S3Endpoint:
    return _s3.S3Endpoint(
        endpoint="https://abc123.r2.cloudflarestorage.com",
        region="auto",
        access_key="AKIA1234",
        secret_key="secret123",
        bucket="nexus-test",
    )


def test_combined_script_contains_both_config_and_body() -> None:
    """The combined script must include the rclone config block AND
    the restore body — caller relies on the single-script invariant
    to keep the SSH round-trip atomic."""
    postgres, rsync = standard_targets()
    script = render_combined_restore_script(
        endpoint=_endpoint(),
        postgres_targets=postgres,
        rsync_targets=rsync,
    )
    # rclone profile heading
    assert f"[{_s3.RCLONE_PROFILE}]" in script
    # restore-body landmarks
    assert "snapshots/latest.txt" in script
    assert "pg_restore" in script


def test_combined_script_writes_config_atomically_at_mode_600() -> None:
    """The rclone config file holds the R2 secret access key. Must
    land at mode 600 atomically — no chmod race window where the
    file exists with default permissions."""
    script = render_combined_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(),
        rsync_targets=(),
    )
    assert 'install -m 600 /dev/stdin "$HOME/.config/rclone/rclone.conf"' in script


def test_combined_script_starts_with_shebang_and_safety_pragmas() -> None:
    script = render_combined_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(),
        rsync_targets=(),
    )
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script


def test_combined_script_orders_config_before_body() -> None:
    """Body needs the config in place to find the rclone profile.
    Config write must come first."""
    script = render_combined_restore_script(
        endpoint=_endpoint(),
        postgres_targets=(),
        rsync_targets=(),
    )
    config_pos = script.find("install -m 600")
    body_pos = script.find("looking up latest snapshot")
    assert 0 < config_pos < body_pos


# ---------------------------------------------------------------------------
# restore_from_s3 — outcome dispatch
# ---------------------------------------------------------------------------


def _fake_ssh(stdout: str) -> MagicMock:
    """SSHClient stub whose ``run_script`` returns a
    CompletedProcess-shaped object with the given stdout."""
    ssh = MagicMock()
    ssh.run_script.return_value = subprocess.CompletedProcess(
        args=["ssh", "nexus", "bash", "-s"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )
    return ssh


def test_restore_from_s3_skips_when_feature_flag_off() -> None:
    """No flag → no S3 path. ``ssh.run_script`` must NOT be called
    — that's the whole point of the early skip."""
    ssh = _fake_ssh("")
    result = restore_from_s3(ssh, env={"NEXUS_S3_PERSISTENCE": "false"})
    assert isinstance(result, S3RestoreSkipped)
    assert result.reason == "feature_flag_off"
    ssh.run_script.assert_not_called()


def test_restore_from_s3_skips_when_env_incomplete(capsys: pytest.CaptureFixture[str]) -> None:
    """Flag on but a required env var missing — should warn to
    stderr (operator-actionable) and skip."""
    env = _good_env()
    del env["PERSISTENCE_S3_BUCKET"]
    ssh = _fake_ssh("")
    result = restore_from_s3(ssh, env=env)
    assert isinstance(result, S3RestoreSkipped)
    assert result.reason == "no_endpoint_env"
    ssh.run_script.assert_not_called()
    captured = capsys.readouterr()
    assert "feature flag" in captured.err
    assert "skipping" in captured.err


def test_restore_from_s3_returns_fresh_start_when_bucket_empty() -> None:
    """Bucket has no latest.txt → restore script outputs the
    fresh-start marker → orchestration returns Skipped /
    fresh_start_empty_s3."""
    ssh = _fake_ssh("fresh-start: no snapshot in S3, leaving local state empty\n")
    result = restore_from_s3(ssh, env=_good_env())
    assert isinstance(result, S3RestoreSkipped)
    assert result.reason == "fresh_start_empty_s3"
    ssh.run_script.assert_called_once()


def test_restore_from_s3_returns_applied_with_timestamp() -> None:
    """Successful restore → ``Applied`` with the parsed timestamp."""
    ssh = _fake_ssh(
        "→ restore: looking up latest snapshot\n"
        "→ restore: using snapshot snapshots/20260511T120000Z\n"
        "→ restore: pulling filesystem trees\n"
        "→ restore: applying postgres dumps\n"
        "✓ restore complete from snapshots/20260511T120000Z\n",
    )
    result = restore_from_s3(ssh, env=_good_env())
    assert isinstance(result, S3RestoreApplied)
    assert result.snapshot_timestamp == "20260511T120000Z"


def test_restore_from_s3_returns_applied_unknown_when_log_shape_drifts() -> None:
    """If the server-side log lines change shape (we can't find the
    "using snapshot" marker), we still return Applied — rc=0 means
    the data is in place, just with a less-informative diagnostic."""
    ssh = _fake_ssh("✓ restore complete\n")  # no "using snapshot" line
    result = restore_from_s3(ssh, env=_good_env())
    assert isinstance(result, S3RestoreApplied)
    assert result.snapshot_timestamp == "(unknown)"


def test_restore_from_s3_propagates_called_process_error() -> None:
    """If the remote script exits non-zero, the underlying
    CalledProcessError must propagate — pipeline.py should hard-fail
    rather than let the spinup proceed with half-populated data."""
    ssh = MagicMock()
    ssh.run_script.side_effect = subprocess.CalledProcessError(
        returncode=2,
        cmd=["ssh", "nexus", "bash", "-s"],
        output="✗ restore-failed: latest.txt has invalid timestamp\n",
    )
    with pytest.raises(subprocess.CalledProcessError):
        restore_from_s3(ssh, env=_good_env())
