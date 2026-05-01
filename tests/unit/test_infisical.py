"""Tests for nexus_deploy.infisical — Phase 1 Modul 1.1 (#505).

Covers:
- skip-empty rule (#504 contract: preserve operator UI edits)
- folder list + per-folder key list match deploy.sh source-order
- payload JSON shape (folder + secrets-batch upsert)
- adversarial token quoting in the remote bash loop
- end-to-end bootstrap with mocked ssh/rsync runners
- snapshot of compute_folders output for a fully-populated config
- CLI integration: `infisical bootstrap` reads stdin + env vars
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion

from nexus_deploy.config import NexusConfig
from nexus_deploy.infisical import (
    BootstrapEnv,
    BootstrapResult,
    FolderSpec,
    InfisicalClient,
    _filter_empty,
    compute_folders,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ---------------------------------------------------------------------------
# _filter_empty — skip-empty rule
# ---------------------------------------------------------------------------


def test_filter_empty_drops_none() -> None:
    assert _filter_empty({"K": None, "L": "v"}) == {"L": "v"}


def test_filter_empty_drops_empty_string() -> None:
    assert _filter_empty({"K": "", "L": "v"}) == {"L": "v"}


def test_filter_empty_keeps_whitespace() -> None:
    """A single space is a valid value (some configs use ' ' as a sentinel)."""
    assert _filter_empty({"K": " ", "L": "v"}) == {"K": " ", "L": "v"}


def test_filter_empty_preserves_input_order() -> None:
    items = {"C": "1", "A": "2", "B": "3"}
    assert list(_filter_empty(items)) == ["C", "A", "B"]


# ---------------------------------------------------------------------------
# FolderSpec — payload shapes
# ---------------------------------------------------------------------------


def test_folder_payload_shape() -> None:
    spec = FolderSpec("kestra", {"K": "v"})
    assert spec.folder_payload("proj-1", "dev") == {
        "projectId": "proj-1",
        "environment": "dev",
        "name": "kestra",
        "path": "/",
    }


def test_secrets_payload_shape() -> None:
    spec = FolderSpec("kestra", {"K1": "v1", "K2": "v2"})
    assert spec.secrets_payload("proj-1", "dev") == {
        "projectId": "proj-1",
        "environment": "dev",
        "secretPath": "/kestra",
        "mode": "upsert",
        "secrets": [
            {"secretKey": "K1", "secretValue": "v1"},
            {"secretKey": "K2", "secretValue": "v2"},
        ],
    }


def test_secrets_payload_preserves_secret_order() -> None:
    """Source-order matches deploy.sh's jq filter (sequential `secretKey: $kN`)."""
    spec = FolderSpec("dify", {"DIFY_USERNAME": "u", "DIFY_PASSWORD": "p", "DIFY_DB_PASSWORD": "d"})
    payload = spec.secrets_payload("p", "e")
    secrets = payload["secrets"]
    assert isinstance(secrets, list)
    keys = [s["secretKey"] for s in secrets]
    assert keys == ["DIFY_USERNAME", "DIFY_PASSWORD", "DIFY_DB_PASSWORD"]


# ---------------------------------------------------------------------------
# compute_folders — schema + ordering + conditional gates
# ---------------------------------------------------------------------------


def _make_config(**overrides: str) -> NexusConfig:
    return NexusConfig.from_secrets_json(json.dumps(overrides))


def test_compute_folders_minimal_emits_unconditional_only() -> None:
    """Empty config + minimal env → no R2/Hetzner-S3/External-S3/SSH folders."""
    folders = compute_folders(NexusConfig.from_secrets_json("{}"), BootstrapEnv())
    names = [f.name for f in folders]
    # Conditional folders absent
    assert "r2-datalake" not in names
    assert "hetzner-s3" not in names
    assert "external-s3" not in names
    assert "ssh" not in names
    # Unconditional core present
    for required in ("config", "infisical", "kestra", "gitea", "woodpecker"):
        assert required in names


def test_compute_folders_r2_gate() -> None:
    """All four r2_* fields must be present for the r2-datalake folder."""
    config = _make_config(
        r2_data_endpoint="ep",
        r2_data_access_key="ak",
        r2_data_secret_key="sk",
        # missing r2_data_bucket
    )
    folders = compute_folders(config, BootstrapEnv())
    assert "r2-datalake" not in [f.name for f in folders]

    config = _make_config(
        r2_data_endpoint="ep",
        r2_data_access_key="ak",
        r2_data_secret_key="sk",
        r2_data_bucket="bk",
    )
    folders = compute_folders(config, BootstrapEnv())
    r2 = next(f for f in folders if f.name == "r2-datalake")
    assert r2.secrets == {
        "R2_ENDPOINT": "ep",
        "R2_ACCESS_KEY": "ak",
        "R2_SECRET_KEY": "sk",
        "R2_BUCKET": "bk",
    }


