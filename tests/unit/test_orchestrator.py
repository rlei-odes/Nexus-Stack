"""Tests for nexus_deploy.orchestrator — Phase 3 Modul 3.4b (#505).

Heavy mocking of underlying module functions — orchestrator is wiring,
not new logic. Focus on:

- State-handoff between phases (gitea_token → seed/kestra/woodpecker/mirror)
- Phase ordering (gitea before seed, gitea before kestra, etc.)
- Skip-conditions (kestra skipped when 'kestra' not enabled, etc.)
- Failed phase aborts orchestrator (later phases not invoked)
- Partial phase keeps orchestrator running
- ExitStack tunnel cleanup
- CLI rc=0/1/2 contract
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from nexus_deploy.config import NexusConfig
from nexus_deploy.gitea import (
    CreateRepoResult,
    CreateUserResult,
    ForkResult,
    GiteaResult,
    MirrorResult,
    MirrorSetupResult,
    OAuthAppResult,
)
from nexus_deploy.infisical import BootstrapEnv
from nexus_deploy.orchestrator import (
    Orchestrator,
    OrchestratorResult,
    OrchestratorState,
    PhaseResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_config() -> NexusConfig:
    """Minimal config for orchestrator instantiation; phase methods
    are mocked so most fields don't matter."""
    return NexusConfig(
        admin_username="admin",
        gitea_admin_password="gitea-admin",
        kestra_admin_password="kestra-pw",
    )


@pytest.fixture
def minimal_env() -> BootstrapEnv:
    return BootstrapEnv(
        domain="example.com",
        admin_email="admin@example.com",
    )


@pytest.fixture
def orchestrator(minimal_config: NexusConfig, minimal_env: BootstrapEnv) -> Orchestrator:
    """An orchestrator with a typical enabled list. Phase methods
    will be mocked per-test."""
    return Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea", "kestra", "jupyter", "marimo", "woodpecker"],
        repo_name="nexus-example-com-gitea",
        gitea_repo_owner="admin",
        project_id="proj-id",
        infisical_token="infi-token",
    )


# Generic mock for a phase that returns a successful PhaseResult.
def _ok_phase(name: str) -> Any:
    return PhaseResult(name=name, status="ok")


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------


def test_orchestrator_result_is_success_when_all_ok() -> None:
    result = OrchestratorResult(
        phases=(
            PhaseResult("a", "ok"),
            PhaseResult("b", "ok"),
            PhaseResult("c", "skipped"),
        ),
        state=OrchestratorState(),
    )
    assert result.is_success
    assert not result.has_partial
    assert not result.has_hard_failure


def test_orchestrator_result_partial() -> None:
    result = OrchestratorResult(
        phases=(PhaseResult("a", "ok"), PhaseResult("b", "partial")),
        state=OrchestratorState(),
    )
    assert not result.is_success
    assert result.has_partial
    assert not result.has_hard_failure


def test_orchestrator_result_hard_failure() -> None:
    result = OrchestratorResult(
        phases=(PhaseResult("a", "failed"),),
        state=OrchestratorState(),
    )
    assert not result.is_success
    assert result.has_hard_failure


# ---------------------------------------------------------------------------
# State-handoff: gitea_token flows from gitea-configure to downstream phases
# ---------------------------------------------------------------------------


def _mk_gitea_result(token: str = "abc-token") -> GiteaResult:
    """Helper: build a GiteaResult with the given token."""
    return GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="created"),
        user=None,
        token=token,
        token_error="",
        repo=CreateRepoResult(name="repo", status="created"),
        collaborator_added=False,
        restart_services=("kestra", "jupyter"),
    )


def test_state_handoff_gitea_token_reaches_seed(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-state: gitea-configure populates state.gitea_token; seed reads it."""
    captured_tokens: list[str | None] = []

    def fake_gitea_configure(*args: Any, **kwargs: Any) -> GiteaResult:
        return _mk_gitea_result(token="abc-token")

    def fake_seed(**kwargs: Any) -> Any:
        captured_tokens.append(kwargs.get("token"))
        result = MagicMock()
        result.created = 5
        result.skipped = 0
        result.failed = 0
        return result

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._gitea.run_configure_gitea", fake_gitea_configure
    )
    monkeypatch.setattr("nexus_deploy.orchestrator._seeder.run_seed_for_repo", fake_seed)
    # Mock other phases to avoid running them
    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    for phase_name in (
        "_phase_infisical_bootstrap",
        "_phase_services_configure",
        "_phase_kestra_register",
        "_phase_woodpecker_oauth",
        "_phase_mirror_setup",
        "_phase_secret_sync_jupyter",
        "_phase_secret_sync_marimo",
    ):
        monkeypatch.setattr(orchestrator, phase_name, lambda _ssh, n=phase_name: _ok_phase(n))
    # Make examples/workspace-seeds/ "exist" so seed phase doesn't skip
    monkeypatch.setattr("nexus_deploy.orchestrator.Path.is_dir", lambda self: True)

    result = orchestrator.run_all()
    assert orchestrator.state.gitea_token == "abc-token"
    assert captured_tokens == ["abc-token"]
    assert result.state.gitea_token == "abc-token"


def test_state_handoff_restart_services_populated_from_gitea(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-stdout: state.restart_services populated by gitea-configure;
    will be emitted to stdout by the CLI."""
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._gitea.run_configure_gitea",
        lambda *a, **kw: _mk_gitea_result(),
    )
    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    for phase_name in (
        "_phase_infisical_bootstrap",
        "_phase_services_configure",
        "_phase_seed",
        "_phase_kestra_register",
        "_phase_woodpecker_oauth",
        "_phase_mirror_setup",
        "_phase_secret_sync_jupyter",
        "_phase_secret_sync_marimo",
    ):
        monkeypatch.setattr(orchestrator, phase_name, lambda _ssh, n=phase_name: _ok_phase(n))

    result = orchestrator.run_all()
    assert result.state.restart_services == ("kestra", "jupyter")


def test_state_handoff_woodpecker_creds_populated(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-stdout: woodpecker-oauth populates state.woodpecker_*."""
    orchestrator.state.gitea_token = "pre-set-token"  # skip the gitea phase

    def fake_woodpecker(*args: Any, **kwargs: Any) -> tuple[OAuthAppResult, str | None, bool]:
        return (
            OAuthAppResult(client_id="wp-id", client_secret="wp-secret", name="Woodpecker CI"),
            None,
            False,
        )

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._gitea.run_woodpecker_oauth_setup", fake_woodpecker
    )
    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    for phase_name in (
        "_phase_infisical_bootstrap",
        "_phase_services_configure",
        "_phase_gitea_configure",
        "_phase_seed",
        "_phase_kestra_register",
        "_phase_mirror_setup",
        "_phase_secret_sync_jupyter",
        "_phase_secret_sync_marimo",
    ):
        monkeypatch.setattr(orchestrator, phase_name, lambda _ssh, n=phase_name: _ok_phase(n))

    result = orchestrator.run_all()
    assert result.state.woodpecker_client_id == "wp-id"
    assert result.state.woodpecker_client_secret == "wp-secret"


def test_state_handoff_fork_populated_from_mirror(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-state: mirror-setup populates state.fork_name + fork_owner
    when a fork is provisioned."""
    orchestrator.gh_mirror_repos = ["https://github.com/owner/repo"]
    orchestrator.gh_mirror_token = "gh-tok"
    orchestrator.gitea_user_username = "user"
    orchestrator.state.gitea_token = "pre-set-token"

    def fake_mirror(*args: Any, **kwargs: Any) -> MirrorSetupResult:
        return MirrorSetupResult(
            admin_uid=1,
            admin_uid_error="",
            mirrors=(MirrorResult(name="repo", status="created"),),
            fork=ForkResult(owner="user", name="user-fork", status="created"),
            collaborator_added_count=1,
            fork_synced=True,
        )

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_mirror_setup", fake_mirror)
    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    for phase_name in (
        "_phase_infisical_bootstrap",
        "_phase_services_configure",
        "_phase_gitea_configure",
        "_phase_seed",
        "_phase_kestra_register",
        "_phase_woodpecker_oauth",
        "_phase_secret_sync_jupyter",
        "_phase_secret_sync_marimo",
    ):
        monkeypatch.setattr(orchestrator, phase_name, lambda _ssh, n=phase_name: _ok_phase(n))

    result = orchestrator.run_all()
    assert result.state.fork_name == "user-fork"
    assert result.state.fork_owner == "user"


# ---------------------------------------------------------------------------
# Phase skipping conditions
# ---------------------------------------------------------------------------


def test_phase_kestra_skipped_when_kestra_not_enabled(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],  # kestra NOT enabled
        repo_name="r",
        gitea_repo_owner="o",
    )
    ssh = MagicMock()
    result = orch._phase_kestra_register(ssh)
    assert result.status == "skipped"
    assert "kestra not enabled" in result.detail


