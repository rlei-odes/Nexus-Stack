"""Tests for nexus_deploy.pipeline — Phase 4c (#505).

End-to-end mocked pipeline runs. The 3 new modules + the orchestrator
are DI'd via monkeypatch + the public ``tofu_runner`` /
``docker_hub_login`` seams. Per-phase invariants are R-tagged.

Coverage targets:
- R-tofu-state-fail: missing tofu state aborts BEFORE any output read.
- R-secrets-empty: empty secrets JSON aborts.
- R-r2-creds-injected: when ``.r2-credentials`` exists, the
  AWS_* env vars are populated BEFORE tofu calls.
- R-r2-creds-missing: missing ``.r2-credentials`` is a legitimate
  skip; pipeline continues.
- R-domain-required: empty ``domain`` in tfvars aborts.
- R-collision-fallback: admin == user_email triggers
  ``gitea-admin@<domain>`` (smoke through the pipeline).
- R-orchestrator-hard-fail: hard failure → PipelineError.
- R-banner-renders: format_done_banner produces a stable shape.
- R-options-defaults: missing PipelineOptions fields default
  cleanly.
- R-rc-mapping: CLI handler maps 0/1/2 from the result + exception.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus_deploy.orchestrator import OrchestratorResult, OrchestratorState, PhaseResult
from nexus_deploy.pipeline import (
    PipelineError,
    PipelineOptions,
    PipelineResult,
    format_done_banner,
    run_pipeline,
)
from nexus_deploy.tofu import TofuRunner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tofu_dir(tmp_path: Path) -> Path:
    """Create a tofu/stack/config.tfvars + .r2-credentials skeleton."""
    tofu_root = tmp_path / "tofu"
    stack = tofu_root / "stack"
    stack.mkdir(parents=True)
    (stack / "config.tfvars").write_text(
        'domain = "example.com"\n'
        'admin_email = "admin@example.com"\n'
        'user_email = "user@example.com"\n',
        encoding="utf-8",
    )
    (tofu_root / ".r2-credentials").write_text(
        'R2_ACCESS_KEY_ID="ABC"\nR2_SECRET_ACCESS_KEY="DEF"\n',
        encoding="utf-8",
    )
    return stack


@pytest.fixture
def project_root(tofu_dir: Path) -> Path:
    """tofu/stack's parent's parent — i.e., where tofu/ lives."""
    return tofu_dir.parent.parent


@pytest.fixture
def fake_secrets_payload() -> dict[str, str]:
    """Minimum SECRETS_JSON shape that NexusConfig.from_secrets_json
    accepts. NexusConfig is permissive — unknown keys are ignored."""
    return {
        "ADMIN_USERNAME": "admin",
        "GITEA_ADMIN_PASS": "g-admin-pw",
        "INFISICAL_PASS": "inf-admin-pw",
        "WOODPECKER_AGENT_SECRET": "wp-secret",
    }


@pytest.fixture
def fake_tofu_runner(fake_secrets_payload: dict[str, str]) -> MagicMock:
    """A TofuRunner stand-in. ``state_list_ok`` returns True; outputs
    are configurable via ``output_json_map`` /
    ``output_raw_map`` set via setattr after construction."""
    runner = MagicMock(spec=TofuRunner)
    runner.tofu_dir = Path("/fake")
    runner.state_list_ok.return_value = True
    json_map: dict[str, Any] = {
        "secrets": fake_secrets_payload,
        "image_versions": {"kestra": "v0.51"},
        "enabled_services": ["kestra", "jupyter"],
        "firewall_rules": {},
        "ssh_service_token": {"client_id": "cf-id", "client_secret": "cf-secret"},
        "service_urls": {"kestra": "https://kestra.example.com"},
    }
    raw_map: dict[str, str] = {
        "server_ip": "1.2.3.4",
        "persistent_volume_id": "1234",
    }
    runner.output_json.side_effect = lambda name, default=None: json_map.get(name, default)
    runner.output_raw.side_effect = lambda name, default="": raw_map.get(name, default)
    return runner


@pytest.fixture
def setup_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install no-op mocks for every external boundary the pipeline
    crosses. Tests that need to assert specific behavior re-set the
    mock they care about."""
    from nexus_deploy.setup import SSHReadinessResult, VolumeMountResult

    mocks: dict[str, Any] = {
        "configure_ssh": MagicMock(return_value=None),
        "wait_for_ssh": MagicMock(return_value=SSHReadinessResult(succeeded=True, attempts=1)),
        "ensure_jq": MagicMock(return_value=False),
        "mount_persistent_volume": MagicMock(
            return_value=VolumeMountResult(mounted=True, fstab_added=True, detail="mounted"),
        ),
        "setup_wetty_ssh_agent": MagicMock(return_value=None),
        "ssh_keygen_cleanup": MagicMock(),
        "SSHClient": MagicMock(),
    }
    monkeypatch.setattr("nexus_deploy.pipeline._setup.configure_ssh", mocks["configure_ssh"])
    monkeypatch.setattr("nexus_deploy.pipeline._setup.wait_for_ssh", mocks["wait_for_ssh"])
    monkeypatch.setattr("nexus_deploy.pipeline._setup.ensure_jq", mocks["ensure_jq"])
    monkeypatch.setattr(
        "nexus_deploy.pipeline._setup.mount_persistent_volume",
        mocks["mount_persistent_volume"],
    )
    monkeypatch.setattr(
        "nexus_deploy.pipeline._setup.setup_wetty_ssh_agent",
        mocks["setup_wetty_ssh_agent"],
    )
    monkeypatch.setattr("nexus_deploy.pipeline._ssh_keygen_cleanup", mocks["ssh_keygen_cleanup"])
    # SSHClient is used as a context manager in the pipeline; the mock
    # just returns itself for both __enter__ / __exit__.
    ssh_instance = MagicMock()
    mocks["SSHClient"].return_value.__enter__.return_value = ssh_instance
    mocks["SSHClient"].return_value.__exit__.return_value = False
    monkeypatch.setattr("nexus_deploy.pipeline.SSHClient", mocks["SSHClient"])
    mocks["ssh_instance"] = ssh_instance
    return mocks