def test_compute_folders_hetzner_default_bucket_chain() -> None:
    """HETZNER_S3_BUCKET prefers _general, falls back to _lakefs."""
    base = {
        "hetzner_s3_server": "s3.example",
        "hetzner_s3_access_key": "ak",
        "hetzner_s3_secret_key": "sk",
    }
    folders = compute_folders(_make_config(**base, hetzner_s3_bucket_general="g"), BootstrapEnv())
    h = next(f for f in folders if f.name == "hetzner-s3")
    assert h.secrets["HETZNER_S3_BUCKET"] == "g"

    folders = compute_folders(
        _make_config(**base, hetzner_s3_bucket_lakefs="l"),
        BootstrapEnv(),
    )
    h = next(f for f in folders if f.name == "hetzner-s3")
    assert h.secrets["HETZNER_S3_BUCKET"] == "l"

    folders = compute_folders(
        _make_config(**base, hetzner_s3_bucket_general="g", hetzner_s3_bucket_lakefs="l"),
        BootstrapEnv(),
    )
    h = next(f for f in folders if f.name == "hetzner-s3")
    assert h.secrets["HETZNER_S3_BUCKET"] == "g"


def test_compute_folders_skip_empty_drops_optional_keys() -> None:
    """A folder builder skips per-key None/empty values (preserves UI edits)."""
    folders = compute_folders(NexusConfig.from_secrets_json("{}"), BootstrapEnv(domain="x.test"))
    config_folder = next(f for f in folders if f.name == "config")
    assert config_folder.secrets == {"DOMAIN": "x.test", "ADMIN_USERNAME": "admin"}
    # ADMIN_EMAIL absent → not in payload


def test_compute_folders_woodpecker_oauth_optional() -> None:
    folders = compute_folders(
        _make_config(woodpecker_agent_secret="s"),
        BootstrapEnv(),
    )
    w = next(f for f in folders if f.name == "woodpecker")
    assert w.secrets == {"WOODPECKER_AGENT_SECRET": "s"}

    folders = compute_folders(
        _make_config(woodpecker_agent_secret="s"),
        BootstrapEnv(woodpecker_gitea_client="cid", woodpecker_gitea_secret="csec"),
    )
    w = next(f for f in folders if f.name == "woodpecker")
    assert w.secrets == {
        "WOODPECKER_AGENT_SECRET": "s",
        "WOODPECKER_GITEA_CLIENT": "cid",
        "WOODPECKER_GITEA_SECRET": "csec",
    }


def test_compute_folders_ssh_optional() -> None:
    folders = compute_folders(NexusConfig.from_secrets_json("{}"), BootstrapEnv())
    assert "ssh" not in [f.name for f in folders]

    folders = compute_folders(
        NexusConfig.from_secrets_json("{}"),
        BootstrapEnv(ssh_private_key_base64="b64-key"),
    )
    ssh = next(f for f in folders if f.name == "ssh")
    assert ssh.secrets == {"SSH_PRIVATE_KEY_BASE64": "b64-key"}


def test_compute_folders_gitea_repo_url_falls_back_to_default_repo_name() -> None:
    """`${REPO_NAME:-nexus-${DOMAIN//./-}-gitea}` mirror."""
    config = _make_config(admin_username="bob")
    folders = compute_folders(config, BootstrapEnv(domain="ex.example.com"))
    gitea = next(f for f in folders if f.name == "gitea")
    assert (
        gitea.secrets["GITEA_REPO_URL"]
        == "https://git.ex.example.com/bob/nexus-ex-example-com-gitea.git"
    )