def test_phase_mirror_skipped_when_no_mirrors_configured(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],
        repo_name="r",
        gitea_repo_owner="o",
        # gh_mirror_repos defaults to []
    )
    ssh = MagicMock()
    result = orch._phase_mirror_setup(ssh)
    assert result.status == "skipped"
    assert "no mirrors" in result.detail


def test_phase_seed_skipped_when_no_gitea_token(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    """Seed depends on gitea_token; if a prior phase didn't produce
    one, seed skips gracefully."""
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],
        repo_name="r",
        gitea_repo_owner="o",
    )
    # state.gitea_token is None by default
    ssh = MagicMock()
    result = orch._phase_seed(ssh)
    assert result.status == "skipped"
    assert "no gitea_token" in result.detail


def test_phase_woodpecker_skipped_when_woodpecker_not_enabled(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],  # NOT woodpecker
        repo_name="r",
        gitea_repo_owner="o",
    )
    ssh = MagicMock()
    result = orch._phase_woodpecker_oauth(ssh)
    assert result.status == "skipped"


def test_phase_woodpecker_skipped_when_no_gitea_token(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    """If gitea-configure didn't produce a token (e.g. partial fail),
    woodpecker-oauth skips."""
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["woodpecker"],
        repo_name="r",
        gitea_repo_owner="o",
    )
    # state.gitea_token is None
    ssh = MagicMock()
    result = orch._phase_woodpecker_oauth(ssh)
    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# Phase failure aborts orchestrator
# ---------------------------------------------------------------------------