@pytest.fixture
def mock_orchestrator(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Make Orchestrator() return a MagicMock whose
    run_pre_bootstrap + run_all return clean OrchestratorResults."""
    instance = MagicMock()
    instance.run_pre_bootstrap.return_value = OrchestratorResult(
        phases=(PhaseResult(name="pre", status="ok"),),
        state=OrchestratorState(),
    )
    instance.run_all.return_value = OrchestratorResult(
        phases=(PhaseResult(name="post", status="ok"),),
        state=OrchestratorState(),
    )
    cls_mock = MagicMock(return_value=instance)
    monkeypatch.setattr("nexus_deploy.pipeline.Orchestrator", cls_mock)
    return instance


# ---------------------------------------------------------------------------
# R-tofu-state-fail / R-secrets-empty
# ---------------------------------------------------------------------------


def test_pipeline_aborts_when_tofu_state_uninitialized(
    project_root: Path, fake_tofu_runner: MagicMock, setup_mocks: dict[str, Any]
) -> None:
    """R-tofu-state-fail: state_list_ok=False → PipelineError BEFORE
    any output_json call."""
    fake_tofu_runner.state_list_ok.return_value = False
    with pytest.raises(PipelineError, match=r"state .* not initialised"):
        run_pipeline(
            project_root=project_root,
            options=PipelineOptions(),
            tofu_runner=fake_tofu_runner,
        )
    fake_tofu_runner.output_json.assert_not_called()


def test_pipeline_aborts_on_empty_secrets(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
) -> None:
    """R-secrets-empty: secrets={} → PipelineError. Without secrets
    the orchestrator can't run."""
    fake_tofu_runner.output_json.side_effect = lambda name, default=None: (
        {} if name == "secrets" else default
    )
    with pytest.raises(PipelineError, match=r"secrets .* empty"):
        run_pipeline(
            project_root=project_root,
            options=PipelineOptions(),
            tofu_runner=fake_tofu_runner,
        )


# ---------------------------------------------------------------------------
# R-r2-creds-injected / R-r2-creds-missing
# ---------------------------------------------------------------------------


def test_pipeline_injects_r2_creds_into_environ(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-r2-creds-injected: when .r2-credentials exists with both
    keys, AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY land in
    os.environ BEFORE state_list_ok runs."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    captured_env: dict[str, str | None] = {}

    def _state_list_ok_capture() -> bool:
        captured_env["AWS_ACCESS_KEY_ID"] = os.environ.get("AWS_ACCESS_KEY_ID")
        captured_env["AWS_SECRET_ACCESS_KEY"] = os.environ.get("AWS_SECRET_ACCESS_KEY")
        return True

    fake_tofu_runner.state_list_ok.side_effect = _state_list_ok_capture
    run_pipeline(
        project_root=project_root,
        options=PipelineOptions(),
        tofu_runner=fake_tofu_runner,
    )
    assert captured_env["AWS_ACCESS_KEY_ID"] == "ABC"
    assert captured_env["AWS_SECRET_ACCESS_KEY"] == "DEF"


def test_pipeline_skips_creds_when_file_missing(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-r2-creds-missing: no .r2-credentials → pipeline continues
    without injecting AWS_*. Operator's pre-existing env (from CI
    secrets) survives."""
    (project_root / "tofu" / ".r2-credentials").unlink()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "preset-by-ci")
    run_pipeline(
        project_root=project_root,
        options=PipelineOptions(),
        tofu_runner=fake_tofu_runner,
    )
    # CI's pre-set value survives — we didn't overwrite it.
    assert os.environ["AWS_ACCESS_KEY_ID"] == "preset-by-ci"


# ---------------------------------------------------------------------------
# R-domain-required
# ---------------------------------------------------------------------------


def test_pipeline_aborts_on_empty_domain(
    project_root: Path, fake_tofu_runner: MagicMock, setup_mocks: dict[str, Any]
) -> None:
    """R-domain-required: tfvars with no domain → PipelineError."""
    (project_root / "tofu" / "stack" / "config.tfvars").write_text(
        'admin_email = "admin@example.com"\n', encoding="utf-8"
    )
    with pytest.raises(PipelineError, match="missing a non-empty 'domain'"):
        run_pipeline(
            project_root=project_root,
            options=PipelineOptions(),
            tofu_runner=fake_tofu_runner,
        )


# ---------------------------------------------------------------------------
# R-orchestrator-hard-fail
# ---------------------------------------------------------------------------


def test_pipeline_aborts_when_pre_bootstrap_hard_fails(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
) -> None:
    """R-orchestrator-hard-fail (pre-bootstrap): any phase
    status='failed' raises PipelineError."""
    mock_orchestrator.run_pre_bootstrap.return_value = OrchestratorResult(
        phases=(PhaseResult(name="pre", status="failed", detail="boom"),),
        state=OrchestratorState(),
    )
    with pytest.raises(PipelineError, match="pre-bootstrap pipeline aborted"):
        run_pipeline(
            project_root=project_root,
            options=PipelineOptions(),
            tofu_runner=fake_tofu_runner,
        )
    # run_all must NOT have been called — pre-bootstrap aborted.
    mock_orchestrator.run_all.assert_not_called()


def test_pipeline_aborts_when_run_all_hard_fails(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
) -> None:
    """R-orchestrator-hard-fail (run-all): post-bootstrap hard fail
    after pre-bootstrap succeeded → PipelineError."""
    mock_orchestrator.run_all.return_value = OrchestratorResult(
        phases=(PhaseResult(name="post", status="failed", detail="boom"),),
        state=OrchestratorState(),
    )
    with pytest.raises(PipelineError, match="post-bootstrap pipeline aborted"):
        run_pipeline(
            project_root=project_root,
            options=PipelineOptions(),
            tofu_runner=fake_tofu_runner,
        )


# ---------------------------------------------------------------------------
# R-banner-renders
# ---------------------------------------------------------------------------


def test_format_done_banner_contains_service_urls() -> None:
    """R-banner-renders: service URLs are formatted as 'name: url'."""
    result = PipelineResult(
        pre_bootstrap=OrchestratorResult(phases=(), state=OrchestratorState()),
        run_all=OrchestratorResult(phases=(), state=OrchestratorState()),
        service_urls={
            "kestra": "https://kestra.example.com",
            "jupyter": "https://jupyter.example.com",
        },
    )
    banner = format_done_banner(result)
    assert "✅ Deployment Complete" in banner
    assert "kestra: https://kestra.example.com" in banner
    assert "jupyter: https://jupyter.example.com" in banner
    assert "ssh nexus" in banner
    assert "Credentials available in Infisical" in banner


def test_format_done_banner_handles_empty_service_urls() -> None:
    """When tofu didn't return any URLs, the banner notes that
    instead of being empty."""
    result = PipelineResult(
        pre_bootstrap=OrchestratorResult(phases=(), state=OrchestratorState()),
        run_all=OrchestratorResult(phases=(), state=OrchestratorState()),
    )
    banner = format_done_banner(result)
    assert "service URLs not available" in banner


# ---------------------------------------------------------------------------
# R-wetty-conditional + R-dockerhub-conditional
# ---------------------------------------------------------------------------


def test_pipeline_skips_wetty_when_not_enabled(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
) -> None:
    """When 'wetty' isn't in enabled_services, setup_wetty_ssh_agent
    is NOT called."""
    fake_tofu_runner.output_json.side_effect = lambda name, default=None: {
        "secrets": {"ADMIN_USERNAME": "admin"},
        "image_versions": {},
        "enabled_services": ["kestra"],  # no wetty
        "firewall_rules": {},
        "ssh_service_token": {"client_id": "x", "client_secret": "y"},
        "service_urls": {},
    }.get(name, default)
    run_pipeline(
        project_root=project_root,
        options=PipelineOptions(),
        tofu_runner=fake_tofu_runner,
    )
    setup_mocks["setup_wetty_ssh_agent"].assert_not_called()


def test_pipeline_runs_wetty_when_enabled(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
) -> None:
    fake_tofu_runner.output_json.side_effect = lambda name, default=None: {
        "secrets": {"ADMIN_USERNAME": "admin"},
        "image_versions": {},
        "enabled_services": ["wetty"],
        "firewall_rules": {},
        "ssh_service_token": {"client_id": "x", "client_secret": "y"},
        "service_urls": {},
    }.get(name, default)
    run_pipeline(
        project_root=project_root,
        options=PipelineOptions(),
        tofu_runner=fake_tofu_runner,
    )
    setup_mocks["setup_wetty_ssh_agent"].assert_called_once()


def test_pipeline_skips_dockerhub_login_without_creds(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
) -> None:
    """No DOCKERHUB_USER/TOKEN → docker_hub_login is not invoked."""
    spy = MagicMock()
    run_pipeline(
        project_root=project_root,
        options=PipelineOptions(),  # no creds
        tofu_runner=fake_tofu_runner,
        docker_hub_login=spy,
    )
    spy.assert_not_called()


def test_pipeline_runs_dockerhub_login_when_creds_set(
    project_root: Path,
    fake_tofu_runner: MagicMock,
    setup_mocks: dict[str, Any],
    mock_orchestrator: MagicMock,
) -> None:
    spy = MagicMock()
    run_pipeline(
        project_root=project_root,
        options=PipelineOptions(dockerhub_user="alice", dockerhub_token="ghp_x"),
        tofu_runner=fake_tofu_runner,
        docker_hub_login=spy,
    )
    spy.assert_called_once_with("nexus", "alice", "ghp_x")


# ---------------------------------------------------------------------------
# R-options-defaults
# ---------------------------------------------------------------------------


def test_pipeline_options_defaults() -> None:
    options = PipelineOptions()
    assert options.ssh_private_key_content is None
    assert options.gh_mirror_token is None
    assert options.gh_mirror_repos is None
    assert options.dockerhub_user is None
    assert options.dockerhub_token is None
    assert options.infisical_env == "dev"


def test_pipeline_options_frozen() -> None:
    from dataclasses import FrozenInstanceError

    options = PipelineOptions()
    with pytest.raises(FrozenInstanceError):
        options.infisical_env = "prod"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# R-rc-mapping (CLI handler)
# ---------------------------------------------------------------------------


def test_cli_run_pipeline_unknown_arg_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    from nexus_deploy.__main__ import _run_pipeline

    rc = _run_pipeline(["--bogus"])
    assert rc == 2
    assert "unknown args" in capsys.readouterr().err


def test_cli_run_pipeline_returns_2_on_pipeline_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PipelineError → rc=2 with the error message in stderr."""
    from nexus_deploy.__main__ import _run_pipeline

    def _raise(*_a: Any, **_kw: Any) -> Any:
        raise PipelineError("synthetic boom")

    monkeypatch.setattr("nexus_deploy.__main__._pipeline.run_pipeline", _raise)
    rc = _run_pipeline([])
    assert rc == 2
    assert "synthetic boom" in capsys.readouterr().err


def test_cli_run_pipeline_returns_2_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _run_pipeline

    def _raise(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("synthetic")

    monkeypatch.setattr("nexus_deploy.__main__._pipeline.run_pipeline", _raise)
    rc = _run_pipeline([])
    assert rc == 2
    assert "unexpected error (RuntimeError)" in capsys.readouterr().err


def test_cli_run_pipeline_returns_0_on_clean_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _run_pipeline

    fake = PipelineResult(
        pre_bootstrap=OrchestratorResult(
            phases=(PhaseResult(name="pre", status="ok"),),
            state=OrchestratorState(),
        ),
        run_all=OrchestratorResult(
            phases=(PhaseResult(name="post", status="ok"),),
            state=OrchestratorState(),
        ),
    )
    monkeypatch.setattr("nexus_deploy.__main__._pipeline.run_pipeline", lambda **_: fake)
    rc = _run_pipeline([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Deployment Complete" in out


def test_cli_run_pipeline_returns_1_on_partial(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _run_pipeline

    fake = PipelineResult(
        pre_bootstrap=OrchestratorResult(
            phases=(PhaseResult(name="pre", status="partial"),),
            state=OrchestratorState(),
        ),
        run_all=OrchestratorResult(
            phases=(PhaseResult(name="post", status="ok"),),
            state=OrchestratorState(),
        ),
    )
    monkeypatch.setattr("nexus_deploy.__main__._pipeline.run_pipeline", lambda **_: fake)
    assert _run_pipeline([]) == 1


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants
# ---------------------------------------------------------------------------


def test_pipeline_result_default_service_urls_empty() -> None:
    result = PipelineResult(
        pre_bootstrap=OrchestratorResult(phases=(), state=OrchestratorState()),
        run_all=OrchestratorResult(phases=(), state=OrchestratorState()),
    )
    assert result.service_urls == {}


def test_pipeline_result_frozen() -> None:
    from dataclasses import FrozenInstanceError

    result = PipelineResult(
        pre_bootstrap=OrchestratorResult(phases=(), state=OrchestratorState()),
        run_all=OrchestratorResult(phases=(), state=OrchestratorState()),
    )
    with pytest.raises(FrozenInstanceError):
        result.service_urls = {"foo": "bar"}  # type: ignore[misc]