def test_compute_folders_full_snapshot(snapshot: SnapshotAssertion) -> None:
    """Lock the entire folder list + ordering + per-folder keys.

    Uses the ``secrets_full.json`` fixture (88 fields populated) so any
    accidental reordering or skipped key surfaces as a snapshot diff.
    """
    raw = (FIXTURES / "secrets_full.json").read_text()
    config = NexusConfig.from_secrets_json(raw)
    env = BootstrapEnv(
        domain="snapshot.test",
        admin_email="admin@snapshot.test",
        gitea_user_email="user@snapshot.test",
        gitea_user_username="snapshot-user",
        gitea_repo_owner="snapshot-org",
        repo_name="snapshot-repo",
        om_principal_domain="snapshot.test",
        woodpecker_gitea_client="cid",
        woodpecker_gitea_secret="csec",
        ssh_private_key_base64="snapshot-ssh-base64",
    )
    folders = compute_folders(config, env)
    assert {f.name: f.secrets for f in folders} == snapshot


# ---------------------------------------------------------------------------
# InfisicalClient — payload encoding + remote-loop bash
# ---------------------------------------------------------------------------


def test_encode_payloads_round_trip() -> None:
    client = InfisicalClient("p", "dev", "tok")
    folders = [FolderSpec("kestra", {"K": "v"})]
    encoded = client.encode_payloads(folders)
    f_payload = json.loads(encoded["f-kestra.json"])
    s_payload = json.loads(encoded["s-kestra.json"])
    assert f_payload == folders[0].folder_payload("p", "dev")
    assert s_payload == folders[0].secrets_payload("p", "dev")


def test_encode_payloads_compact() -> None:
    """No whitespace between JSON tokens — matches `json.dumps(..., separators=(',',':'))`."""
    client = InfisicalClient("p", "dev", "tok")
    encoded = client.encode_payloads([FolderSpec("k", {"X": "1"})])
    assert " " not in encoded["s-k.json"]


def test_remote_loop_quotes_token_safely(tmp_path: Path) -> None:
    """Adversarial token can't break out of the bash structure.

    Eval-extracts only the ``TOKEN=`` assignment from the generated
    loop and verifies the resolved bash variable equals the original
    payload — confirming that the quoting in :meth:`_build_remote_loop`
    survives an attempt to use ``';rm -rf /;echo '`` to escape and
    inject commands. Side-channel canary in tmp_path catches any
    accidental execution of the injection payload.
    """
    canary_dir = tmp_path / "canary"
    canary_dir.mkdir()
    canary = canary_dir / "INJECTED"
    nasty = f"tok';touch {shlex.quote(str(canary))};echo '"
    client = InfisicalClient("p", "dev", nasty)
    loop = client._build_remote_loop()
    # Extract just the TOKEN= line. Eval-running the full loop would
    # reach the curl + rm -rf in the loop body; isolating the assignment
    # is enough to prove the quoting holds. We also force the
    # token-fallback file to a path that doesn't exist so the OR-fallback
    # branch fires and we exercise the shlex.quote'd token literal.
    token_line = next(line for line in loop.splitlines() if line.startswith("TOKEN=$(cat"))
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f'{token_line.replace("/opt/docker-server/.infisical-token", "/nonexistent")}\nprintf "%s" "$TOKEN"',
        ],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert completed.stdout == nasty
    assert not canary.exists(), "shlex.quote breach: injection payload executed"


# ---------------------------------------------------------------------------
# bootstrap() — end-to-end with mocked ssh/rsync
# ---------------------------------------------------------------------------


def _ok_ssh(stdout: str = "5:0") -> Any:
    def runner(_cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr="")

    return runner


