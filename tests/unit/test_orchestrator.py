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

from typing import Any
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
        token_error=None,
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

    def make_phase(name: str, status: str = "ok") -> Any:
        def phase(_ssh: Any) -> PhaseResult:
            invoked.append(name)
            return PhaseResult(name=name, status=status)  # type: ignore[arg-type]

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

    def make_phase(name: str, status: str = "ok") -> Any:
        def phase(_ssh: Any) -> PhaseResult:
            invoked.append(name)
            return PhaseResult(name=name, status=status)  # type: ignore[arg-type]

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
    out = capsys.readouterr().out
    err = capsys.readouterr().err
    assert "SECRET-TOKEN-ABCDEF" not in out
    assert "SECRET-TOKEN-ABCDEF" not in err
    assert "GITEA_TOKEN" not in out
    assert "FORK_NAME" not in out
    assert "FORK_OWNER" not in out


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