def test_failed_phase_aborts_downstream_phases(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-abort: a phase with status='failed' stops the run; later
    phases are NOT invoked."""
    invoked: list[str] = []

    def make_phase(name: str, status: Literal["ok", "partial", "failed", "skipped"] = "ok") -> Any:
        def phase(_ssh: Any) -> PhaseResult:
            invoked.append(name)
            return PhaseResult(name=name, status=status)

        return phase

    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    monkeypatch.setattr(orchestrator, "_phase_infisical_bootstrap", make_phase("infisical"))
    # services-configure FAILS
    monkeypatch.setattr(orchestrator, "_phase_services_configure", make_phase("services", "failed"))
    # Downstream phases — should NOT be invoked
    monkeypatch.setattr(orchestrator, "_phase_gitea_configure", make_phase("gitea"))
    monkeypatch.setattr(orchestrator, "_phase_seed", make_phase("seed"))
    monkeypatch.setattr(orchestrator, "_phase_kestra_register", make_phase("kestra"))
    monkeypatch.setattr(orchestrator, "_phase_woodpecker_oauth", make_phase("woodpecker"))
    monkeypatch.setattr(orchestrator, "_phase_mirror_setup", make_phase("mirror"))
    monkeypatch.setattr(orchestrator, "_phase_secret_sync_jupyter", make_phase("ss-j"))
    monkeypatch.setattr(orchestrator, "_phase_secret_sync_marimo", make_phase("ss-m"))

    result = orchestrator.run_all()
    assert invoked == ["infisical", "services"]
    assert result.has_hard_failure
    assert len(result.phases) == 2
    assert result.phases[1].status == "failed"


def test_partial_phase_continues_to_downstream(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-continue: status='partial' is yellow-warn but not abort.
    Downstream phases still run."""
    invoked: list[str] = []

    def make_phase(name: str, status: Literal["ok", "partial", "failed", "skipped"] = "ok") -> Any:
        def phase(_ssh: Any) -> PhaseResult:
            invoked.append(name)
            return PhaseResult(name=name, status=status)

        return phase

    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    monkeypatch.setattr(orchestrator, "_phase_infisical_bootstrap", make_phase("infisical"))
    monkeypatch.setattr(
        orchestrator, "_phase_services_configure", make_phase("services", "partial")
    )
    monkeypatch.setattr(orchestrator, "_phase_gitea_configure", make_phase("gitea"))
    monkeypatch.setattr(orchestrator, "_phase_seed", make_phase("seed"))
    monkeypatch.setattr(orchestrator, "_phase_kestra_register", make_phase("kestra"))
    monkeypatch.setattr(orchestrator, "_phase_woodpecker_oauth", make_phase("woodpecker"))
    monkeypatch.setattr(orchestrator, "_phase_mirror_setup", make_phase("mirror"))
    monkeypatch.setattr(orchestrator, "_phase_secret_sync_jupyter", make_phase("ss-j"))
    monkeypatch.setattr(orchestrator, "_phase_secret_sync_marimo", make_phase("ss-m"))

    result = orchestrator.run_all()
    # All 9 phases ran despite the partial in services-configure
    assert len(invoked) == 9
    assert result.has_partial
    assert not result.has_hard_failure


# ---------------------------------------------------------------------------
# Phase ordering
# ---------------------------------------------------------------------------


def test_phases_run_in_deterministic_order(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-order: phases run in the documented order."""
    invoked: list[str] = []

    def make_phase(name: str) -> Any:
        def phase(_ssh: Any) -> PhaseResult:
            invoked.append(name)
            return PhaseResult(name=name, status="ok")

        return phase

    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    monkeypatch.setattr(orchestrator, "_phase_infisical_bootstrap", make_phase("1-infisical"))
    monkeypatch.setattr(orchestrator, "_phase_services_configure", make_phase("2-services"))
    monkeypatch.setattr(orchestrator, "_phase_gitea_configure", make_phase("3-gitea"))
    monkeypatch.setattr(orchestrator, "_phase_seed", make_phase("4-seed"))
    monkeypatch.setattr(orchestrator, "_phase_kestra_register", make_phase("5-kestra"))
    monkeypatch.setattr(orchestrator, "_phase_woodpecker_oauth", make_phase("6-woodpecker"))
    monkeypatch.setattr(orchestrator, "_phase_mirror_setup", make_phase("7-mirror"))
    monkeypatch.setattr(orchestrator, "_phase_secret_sync_jupyter", make_phase("8-ss-jupyter"))
    monkeypatch.setattr(orchestrator, "_phase_secret_sync_marimo", make_phase("9-ss-marimo"))

    orchestrator.run_all()
    assert invoked == [
        "1-infisical",
        "2-services",
        "3-gitea",
        "4-seed",
        "5-kestra",
        "6-woodpecker",
        "7-mirror",
        "8-ss-jupyter",
        "9-ss-marimo",
    ]


# ---------------------------------------------------------------------------
# CLI rc=0/1/2 contract
# ---------------------------------------------------------------------------


def test_cli_run_all_unknown_arg_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    from nexus_deploy.__main__ import _run_all

    rc = _run_all(["--bogus"])
    assert rc == 2
    assert "unknown args" in capsys.readouterr().err


def test_cli_run_all_missing_env_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _run_all

    for var in (
        "ADMIN_EMAIL",
        "REPO_NAME",
        "GITEA_REPO_OWNER",
        "ENABLED_SERVICES",
        "DOMAIN",
        "PROJECT_ID",
        "INFISICAL_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    rc = _run_all([])
    assert rc == 2
    assert "missing required env" in capsys.readouterr().err


def test_cli_run_all_rc0_on_all_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _run_all

    for var, val in (
        ("ADMIN_EMAIL", "a@b"),
        ("REPO_NAME", "r"),
        ("GITEA_REPO_OWNER", "o"),
        ("ENABLED_SERVICES", "gitea,kestra"),
        ("DOMAIN", "example.com"),
        ("PROJECT_ID", "p"),
        ("INFISICAL_TOKEN", "t"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")

    fake_result = OrchestratorResult(
        phases=(PhaseResult("p1", "ok"), PhaseResult("p2", "skipped")),
        state=OrchestratorState(restart_services=("kestra",)),
    )
    with patch.object(Orchestrator, "run_all", return_value=fake_result):
        rc = _run_all([])
    assert rc == 0
    out = capsys.readouterr().out
    # shlex.quote of "kestra" (no special chars) yields bare "kestra"
    assert "RESTART_SERVICES=kestra" in out


def test_cli_run_all_rc1_on_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_deploy.__main__ import _run_all

    for var, val in (
        ("ADMIN_EMAIL", "a@b"),
        ("REPO_NAME", "r"),
        ("GITEA_REPO_OWNER", "o"),
        ("ENABLED_SERVICES", "gitea"),
        ("DOMAIN", "example.com"),
        ("PROJECT_ID", "p"),
        ("INFISICAL_TOKEN", "t"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")

    fake_result = OrchestratorResult(
        phases=(PhaseResult("p1", "partial", "warn"),),
        state=OrchestratorState(),
    )
    with patch.object(Orchestrator, "run_all", return_value=fake_result):
        rc = _run_all([])
    assert rc == 1


def test_cli_run_all_rc2_on_hard_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_deploy.__main__ import _run_all

    for var, val in (
        ("ADMIN_EMAIL", "a@b"),
        ("REPO_NAME", "r"),
        ("GITEA_REPO_OWNER", "o"),
        ("ENABLED_SERVICES", "gitea"),
        ("DOMAIN", "example.com"),
        ("PROJECT_ID", "p"),
        ("INFISICAL_TOKEN", "t"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")

    fake_result = OrchestratorResult(
        phases=(PhaseResult("p1", "failed", "boom"),),
        state=OrchestratorState(),
    )
    with patch.object(Orchestrator, "run_all", return_value=fake_result):
        rc = _run_all([])
    assert rc == 2


def test_cli_run_all_emits_woodpecker_creds_when_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-stdout: woodpecker_* values are emitted as eval-able lines
    when populated by orchestrator state."""
    from nexus_deploy.__main__ import _run_all

    for var, val in (
        ("ADMIN_EMAIL", "a@b"),
        ("REPO_NAME", "r"),
        ("GITEA_REPO_OWNER", "o"),
        ("ENABLED_SERVICES", "gitea,woodpecker"),
        ("DOMAIN", "example.com"),
        ("PROJECT_ID", "p"),
        ("INFISICAL_TOKEN", "t"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")

    fake_result = OrchestratorResult(
        phases=(PhaseResult("p1", "ok"),),
        state=OrchestratorState(
            restart_services=(),
            woodpecker_client_id="wp-client",
            woodpecker_client_secret="wp-secret",
        ),
    )
    with patch.object(Orchestrator, "run_all", return_value=fake_result):
        _run_all([])
    out = capsys.readouterr().out
    assert (
        "WOODPECKER_GITEA_CLIENT='wp-client'" in out or "WOODPECKER_GITEA_CLIENT=wp-client" in out
    )
    assert (
        "WOODPECKER_GITEA_SECRET='wp-secret'" in out or "WOODPECKER_GITEA_SECRET=wp-secret" in out
    )


def test_cli_run_all_no_gitea_token_in_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-state-confinement: gitea_token must NOT leak to stdout —
    it's consumed entirely inside the orchestrator. Same for
    fork_name / fork_owner."""
    from nexus_deploy.__main__ import _run_all

    for var, val in (
        ("ADMIN_EMAIL", "a@b"),
        ("REPO_NAME", "r"),
        ("GITEA_REPO_OWNER", "o"),
        ("ENABLED_SERVICES", "gitea"),
        ("DOMAIN", "example.com"),
        ("PROJECT_ID", "p"),
        ("INFISICAL_TOKEN", "t"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")

    fake_result = OrchestratorResult(
        phases=(PhaseResult("p1", "ok"),),
        state=OrchestratorState(
            gitea_token="SECRET-TOKEN-ABCDEF",
            fork_name="some-fork",
            fork_owner="some-owner",
        ),
    )
    with patch.object(Orchestrator, "run_all", return_value=fake_result):
        _run_all([])
    captured = capsys.readouterr()
    assert "SECRET-TOKEN-ABCDEF" not in captured.out
    assert "SECRET-TOKEN-ABCDEF" not in captured.err
    assert "GITEA_TOKEN" not in captured.out
    assert "FORK_NAME" not in captured.out
    assert "FORK_OWNER" not in captured.out


def test_phase_infisical_skipped_without_creds(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=[],
        repo_name="r",
        gitea_repo_owner="o",
        # project_id + infisical_token left as None
    )
    result = orch._phase_infisical_bootstrap(MagicMock())
    assert result.status == "skipped"


def test_phase_gitea_configure_skipped_when_gitea_disabled(
    minimal_env: BootstrapEnv,
) -> None:
    config = NexusConfig()
    orch = Orchestrator(
        config=config,
        bootstrap_env=minimal_env,
        enabled_services=[],
        repo_name="r",
        gitea_repo_owner="o",
    )
    result = orch._phase_gitea_configure(MagicMock())
    assert result.status == "skipped"


def test_phase_gitea_configure_partial_without_admin_password(
    minimal_env: BootstrapEnv,
) -> None:
    config = NexusConfig()  # no gitea_admin_password
    orch = Orchestrator(
        config=config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],
        repo_name="r",
        gitea_repo_owner="o",
    )
    result = orch._phase_gitea_configure(MagicMock())
    assert result.status == "partial"
    assert "GITEA_ADMIN_PASS" in result.detail


def test_phase_kestra_partial_without_admin_pass(minimal_env: BootstrapEnv) -> None:
    config = NexusConfig()  # no kestra_admin_password
    orch = Orchestrator(
        config=config,
        bootstrap_env=minimal_env,
        enabled_services=["kestra"],
        repo_name="r",
        gitea_repo_owner="o",
    )
    result = orch._phase_kestra_register(MagicMock())
    assert result.status == "partial"


def test_phase_secret_sync_jupyter_skipped_when_disabled(minimal_env: BootstrapEnv) -> None:
    config = NexusConfig()
    orch = Orchestrator(
        config=config,
        bootstrap_env=minimal_env,
        enabled_services=[],
        repo_name="r",
        gitea_repo_owner="o",
    )
    result = orch._phase_secret_sync_jupyter(MagicMock())
    assert result.status == "skipped"


def test_phase_secret_sync_marimo_skipped_when_disabled(minimal_env: BootstrapEnv) -> None:
    config = NexusConfig()
    orch = Orchestrator(
        config=config,
        bootstrap_env=minimal_env,
        enabled_services=[],
        repo_name="r",
        gitea_repo_owner="o",
    )
    result = orch._phase_secret_sync_marimo(MagicMock())
    assert result.status == "skipped"


def test_phase_secret_sync_partial_without_creds(minimal_env: BootstrapEnv) -> None:
    config = NexusConfig()
    orch = Orchestrator(
        config=config,
        bootstrap_env=minimal_env,
        enabled_services=["jupyter"],
        repo_name="r",
        gitea_repo_owner="o",
        # project_id + infisical_token left as None
    )
    result = orch._phase_secret_sync_jupyter(MagicMock())
    assert result.status == "partial"


def test_cli_run_all_unexpected_exception_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _run_all

    for var, val in (
        ("ADMIN_EMAIL", "a@b"),
        ("REPO_NAME", "r"),
        ("GITEA_REPO_OWNER", "o"),
        ("ENABLED_SERVICES", "gitea"),
        ("DOMAIN", "example.com"),
        ("PROJECT_ID", "p"),
        ("INFISICAL_TOKEN", "t"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")

    def boom(self: Any) -> Any:
        raise RuntimeError("secret-bearing-message-NEVER-PRINT")

    with patch.object(Orchestrator, "run_all", boom):
        rc = _run_all([])
    assert rc == 2
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "secret-bearing-message-NEVER-PRINT" not in captured.err
    assert "secret-bearing-message-NEVER-PRINT" not in captured.out


# ---------------------------------------------------------------------------
# Per-phase happy-path bodies (mock underlying module functions to drive
# the try-block + result-mapping logic to ok/partial/failed PhaseResults).
# ---------------------------------------------------------------------------


class _FakeTunnel:
    """Stand-in for SSHClient.port_forward()'s context manager.
    Yields the port int that __enter__ returns."""

    def __init__(self, port: int = 5500) -> None:
        self._port = port

    def __enter__(self) -> int:
        return self._port

    def __exit__(self, *_a: Any) -> None:
        return None


def _ssh_with_tunnel() -> Any:
    ssh = MagicMock()
    ssh.port_forward = MagicMock(return_value=_FakeTunnel())
    return ssh


def test_phase_infisical_bootstrap_ok(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.infisical import BootstrapResult

    fake_client = MagicMock()
    fake_client.bootstrap.return_value = BootstrapResult(folders_built=3, pushed=12, failed=0)
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._infisical.InfisicalClient", lambda **_kw: fake_client
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._infisical.compute_folders", lambda *_a, **_kw: ()
    )
    result = orchestrator._phase_infisical_bootstrap(MagicMock())
    assert result.status == "ok"
    assert "built=3" in result.detail
    assert "pushed=12" in result.detail


def test_phase_infisical_bootstrap_partial_when_failed_gt_zero(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.infisical import BootstrapResult

    fake_client = MagicMock()
    fake_client.bootstrap.return_value = BootstrapResult(folders_built=2, pushed=5, failed=1)
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._infisical.InfisicalClient", lambda **_kw: fake_client
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._infisical.compute_folders", lambda *_a, **_kw: ()
    )
    result = orchestrator._phase_infisical_bootstrap(MagicMock())
    assert result.status == "partial"
    assert "failed=1" in result.detail


def test_phase_infisical_bootstrap_failed_on_transport(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def boom(**_kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr("nexus_deploy.orchestrator._infisical.InfisicalClient", boom)
    result = orchestrator._phase_infisical_bootstrap(MagicMock())
    assert result.status == "failed"
    assert "TimeoutExpired" in result.detail


def test_phase_infisical_bootstrap_skipped_when_creds_missing(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=[],
        repo_name="r",
        gitea_repo_owner="o",
        # project_id / infisical_token default to None
    )
    result = orch._phase_infisical_bootstrap(MagicMock())
    assert result.status == "skipped"


def test_phase_services_configure_ok(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.services import HookResult, SetupResult

    fake = SetupResult(
        hooks=(
            HookResult(name="x", status="configured"),
            HookResult(name="y", status="already-configured"),
        )
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._services.run_admin_setups", lambda *_a, **_kw: fake
    )
    result = orchestrator._phase_services_configure(MagicMock())
    assert result.status == "ok"
    assert "configured=1" in result.detail


def test_phase_services_configure_partial_when_failed(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.services import HookResult, SetupResult

    fake = SetupResult(
        hooks=(
            HookResult(name="x", status="configured"),
            HookResult(name="y", status="failed"),
        )
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._services.run_admin_setups", lambda *_a, **_kw: fake
    )
    result = orchestrator._phase_services_configure(MagicMock())
    assert result.status == "partial"
    assert "failed=1" in result.detail


def test_phase_gitea_configure_ok_populates_state(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _mk_gitea_result(token="abc-token")
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._gitea.run_configure_gitea", lambda *_a, **_kw: fake
    )
    result = orchestrator._phase_gitea_configure(_ssh_with_tunnel())
    assert result.status == "ok"
    assert orchestrator.state.gitea_token == "abc-token"
    assert orchestrator.state.restart_services == ("kestra", "jupyter")


def test_phase_gitea_configure_partial_when_subresult_unsuccessful(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = GiteaResult(
        db_pw_synced=True,
        admin=CreateUserResult(name="admin", status="failed"),  # is_success=False
        user=None,
        token="abc",
        token_error="",
        repo=CreateRepoResult(name="repo", status="created"),
        collaborator_added=False,
        restart_services=(),
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._gitea.run_configure_gitea", lambda *_a, **_kw: fake
    )
    result = orchestrator._phase_gitea_configure(_ssh_with_tunnel())
    assert result.status == "partial"


def test_phase_gitea_configure_partial_when_admin_password_missing(
    minimal_env: BootstrapEnv,
) -> None:
    cfg = NexusConfig(admin_username="admin")  # no gitea_admin_password
    orch = Orchestrator(
        config=cfg,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],
        repo_name="r",
        gitea_repo_owner="o",
    )
    result = orch._phase_gitea_configure(MagicMock())
    assert result.status == "partial"
    assert "GITEA_ADMIN_PASS" in result.detail


def test_phase_gitea_configure_failed_on_transport(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_configure_gitea", boom)
    result = orchestrator._phase_gitea_configure(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "OSError" in result.detail


def test_phase_seed_ok(orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_deploy.seeder import SeedResult

    orchestrator.state.gitea_token = "tok"
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._seeder.run_seed_for_repo",
        lambda **_kw: SeedResult(created=4, skipped=1, failed=0),
    )
    monkeypatch.setattr("nexus_deploy.orchestrator.Path.is_dir", lambda self: True)
    result = orchestrator._phase_seed(MagicMock())
    assert result.status == "ok"
    assert "created=4" in result.detail


def test_phase_seed_partial_when_some_failed_but_progress_made(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.seeder import SeedResult

    orchestrator.state.gitea_token = "tok"
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._seeder.run_seed_for_repo",
        lambda **_kw: SeedResult(created=2, skipped=1, failed=1),
    )
    monkeypatch.setattr("nexus_deploy.orchestrator.Path.is_dir", lambda self: True)
    result = orchestrator._phase_seed(MagicMock())
    assert result.status == "partial"


def test_phase_seed_failed_when_zero_progress(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.seeder import SeedResult

    orchestrator.state.gitea_token = "tok"
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._seeder.run_seed_for_repo",
        lambda **_kw: SeedResult(created=0, skipped=0, failed=5),
    )
    monkeypatch.setattr("nexus_deploy.orchestrator.Path.is_dir", lambda self: True)
    result = orchestrator._phase_seed(MagicMock())
    assert result.status == "failed"


def test_phase_seed_skipped_when_seeds_dir_missing(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.state.gitea_token = "tok"
    monkeypatch.setattr("nexus_deploy.orchestrator.Path.is_dir", lambda self: False)
    result = orchestrator._phase_seed(MagicMock())
    assert result.status == "skipped"
    assert "missing" in result.detail


def test_phase_kestra_register_ok(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.kestra import RegisterResult, SystemFlowsResult

    fake = SystemFlowsResult(
        flows=(RegisterResult(name="git-sync", status="created"),),
        execution_state="SUCCESS",
        verify_skipped_reason=None,
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._kestra.run_register_system_flows",
        lambda *_a, **_kw: fake,
    )
    result = orchestrator._phase_kestra_register(_ssh_with_tunnel())
    assert result.status == "ok"
    assert "execution=SUCCESS" in result.detail


def test_phase_kestra_register_partial_when_admin_email_missing(
    minimal_config: NexusConfig,
) -> None:
    env = BootstrapEnv(domain="example.com")  # admin_email missing
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=env,
        enabled_services=["kestra"],
        repo_name="r",
        gitea_repo_owner="o",
    )
    result = orch._phase_kestra_register(MagicMock())
    assert result.status == "partial"
    assert "ADMIN_EMAIL" in result.detail


def test_phase_kestra_register_failed_on_transport(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise subprocess.CalledProcessError(returncode=1, cmd="x")

    monkeypatch.setattr("nexus_deploy.orchestrator._kestra.run_register_system_flows", boom)
    result = orchestrator._phase_kestra_register(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "CalledProcessError" in result.detail


def test_phase_woodpecker_oauth_partial_when_domain_missing(
    minimal_config: NexusConfig,
) -> None:
    env = BootstrapEnv()  # no domain
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=env,
        enabled_services=["woodpecker"],
        repo_name="r",
        gitea_repo_owner="o",
    )
    orch.state.gitea_token = "tok"
    result = orch._phase_woodpecker_oauth(MagicMock())
    assert result.status == "partial"
    assert "DOMAIN" in result.detail


def test_phase_woodpecker_oauth_failed_when_rotation_half_complete(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If create succeeded but rotation didn't, the failed branch
    surfaces the half-complete state."""
    orchestrator.state.gitea_token = "tok"

    def fake_oauth(*_a: Any, **_kw: Any) -> tuple[Any, str | None, bool]:
        return (None, "rotation aborted mid-flight", True)

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_woodpecker_oauth_setup", fake_oauth)
    result = orchestrator._phase_woodpecker_oauth(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "half-complete" in result.detail


def test_phase_woodpecker_oauth_partial_when_create_failed_no_rotation(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.state.gitea_token = "tok"

    def fake_oauth(*_a: Any, **_kw: Any) -> tuple[Any, str | None, bool]:
        return (None, "create returned 422", False)

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_woodpecker_oauth_setup", fake_oauth)
    result = orchestrator._phase_woodpecker_oauth(_ssh_with_tunnel())
    assert result.status == "partial"


def test_phase_mirror_setup_partial_when_gh_token_missing(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],
        repo_name="r",
        gitea_repo_owner="o",
        gh_mirror_repos=["https://github.com/x/y"],
    )
    orch.state.gitea_token = "tok"
    result = orch._phase_mirror_setup(MagicMock())
    assert result.status == "partial"
    assert "GH_MIRROR_TOKEN" in result.detail


def test_phase_mirror_setup_partial_when_some_mirrors_failed(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.gh_mirror_repos = ["https://github.com/x/y"]
    orchestrator.gh_mirror_token = "gh-tok"
    orchestrator.state.gitea_token = "tok"

    def fake_mirror(*_a: Any, **_kw: Any) -> MirrorSetupResult:
        return MirrorSetupResult(
            admin_uid=1,
            admin_uid_error="",
            mirrors=(
                MirrorResult(name="x", status="created"),
                MirrorResult(name="y", status="failed"),
            ),
            fork=None,
            collaborator_added_count=0,
            fork_synced=False,
        )

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_mirror_setup", fake_mirror)
    result = orchestrator._phase_mirror_setup(_ssh_with_tunnel())
    assert result.status == "partial"


def test_phase_secret_sync_jupyter_skipped_when_not_enabled(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["gitea"],  # NOT jupyter
        repo_name="r",
        gitea_repo_owner="o",
        project_id="p",
        infisical_token="t",
    )
    result = orch._phase_secret_sync_jupyter(MagicMock())
    assert result.status == "skipped"


def test_phase_secret_sync_jupyter_partial_when_creds_missing(
    minimal_config: NexusConfig, minimal_env: BootstrapEnv
) -> None:
    orch = Orchestrator(
        config=minimal_config,
        bootstrap_env=minimal_env,
        enabled_services=["jupyter"],
        repo_name="r",
        gitea_repo_owner="o",
        # project_id / infisical_token default None
    )
    result = orch._phase_secret_sync_jupyter(MagicMock())
    assert result.status == "partial"


def test_phase_secret_sync_jupyter_ok(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.secret_sync import SyncResult

    fake = SyncResult(
        pushed=5,
        skipped_invalid_name=0,
        skipped_multiline=0,
        failed_folders=0,
        collisions=0,
        succeeded_folders=2,
        wrote=True,
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._secret_sync.run_sync_for_stack",
        lambda *_a, **_kw: fake,
    )
    result = orchestrator._phase_secret_sync(MagicMock(), "jupyter")
    assert result.status == "ok"
    assert "pushed=5" in result.detail


def test_phase_secret_sync_partial_when_wrote_with_some_failures(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.secret_sync import SyncResult

    fake = SyncResult(
        pushed=3,
        skipped_invalid_name=0,
        skipped_multiline=0,
        failed_folders=1,
        collisions=0,
        succeeded_folders=2,
        wrote=True,
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._secret_sync.run_sync_for_stack",
        lambda *_a, **_kw: fake,
    )
    result = orchestrator._phase_secret_sync(MagicMock(), "jupyter")
    assert result.status == "partial"


def test_phase_secret_sync_outage_gate_ok(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wrote=False but with some succeeded folders means the outage
    gate skipped the write — still 'ok'."""
    from nexus_deploy.secret_sync import SyncResult

    fake = SyncResult(
        pushed=0,
        skipped_invalid_name=0,
        skipped_multiline=0,
        failed_folders=0,
        collisions=0,
        succeeded_folders=2,
        wrote=False,
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._secret_sync.run_sync_for_stack",
        lambda *_a, **_kw: fake,
    )
    result = orchestrator._phase_secret_sync(MagicMock(), "jupyter")
    assert result.status == "ok"
    assert "outage" in result.detail


def test_phase_secret_sync_failed_on_transport(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("network down")

    monkeypatch.setattr("nexus_deploy.orchestrator._secret_sync.run_sync_for_stack", boom)
    result = orchestrator._phase_secret_sync(MagicMock(), "jupyter")
    assert result.status == "failed"
    assert "OSError" in result.detail


def test_run_all_resets_results_between_runs(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-reset: running twice on the same instance produces independent
    results (no accumulation)."""
    monkeypatch.setattr("nexus_deploy.orchestrator.SSHClient", MagicMock())
    for phase_name in (
        "_phase_infisical_bootstrap",
        "_phase_services_configure",
        "_phase_gitea_configure",
        "_phase_seed",
        "_phase_kestra_register",
        "_phase_woodpecker_oauth",
        "_phase_mirror_setup",
        "_phase_secret_sync_jupyter",
        "_phase_secret_sync_marimo",
    ):
        monkeypatch.setattr(orchestrator, phase_name, lambda _ssh, n=phase_name: _ok_phase(n))
    r1 = orchestrator.run_all()
    r2 = orchestrator.run_all()
    assert len(r1.phases) == 9
    assert len(r2.phases) == 9


# ---------------------------------------------------------------------------
# Exception coverage — catch-all + module-specific exception types
# ---------------------------------------------------------------------------


def test_phase_infisical_bootstrap_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch-all branch (RuntimeError, not transport)."""

    def boom(**_kw: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("nexus_deploy.orchestrator._infisical.InfisicalClient", boom)
    result = orchestrator._phase_infisical_bootstrap(MagicMock())
    assert result.status == "failed"
    assert "RuntimeError" in result.detail
    assert "unexpected" in result.detail.lower()


def test_phase_services_configure_failed_on_transport(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("conn refused")

    monkeypatch.setattr("nexus_deploy.orchestrator._services.run_admin_setups", boom)
    result = orchestrator._phase_services_configure(MagicMock())
    assert result.status == "failed"
    assert "OSError" in result.detail


def test_phase_services_configure_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise ValueError("oops")

    monkeypatch.setattr("nexus_deploy.orchestrator._services.run_admin_setups", boom)
    result = orchestrator._phase_services_configure(MagicMock())
    assert result.status == "failed"
    assert "ValueError" in result.detail


def test_phase_gitea_configure_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("oops")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_configure_gitea", boom)
    result = orchestrator._phase_gitea_configure(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "RuntimeError" in result.detail


def test_phase_seed_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.state.gitea_token = "tok"

    def boom(**_kw: Any) -> Any:
        raise RuntimeError("oops")

    monkeypatch.setattr("nexus_deploy.orchestrator._seeder.run_seed_for_repo", boom)
    monkeypatch.setattr("nexus_deploy.orchestrator.Path.is_dir", lambda self: True)
    result = orchestrator._phase_seed(MagicMock())
    assert result.status == "failed"
    assert "RuntimeError" in result.detail


def test_phase_kestra_register_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("oops")

    monkeypatch.setattr("nexus_deploy.orchestrator._kestra.run_register_system_flows", boom)
    result = orchestrator._phase_kestra_register(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "RuntimeError" in result.detail


def test_phase_kestra_register_partial_when_not_success(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flows attempted but at least one failed → SystemFlowsResult.is_success=False."""
    from nexus_deploy.kestra import RegisterResult, SystemFlowsResult

    fake = SystemFlowsResult(
        flows=(RegisterResult(name="git-sync", status="failed"),),
        execution_state=None,
        verify_skipped_reason="execution failed",
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._kestra.run_register_system_flows",
        lambda *_a, **_kw: fake,
    )
    result = orchestrator._phase_kestra_register(_ssh_with_tunnel())
    assert result.status == "partial"


def test_phase_woodpecker_oauth_failed_on_gitea_error(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GiteaError branch (lines 486-491)."""
    from nexus_deploy.gitea import GiteaError

    orchestrator.state.gitea_token = "tok"

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise GiteaError("API 500")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_woodpecker_oauth_setup", boom)
    result = orchestrator._phase_woodpecker_oauth(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "API 500" in result.detail


def test_phase_woodpecker_oauth_failed_on_transport(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.state.gitea_token = "tok"

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("conn refused")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_woodpecker_oauth_setup", boom)
    result = orchestrator._phase_woodpecker_oauth(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "OSError" in result.detail


def test_phase_woodpecker_oauth_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.state.gitea_token = "tok"

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("oops")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_woodpecker_oauth_setup", boom)
    result = orchestrator._phase_woodpecker_oauth(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "RuntimeError" in result.detail


def test_phase_woodpecker_oauth_ok_populates_state(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: result populated → state mutation + ok status."""
    orchestrator.state.gitea_token = "tok"

    def fake_oauth(*_a: Any, **_kw: Any) -> tuple[OAuthAppResult, str | None, bool]:
        return (
            OAuthAppResult(client_id="cid", client_secret="csec", name="Woodpecker CI"),
            None,
            False,
        )

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_woodpecker_oauth_setup", fake_oauth)
    result = orchestrator._phase_woodpecker_oauth(_ssh_with_tunnel())
    assert result.status == "ok"
    assert orchestrator.state.woodpecker_client_id == "cid"
    assert orchestrator.state.woodpecker_client_secret == "csec"


def test_phase_mirror_setup_failed_on_transport(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.gh_mirror_repos = ["https://github.com/x/y"]
    orchestrator.gh_mirror_token = "gh-tok"
    orchestrator.state.gitea_token = "tok"

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("conn refused")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_mirror_setup", boom)
    result = orchestrator._phase_mirror_setup(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "OSError" in result.detail


def test_phase_mirror_setup_failed_on_gitea_error(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_deploy.gitea import GiteaError

    orchestrator.gh_mirror_repos = ["https://github.com/x/y"]
    orchestrator.gh_mirror_token = "gh-tok"
    orchestrator.state.gitea_token = "tok"

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise GiteaError("API 403")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_mirror_setup", boom)
    result = orchestrator._phase_mirror_setup(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "API 403" in result.detail


def test_phase_mirror_setup_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator.gh_mirror_repos = ["https://github.com/x/y"]
    orchestrator.gh_mirror_token = "gh-tok"
    orchestrator.state.gitea_token = "tok"

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("oops")

    monkeypatch.setattr("nexus_deploy.orchestrator._gitea.run_mirror_setup", boom)
    result = orchestrator._phase_mirror_setup(_ssh_with_tunnel())
    assert result.status == "failed"
    assert "RuntimeError" in result.detail


def test_phase_secret_sync_unexpected_exception(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("oops")

    monkeypatch.setattr("nexus_deploy.orchestrator._secret_sync.run_sync_for_stack", boom)
    result = orchestrator._phase_secret_sync(MagicMock(), "jupyter")
    assert result.status == "failed"
    assert "RuntimeError" in result.detail


def test_phase_secret_sync_partial_when_no_usable_result(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wrote=False AND no folders touched → no usable result → partial."""
    from nexus_deploy.secret_sync import SyncResult

    fake = SyncResult(
        pushed=0,
        skipped_invalid_name=0,
        skipped_multiline=0,
        failed_folders=0,
        collisions=0,
        succeeded_folders=0,
        wrote=False,
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._secret_sync.run_sync_for_stack",
        lambda *_a, **_kw: fake,
    )
    result = orchestrator._phase_secret_sync(MagicMock(), "jupyter")
    assert result.status == "partial"


# ---------------------------------------------------------------------------
# Phase 4a (#505) — pre-bootstrap pipeline phases.
# Each new phase is exercised with a happy path + at least one failure
# / partial path to lock the PhaseResult contract.
# ---------------------------------------------------------------------------


def test_phase_service_env_happy_path(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """All renders ok → status=ok with the rendered/skipped counts in detail."""
    from nexus_deploy.service_env import ServiceEnvResult, ServiceRenderResult

    fake_result = ServiceEnvResult(
        services=(
            ServiceRenderResult(service="postgres", status="rendered"),
            ServiceRenderResult(service="kestra", status="rendered"),
        ),
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._service_env.render_all_env_files",
        lambda *_a, **_kw: fake_result,
    )
    # Skip the gitea-block append branch by clearing the bootstrap env's
    # repo_owner so the workspace_coords_complete check fails.
    orchestrator.bootstrap_env = type(orchestrator.bootstrap_env)(
        domain="example.com",
        admin_email="admin@example.com",
        gitea_repo_owner=None,
    )
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_service_env()
    assert result.status == "ok"
    assert "rendered=2" in result.detail


def test_phase_service_env_partial_when_failures(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Some renders failed but at least one succeeded → status=partial."""
    from nexus_deploy.service_env import ServiceEnvResult, ServiceRenderResult

    fake_result = ServiceEnvResult(
        services=(
            ServiceRenderResult(service="postgres", status="rendered"),
            ServiceRenderResult(service="kestra", status="failed", detail="oops"),
        ),
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._service_env.render_all_env_files",
        lambda *_a, **_kw: fake_result,
    )
    orchestrator.bootstrap_env = type(orchestrator.bootstrap_env)(
        domain="example.com",
        admin_email="admin@example.com",
        gitea_repo_owner=None,
    )
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_service_env()
    assert result.status == "partial"
    assert "rendered=1" in result.detail
    assert "failed=1" in result.detail


def test_phase_service_env_failed_on_service_env_error(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """ServiceEnvError (e.g. SFTPGo with empty password) → status=failed."""
    from nexus_deploy.service_env import ServiceEnvError

    def _raises(*_a: Any, **_kw: Any) -> None:
        raise ServiceEnvError("SFTPGo password is empty")

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._service_env.render_all_env_files",
        _raises,
    )
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_service_env()
    assert result.status == "failed"
    assert "SFTPGo" in result.detail


def test_phase_stack_sync_happy_path(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """rsync ok + cleanup ok → status=ok."""
    from nexus_deploy.stack_sync import CleanupResult, RsyncResult, StackSyncResult

    fake = StackSyncResult(
        rsync=(
            RsyncResult(service="postgres", status="synced"),
            RsyncResult(service="kestra", status="synced"),
        ),
        cleanup=CleanupResult(stopped=0, removed=3, failed=0),
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._stack_sync.run_stack_sync",
        lambda *_a, **_kw: fake,
    )
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_stack_sync()
    assert result.status == "ok"
    assert "rsync_synced=2" in result.detail
    assert "cleanup_removed=3" in result.detail


def test_phase_stack_sync_failed_when_cleanup_unparseable(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """cleanup is None (no parseable RESULT) → status=failed."""
    from nexus_deploy.stack_sync import RsyncResult, StackSyncResult

    fake = StackSyncResult(
        rsync=(RsyncResult(service="postgres", status="synced"),),
        cleanup=None,
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._stack_sync.run_stack_sync",
        lambda *_a, **_kw: fake,
    )
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_stack_sync()
    assert result.status == "failed"
    assert "no parseable RESULT" in result.detail


def test_phase_firewall_configure_zero_entry(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Empty firewall_rules → zero-entry mode → status=ok."""
    from nexus_deploy.firewall import GenerateResult, WriteResult

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._firewall.configure",
        lambda *_a, **_kw: (
            GenerateResult(compiled=(), redpanda=None, zero_entry=True),
            WriteResult(written=()),
        ),
    )
    orchestrator.firewall_json = "{}"
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_firewall_configure()
    assert result.status == "ok"
    assert "zero-entry" in result.detail


def test_phase_firewall_configure_partial_when_skipped(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Skipped services → status=partial (per #531 R7 #4)."""
    from nexus_deploy.firewall import (
        CompiledOverride,
        GenerateResult,
        WriteResult,
    )

    fake_compiled = CompiledOverride(
        service="postgres",
        target_path=tmp_path / "postgres" / "docker-compose.firewall.yml",
        yaml_content="services: {}\n",
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._firewall.configure",
        lambda *_a, **_kw: (
            GenerateResult(
                compiled=(fake_compiled,),
                redpanda=None,
                skipped=("kestra",),
                zero_entry=False,
            ),
            WriteResult(written=(fake_compiled.target_path,)),
        ),
    )
    orchestrator.firewall_json = '{"postgres-1": {"port": 5432}, "kestra-1": {"port": 8080}}'
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_firewall_configure()
    assert result.status == "partial"
    assert "skipped=1" in result.detail


def test_phase_firewall_configure_failed_on_value_error(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """ValueError (e.g. missing template token) → status=failed."""

    def _raises(**_kw: Any) -> None:
        raise ValueError("RedPanda template missing __REDPANDA_KAFKA_DOMAIN__")

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._firewall.configure",
        _raises,
    )
    orchestrator.project_root = tmp_path
    result = orchestrator._phase_firewall_configure()
    assert result.status == "failed"
    assert "REDPANDA_KAFKA_DOMAIN" in result.detail


def test_phase_compose_up_happy_path(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All containers started → status=ok."""
    from nexus_deploy.compose_runner import ComposeUpResult

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._compose_runner.run_compose_up",
        lambda *_a, **_kw: ComposeUpResult(started=10, failed=0),
    )
    result = orchestrator._phase_compose_up()
    assert result.status == "ok"
    assert "started=10" in result.detail


def test_phase_compose_up_partial_on_failures(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some containers failed → status=partial."""
    from nexus_deploy.compose_runner import ComposeUpResult

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._compose_runner.run_compose_up",
        lambda *_a, **_kw: ComposeUpResult(started=8, failed=2),
    )
    result = orchestrator._phase_compose_up()
    assert result.status == "partial"
    assert "started=8" in result.detail
    assert "failed=2" in result.detail


def test_phase_infisical_provision_happy_path(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provision returns usable creds → state populated, status=ok."""
    from nexus_deploy.infisical import ProvisionResult

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._infisical.provision_admin",
        lambda *_a, **_kw: ProvisionResult(
            status="freshly-bootstrapped",
            token="real-token",
            project_id="proj-real",
        ),
    )
    orchestrator.admin_password_infisical = "pw"
    result = orchestrator._phase_infisical_provision()
    assert result.status == "ok"
    assert orchestrator.state.infisical_token == "real-token"
    assert orchestrator.state.project_id == "proj-real"
    assert orchestrator.infisical_token == "real-token"
    assert orchestrator.project_id == "proj-real"


def test_phase_infisical_provision_partial_on_dropped_creds(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_credentials=False → partial."""
    from nexus_deploy.infisical import ProvisionResult

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._infisical.provision_admin",
        lambda *_a, **_kw: ProvisionResult(status="loaded-existing", token=None, project_id=None),
    )
    # Reset to None so the post-bootstrap fallback doesn't mask the
    # partial result.
    orchestrator.state.infisical_token = None
    orchestrator.admin_password_infisical = "pw"
    result = orchestrator._phase_infisical_provision()
    assert result.status == "partial"
    assert "no usable credentials" in result.detail
    assert orchestrator.state.infisical_token is None


def test_phase_infisical_provision_skipped_when_password_missing(
    orchestrator: Orchestrator,
) -> None:
    """No admin password → status=skipped."""
    orchestrator.admin_password_infisical = None
    result = orchestrator._phase_infisical_provision()
    assert result.status == "skipped"
    assert "admin_password_infisical" in result.detail


def test_run_pre_bootstrap_runs_phases_in_order(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 5 pre-bootstrap phases run in deterministic order."""
    invocation_order: list[str] = []

    def _make_phase(name: str) -> Any:
        def _phase(self: Any) -> PhaseResult:
            invocation_order.append(name)
            return PhaseResult(name=name, status="ok")

        return _phase

    monkeypatch.setattr(Orchestrator, "_phase_service_env", _make_phase("service-env"))
    monkeypatch.setattr(Orchestrator, "_phase_stack_sync", _make_phase("stack-sync"))
    monkeypatch.setattr(
        Orchestrator,
        "_phase_firewall_configure",
        _make_phase("firewall-configure"),
    )
    monkeypatch.setattr(Orchestrator, "_phase_compose_up", _make_phase("compose-up"))
    monkeypatch.setattr(
        Orchestrator,
        "_phase_infisical_provision",
        _make_phase("infisical-provision"),
    )
    result = orchestrator.run_pre_bootstrap()
    assert invocation_order == [
        "service-env",
        "stack-sync",
        "firewall-configure",
        "compose-up",
        "infisical-provision",
    ]
    assert [p.name for p in result.phases] == invocation_order
    assert result.is_success


def test_run_pre_bootstrap_aborts_on_failure(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase with status='failed' aborts the run; downstream phases skipped."""
    invocation_order: list[str] = []

    def _make_phase(name: str, status: Literal["ok", "failed"]) -> Any:
        def _phase(self: Any) -> PhaseResult:
            invocation_order.append(name)
            return PhaseResult(name=name, status=status)

        return _phase

    monkeypatch.setattr(Orchestrator, "_phase_service_env", _make_phase("service-env", "ok"))
    monkeypatch.setattr(
        Orchestrator,
        "_phase_stack_sync",
        _make_phase("stack-sync", "failed"),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_phase_firewall_configure",
        _make_phase("firewall-configure", "ok"),
    )
    result = orchestrator.run_pre_bootstrap()
    assert invocation_order == ["service-env", "stack-sync"]
    assert result.has_hard_failure


def test_run_pre_bootstrap_partial_continues_to_downstream(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial-success phase doesn't abort — all 5 phases still run."""
    invocation_order: list[str] = []

    def _make_phase(name: str, status: Literal["ok", "partial"]) -> Any:
        def _phase(self: Any) -> PhaseResult:
            invocation_order.append(name)
            return PhaseResult(name=name, status=status)

        return _phase

    monkeypatch.setattr(Orchestrator, "_phase_service_env", _make_phase("service-env", "ok"))
    monkeypatch.setattr(
        Orchestrator,
        "_phase_stack_sync",
        _make_phase("stack-sync", "partial"),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_phase_firewall_configure",
        _make_phase("firewall-configure", "ok"),
    )
    monkeypatch.setattr(Orchestrator, "_phase_compose_up", _make_phase("compose-up", "ok"))
    monkeypatch.setattr(
        Orchestrator,
        "_phase_infisical_provision",
        _make_phase("infisical-provision", "ok"),
    )
    result = orchestrator.run_pre_bootstrap()
    assert len(invocation_order) == 5
    assert result.has_partial
    assert not result.has_hard_failure


def test_run_pre_bootstrap_resets_stale_credentials(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-state-reset (PR #532 R1 #4): re-running on the same instance
    after a previous run that produced partial credentials must NOT
    leak the stale token/project_id into the second run's stdout.
    Reset happens at the top of run_pre_bootstrap before any phase
    fires."""

    # Simulate a first run that left stale credentials on BOTH state
    # mirrors and the orchestrator's own fields. Both must be cleared
    # (R1 #4 cleared state; R2 #3 also clears self.fields so a follow-on
    # run_all() can't pick up stale creds via the post-bootstrap phase
    # gating).
    orchestrator.state.infisical_token = "stale-token-from-prior-run"
    orchestrator.state.project_id = "stale-proj-from-prior-run"
    orchestrator.infisical_token = "stale-token-from-prior-run"
    orchestrator.project_id = "stale-proj-from-prior-run"

    # Mock all phases to ok except infisical-provision which produces
    # status='partial' WITHOUT populating state (simulating the bug
    # condition: dropped credentials).
    def _ok_phase(_self: Any) -> PhaseResult:
        return PhaseResult(name="ok-phase", status="ok")

    def _partial_no_creds(_self: Any) -> PhaseResult:
        return PhaseResult(name="infisical-provision", status="partial", detail="dropped")

    monkeypatch.setattr(Orchestrator, "_phase_service_env", _ok_phase)
    monkeypatch.setattr(Orchestrator, "_phase_stack_sync", _ok_phase)
    monkeypatch.setattr(Orchestrator, "_phase_firewall_configure", _ok_phase)
    monkeypatch.setattr(Orchestrator, "_phase_compose_up", _ok_phase)
    monkeypatch.setattr(Orchestrator, "_phase_infisical_provision", _partial_no_creds)

    result = orchestrator.run_pre_bootstrap()
    assert result.has_partial
    # Stale credentials MUST be cleared on BOTH surfaces, even though
    # the infisical-provision phase didn't populate fresh ones.
    assert orchestrator.state.infisical_token is None
    assert orchestrator.state.project_id is None
    assert orchestrator.infisical_token is None
    assert orchestrator.project_id is None


def test_phase_service_env_skips_gitea_block_on_incomplete_coords(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """R-gitea-guard (PR #532 R1 #3): when ANY workspace coord is
    empty (e.g. gitea_user_password=None), the Gitea workspace block
    is NOT appended — refuses to write a broken block with empty
    PASSWORD/AUTHOR fields. Mirrors the CLI handler's
    workspace_coords_complete check."""
    from nexus_deploy.service_env import ServiceEnvResult, ServiceRenderResult

    fake_result = ServiceEnvResult(
        services=(ServiceRenderResult(service="postgres", status="rendered"),),
    )
    monkeypatch.setattr(
        "nexus_deploy.orchestrator._service_env.render_all_env_files",
        lambda *_a, **_kw: fake_result,
    )

    append_called = False

    def _spy_append(*_a: Any, **_kw: Any) -> tuple[str, ...]:
        nonlocal append_called
        append_called = True
        return ()

    monkeypatch.setattr(
        "nexus_deploy.orchestrator._service_env.append_gitea_workspace_block",
        _spy_append,
    )

    # Set repo_owner + repo_name (the original lax check would've
    # passed) but leave gitea_user_password=None — the new full check
    # must catch this.
    orchestrator.bootstrap_env = type(orchestrator.bootstrap_env)(
        domain="example.com",
        admin_email="admin@example.com",
        gitea_repo_owner="owner",
        gitea_user_email="ops@example.com",
    )
    orchestrator.gitea_user_username = "ops"
    orchestrator.gitea_user_password = None  # missing — must skip the append
    orchestrator.project_root = tmp_path

    result = orchestrator._phase_service_env()
    assert result.status == "ok"
    assert "gitea_appended=0" in result.detail
    assert append_called is False, (
        "append_gitea_workspace_block must NOT be called when any coord is empty"
    )


# ---------------------------------------------------------------------------
# Phase 4a — `nexus-deploy run-pre-bootstrap` CLI handler tests.
# ---------------------------------------------------------------------------


def _setup_pre_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the 6 required env vars for run-pre-bootstrap."""
    for var, val in (
        ("ADMIN_EMAIL", "admin@example.com"),
        ("REPO_NAME", "nexus-example-com-gitea"),
        ("GITEA_REPO_OWNER", "admin"),
        ("ENABLED_SERVICES", "gitea,kestra"),
        ("DOMAIN", "example.com"),
        ("INFISICAL_PASS", "pw"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")


def test_cli_run_pre_bootstrap_unknown_arg_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nexus_deploy.__main__ import _run_pre_bootstrap

    rc = _run_pre_bootstrap(["--unknown"])
    assert rc == 2
    assert "unknown args" in capsys.readouterr().err


def test_cli_run_pre_bootstrap_missing_env_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _run_pre_bootstrap

    # ADMIN_EMAIL deliberately missing; set the rest.
    for var, val in (
        ("REPO_NAME", "r"),
        ("GITEA_REPO_OWNER", "o"),
        ("ENABLED_SERVICES", "gitea"),
        ("DOMAIN", "example.com"),
        ("INFISICAL_PASS", "pw"),
    ):
        monkeypatch.setenv(var, val)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")
    rc = _run_pre_bootstrap([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing required env" in err
    assert "ADMIN_EMAIL" in err


def test_cli_run_pre_bootstrap_rc0_emits_credentials_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Happy path: orchestrator returns ok with populated creds → rc=0
    AND INFISICAL_TOKEN + PROJECT_ID on stdout (eval-able)."""
    from nexus_deploy.__main__ import _run_pre_bootstrap

    _setup_pre_bootstrap_env(monkeypatch)
    fake_result = OrchestratorResult(
        phases=(PhaseResult("p1", "ok"),),
        state=OrchestratorState(
            infisical_token="real-token",
            project_id="proj-real",
        ),
    )
    with patch.object(Orchestrator, "run_pre_bootstrap", return_value=fake_result):
        rc = _run_pre_bootstrap([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "INFISICAL_TOKEN=real-token" in out
    assert "PROJECT_ID=proj-real" in out


def test_cli_run_pre_bootstrap_rc1_on_partial_emits_empty_creds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Partial: e.g. infisical-provision dropped creds → rc=1 with
    empty INFISICAL_TOKEN + PROJECT_ID (eval clears stale values)."""
    from nexus_deploy.__main__ import _run_pre_bootstrap

    _setup_pre_bootstrap_env(monkeypatch)
    fake_result = OrchestratorResult(
        phases=(PhaseResult("infisical-provision", "partial"),),
        state=OrchestratorState(),  # empty token/project_id
    )
    with patch.object(Orchestrator, "run_pre_bootstrap", return_value=fake_result):
        rc = _run_pre_bootstrap([])
    assert rc == 1
    out = capsys.readouterr().out
    # shlex.quote of empty string → ''
    assert "INFISICAL_TOKEN=''" in out
    assert "PROJECT_ID=''" in out


def test_cli_run_pre_bootstrap_rc2_on_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_deploy.__main__ import _run_pre_bootstrap

    _setup_pre_bootstrap_env(monkeypatch)
    fake_result = OrchestratorResult(
        phases=(PhaseResult("stack-sync", "failed"),),
        state=OrchestratorState(),
    )
    with patch.object(Orchestrator, "run_pre_bootstrap", return_value=fake_result):
        rc = _run_pre_bootstrap([])
    assert rc == 2


def test_cli_run_pre_bootstrap_transport_failure_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SSH/transport failure mid-run → rc=2 with stderr explanation."""
    from nexus_deploy.__main__ import _run_pre_bootstrap

    _setup_pre_bootstrap_env(monkeypatch)

    def _raises(self: Any) -> Any:
        import subprocess

        raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=30)

    with patch.object(Orchestrator, "run_pre_bootstrap", _raises):
        rc = _run_pre_bootstrap([])
    assert rc == 2
    assert "transport failure" in capsys.readouterr().err