def _ok_rsync() -> Any:
    def runner(_local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")

    return runner


def test_bootstrap_removes_local_payloads_on_success(tmp_path: Path) -> None:
    """After a successful bootstrap, the local f-/s-*.json files are gone.

    Mirrors deploy.sh's ``rm -rf "$PUSH_DIR"`` cleanup. Files contain
    secret values; leaving them on the runner is a secrets-at-rest
    leak.
    """
    push_dir = tmp_path / "push"
    client = InfisicalClient("p", "dev", "tok", push_dir=push_dir)
    folders = [FolderSpec("kestra", {"K": "v"})]
    client.bootstrap(folders, ssh_runner=_ok_ssh(), rsync_runner=_ok_rsync())
    assert list(push_dir.glob("[fs]-*.json")) == []


def test_bootstrap_removes_local_payloads_on_failure(tmp_path: Path) -> None:
    """Cleanup runs in `finally` — even when ssh raises, payloads are gone."""
    push_dir = tmp_path / "push"
    client = InfisicalClient("p", "dev", "tok", push_dir=push_dir)

    def failing_ssh(_cmd: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["ssh"])

    folders = [FolderSpec("kestra", {"K": "v"})]
    with pytest.raises(subprocess.CalledProcessError):
        client.bootstrap(folders, ssh_runner=failing_ssh, rsync_runner=_ok_rsync())
    assert list(push_dir.glob("[fs]-*.json")) == []


def test_bootstrap_cleanup_preserves_unrelated_files(tmp_path: Path) -> None:
    """Only f-*.json + s-*.json get removed; unrelated files stay."""
    push_dir = tmp_path / "push"
    push_dir.mkdir()
    (push_dir / "operator-notes.txt").write_text("keep me")
    client = InfisicalClient("p", "dev", "tok", push_dir=push_dir)
    client.bootstrap(
        [FolderSpec("kestra", {"K": "v"})],
        ssh_runner=_ok_ssh(),
        rsync_runner=_ok_rsync(),
    )
    assert (push_dir / "operator-notes.txt").exists()


def test_remote_loop_uses_printf_not_echo_for_token() -> None:
    """`echo` would mangle tokens starting with `-n`/`-e`/`-E`. printf doesn't."""
    client = InfisicalClient("p", "dev", "tok-value")
    loop = client._build_remote_loop()
    # Token comes via printf, never via echo
    assert "printf '%s' " in loop
    # Specifically, the fallback line uses printf
    fallback_line = next(line for line in loop.splitlines() if "TOKEN=$(cat" in line)
    assert "echo " not in fallback_line


def test_bootstrap_writes_payloads_before_rsync(tmp_path: Path) -> None:
    """bootstrap() materialises both f-NAME.json and s-NAME.json per folder.

    Files are deleted in the finally block (secrets-at-rest cleanup),
    so this test inspects the push_dir state INSIDE the mocked rsync
    callback — the moment rsync would see them on a real run.
    """
    push_dir = tmp_path / "push"
    client = InfisicalClient("p", "dev", "tok", push_dir=push_dir)
    folders = [FolderSpec("kestra", {"K": "v"}), FolderSpec("postgres", {"P": "1"})]

    seen: dict[str, list[str]] = {"files": []}

    def inspect_rsync(local: Path, _remote: str) -> subprocess.CompletedProcess[str]:
        seen["files"] = sorted(p.name for p in local.glob("*.json"))
        return subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")

    client.bootstrap(folders, ssh_runner=_ok_ssh(), rsync_runner=inspect_rsync)
    assert seen["files"] == [
        "f-kestra.json",
        "f-postgres.json",
        "s-kestra.json",
        "s-postgres.json",
    ]


def test_bootstrap_clears_stale_payloads(tmp_path: Path) -> None:
    """Pre-existing f-/s- files from a prior run are removed before write."""
    push_dir = tmp_path / "push"
    push_dir.mkdir()
    (push_dir / "f-stale.json").write_text("stale")
    (push_dir / "s-stale.json").write_text("stale")
    (push_dir / "unrelated.txt").write_text("keep me")
    client = InfisicalClient("p", "dev", "tok", push_dir=push_dir)
    client.bootstrap(
        [FolderSpec("new", {"K": "v"})],
        ssh_runner=_ok_ssh(),
        rsync_runner=_ok_rsync(),
    )
    assert not (push_dir / "f-stale.json").exists()
    assert not (push_dir / "s-stale.json").exists()
    # Non-payload files are NOT touched
    assert (push_dir / "unrelated.txt").exists()


def test_bootstrap_parses_ok_fail_counts(tmp_path: Path) -> None:
    client = InfisicalClient("p", "dev", "tok", push_dir=tmp_path / "p")
    result = client.bootstrap(
        [FolderSpec("k", {"X": "v"})],
        ssh_runner=_ok_ssh("3:1"),
        rsync_runner=_ok_rsync(),
    )
    assert result == BootstrapResult(folders_built=1, pushed=3, failed=1)


def test_bootstrap_takes_last_line_of_stdout(tmp_path: Path) -> None:
    """deploy.sh's baseline-capture WARN message can precede the OK:FAIL line."""
    client = InfisicalClient("p", "dev", "tok", push_dir=tmp_path / "p")
    result = client.bootstrap(
        [FolderSpec("k", {"X": "v"})],
        ssh_runner=_ok_ssh("WARN: capture failed\n7:2"),
        rsync_runner=_ok_rsync(),
    )
    assert result.pushed == 7
    assert result.failed == 2


def test_bootstrap_unparseable_output_yields_failure(tmp_path: Path) -> None:
    client = InfisicalClient("p", "dev", "tok", push_dir=tmp_path / "p")
    folders = [FolderSpec("k", {"X": "v"}), FolderSpec("p", {"Y": "v"})]
    result = client.bootstrap(
        folders, ssh_runner=_ok_ssh("garbage output"), rsync_runner=_ok_rsync()
    )
    assert result == BootstrapResult(folders_built=2, pushed=0, failed=2)


def test_bootstrap_invokes_rsync_with_push_dir(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_rsync(local: Path, remote: str) -> subprocess.CompletedProcess[str]:
        captured["local"] = local
        captured["remote"] = remote
        return subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")

    push_dir = tmp_path / "push"
    client = InfisicalClient("p", "dev", "tok", push_dir=push_dir)
    client.bootstrap([FolderSpec("k", {"X": "v"})], ssh_runner=_ok_ssh(), rsync_runner=fake_rsync)
    assert captured["local"] == push_dir
    assert captured["remote"] == "nexus:/tmp/infisical-push/"


def test_bootstrap_runs_ssh_loop_with_token(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_ssh(cmd: str) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="0:0", stderr="")

    client = InfisicalClient("p", "dev", "real-token", push_dir=tmp_path / "p")
    client.bootstrap([FolderSpec("k", {"X": "v"})], ssh_runner=fake_ssh, rsync_runner=_ok_rsync())
    cmd = captured["cmd"]
    assert "real-token" in cmd
    assert "/api/v2/folders" in cmd
    assert "/api/v4/secrets/batch" in cmd
    assert 'mode: "upsert"' not in cmd  # the JSON is sent via @file, not inlined
    # Token-fallback file logic preserved
    assert "/opt/docker-server/.infisical-token" in cmd


# ---------------------------------------------------------------------------
# CLI: `nexus-deploy infisical bootstrap`
# ---------------------------------------------------------------------------


def test_cli_infisical_bootstrap_requires_project_id_and_token(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "infisical", "bootstrap"])
    monkeypatch.setattr(sys, "stdin", _StubStdin("{}"))
    # Strip both required vars
    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_TOKEN", raising=False)
    rc = main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "PROJECT_ID and INFISICAL_TOKEN" in captured.err


def test_cli_infisical_bootstrap_unexpected_arg_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "infisical", "bootstrap", "--bogus"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2
    assert "unexpected arg" in captured.err


def test_cli_infisical_bootstrap_invalid_json_exits_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.__main__ import main

    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "infisical", "bootstrap"])
    monkeypatch.setattr(sys, "stdin", _StubStdin("not-json"))
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    rc = main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "not valid JSON" in captured.err


def test_cli_infisical_bootstrap_happy_path(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end CLI exercise with mocked SSH/rsync."""
    from nexus_deploy.__main__ import main

    push_dir = tmp_path / "push"

    def fake_ssh(_cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="3:0", stderr="")

    def fake_rsync(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.ssh_run", fake_ssh)
    monkeypatch.setattr("nexus_deploy._remote.rsync_to_remote", fake_rsync)
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "infisical", "bootstrap"])
    monkeypatch.setattr(sys, "stdin", _StubStdin('{"admin_username": "u"}'))
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    monkeypatch.setenv("DOMAIN", "ex.test")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@ex.test")
    monkeypatch.setenv("PUSH_DIR", str(push_dir))
    rc = main()
    captured = capsys.readouterr()
    assert rc == 0
    assert "pushed=3" in captured.out
    assert "failed=0" in captured.out


def test_cli_infisical_bootstrap_failed_count_returns_1(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Any failed folder push → exit 1 (deploy.sh's `if !` wrap then warns, doesn't abort)."""
    from nexus_deploy.__main__ import main

    def fake_ssh(_cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="2:1", stderr="")

    def fake_rsync(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nexus_deploy._remote.ssh_run", fake_ssh)
    monkeypatch.setattr("nexus_deploy._remote.rsync_to_remote", fake_rsync)
    monkeypatch.setattr(sys, "argv", ["nexus-deploy", "infisical", "bootstrap"])
    monkeypatch.setattr(sys, "stdin", _StubStdin("{}"))
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    monkeypatch.setenv("PUSH_DIR", str(tmp_path / "push"))
    rc = main()
    _ = capsys.readouterr()
    assert rc == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubStdin:
    """Tiny stand-in for ``sys.stdin`` that returns a fixed string."""

    def __init__(self, content: str) -> None:
        self._content = content

    def read(self) -> str:
        return self._content
