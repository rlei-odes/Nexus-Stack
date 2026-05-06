"""Top-level orchestrator (Phase 3 Modul 3.4b, #505).

Replaces the bash eval-handoff dance in ``scripts/deploy.sh`` with
a single Python entrypoint that calls all already-migrated module
functions in sequence and handles state-passing in-process. Three
separate ``mktemp`` + ``eval`` rituals (gitea-configure, woodpecker-
oauth, mirror-setup) collapse into one ``OrchestratorState`` object
mutated as phases run; only three values still need to escape back
to bash for the surviving non-migrated logic (compose-restart loop
+ Woodpecker .env write):

* ``RESTART_SERVICES``  — bash compose-restart loop reads this
* ``WOODPECKER_GITEA_CLIENT`` — written into stacks/woodpecker/.env
* ``WOODPECKER_GITEA_SECRET`` — written into stacks/woodpecker/.env

Other state (``GITEA_TOKEN``, ``FORK_NAME``, ``FORK_OWNER``) is
consumed entirely inside the orchestrator and never exits Python.

Phase order (deterministic, mirrors deploy.sh):

1. infisical bootstrap            (push all secret folders to Infisical)
2. services configure             (REST + exec admin-setup hooks)
3. gitea configure                (admin/user create+sync, repo, token)
4. seed                           (push examples/workspace-seeds/ to repo)
5. kestra register-system-flows   (system.git-sync + flow-sync)
6. gitea woodpecker-oauth         (provision OAuth app for Woodpecker CI)
7. gitea mirror-setup             (per-mirror migrate + fork; if mirrors)
8. secret-sync jupyter            (Infisical → Jupyter .infisical.env)
9. secret-sync marimo             (Infisical → Marimo .infisical.env)

Each phase produces a :class:`PhaseResult`. A phase with status="failed"
aborts the orchestrator (early exit, downstream phases skipped). A
phase with status="partial" continues — operator gets a yellow
warning, downstream phases still run. Same rc=0/1/2 dispatch as
all other migrated CLIs.

``contextlib.ExitStack`` manages the SSH client lifetime in
:meth:`Orchestrator.run_all`. Each phase that needs an HTTP
port-forward to the nexus server opens it inside its own method
via a local ``with ssh.port_forward(...)`` block so the tunnel
is torn down before the phase returns — the ExitStack is not
passed into phases.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from nexus_deploy import compose_runner as _compose_runner
from nexus_deploy import firewall as _firewall
from nexus_deploy import gitea as _gitea
from nexus_deploy import infisical as _infisical
from nexus_deploy import kestra as _kestra
from nexus_deploy import secret_sync as _secret_sync
from nexus_deploy import seeder as _seeder
from nexus_deploy import service_env as _service_env
from nexus_deploy import services as _services
from nexus_deploy import stack_sync as _stack_sync
from nexus_deploy.config import NexusConfig
from nexus_deploy.infisical import BootstrapEnv
from nexus_deploy.ssh import SSHClient


@dataclass
class OrchestratorState:
    """Mutable state populated as phases run.

    Replaces the bash eval-tempfile-handoff pattern with in-process
    Python attributes. Each phase reads what it needs and writes
    its outputs.

    The ``restart_services`` + ``woodpecker_*`` fields are
    additionally emitted to stdout at the end so the surviving
    bash logic in deploy.sh can consume them. ``gitea_token`` /
    ``fork_*`` stay in-process.
    """

    gitea_token: str | None = None
    restart_services: tuple[str, ...] = ()
    woodpecker_client_id: str | None = None
    woodpecker_client_secret: str | None = None
    fork_name: str | None = None
    fork_owner: str | None = None
    # Phase 4a (#505): infisical_token + project_id are now POPULATED by
    # _phase_infisical_provision (running BEFORE _phase_infisical_bootstrap)
    # instead of being inherited as orchestrator inputs. Pre-bootstrap
    # callers don't need to pass them in the constructor.
    #
    # Note: post-bootstrap phases currently gate on the
    # ``self.infisical_token`` / ``self.project_id`` orchestrator fields,
    # not on these state mirrors. ``_phase_infisical_provision`` writes
    # to BOTH (state + self.field) so a single full-pipeline run sees
    # the values everywhere. State stays as the canonical record for
    # the CLI's stdout emission. Caught in PR #532 R2 #5.
    infisical_token: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class PhaseResult:
    """Outcome of a single phase. Same shape as the per-module
    Result dataclasses (RsyncResult, OAuthAppResult, etc.)."""

    name: str
    status: Literal["ok", "partial", "failed", "skipped"]
    detail: str = ""


@dataclass(frozen=True)
class OrchestratorResult:
    """Return value from :meth:`Orchestrator.run_all`."""

    phases: tuple[PhaseResult, ...]
    state: OrchestratorState

    @property
    def is_success(self) -> bool:
        """All phases ok or skipped (no failed, no partial)."""
        return all(p.status in ("ok", "skipped") for p in self.phases)

    @property
    def has_partial(self) -> bool:
        """At least one phase produced status='partial' (yellow warn)."""
        return any(p.status == "partial" for p in self.phases)

    @property
    def has_hard_failure(self) -> bool:
        return any(p.status == "failed" for p in self.phases)


def _allocate_free_port() -> int:
    """Same primitive as :func:`__main__._allocate_free_port`. Inlined
    here so orchestrator.py doesn't depend on __main__."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@dataclass
class Orchestrator:
    """Top-level pipeline runner.

    Construct with the per-deploy inputs, then call :meth:`run_all`.
    State accumulates across phases on ``self.state``.
    """

    config: NexusConfig
    bootstrap_env: BootstrapEnv
    enabled_services: list[str]
    repo_name: str
    gitea_repo_owner: str
    workspace_branch: str = "main"
    gh_mirror_repos: list[str] = field(default_factory=list)
    gh_mirror_token: str | None = None
    gitea_user_username: str | None = None
    gitea_user_email: str | None = None
    gitea_user_password: str | None = None
    ssh_host: str = "nexus"
    project_id: str | None = None
    infisical_token: str | None = None
    infisical_env: str = "dev"

    # Phase 4a (#505) — additions for the pre-bootstrap pipeline.
    # All optional to keep back-compat with existing post-bootstrap-only
    # callers (run_all). When unset, phases that need them surface as
    # status='skipped' with a clear detail.
    cf_client_id: str | None = None  # Cloudflare Access Service Token id
    cf_client_secret: str | None = None  # Cloudflare Access Service Token secret
    persistent_volume_id: str = "0"  # Hetzner Cloud volume id; "0" = no volume
    # Repository checkout root on the runner — phases derive
    # ``project_root / "stacks"`` for per-service compose/.env paths.
    # Renamed from ``stacks_dir`` in PR #532 R2 #1: deploy.sh defines
    # ``STACKS_DIR=$PROJECT_ROOT/stacks`` (the actual stacks dir), so
    # wiring that env var into a field of the same name and then
    # appending ``/"stacks"`` produced a broken ``.../stacks/stacks/...``
    # path. The CLI handler now reads ``PROJECT_ROOT`` instead.
    project_root: Path = field(default_factory=lambda: Path.cwd())
    firewall_json: str = "{}"  # raw `tofu output -json firewall_rules` body
    domain: str = ""  # for firewall RedPanda rendering
    admin_password_infisical: str | None = None  # for infisical provision-admin

    state: OrchestratorState = field(default_factory=OrchestratorState)
    results: list[PhaseResult] = field(default_factory=list)

    def run_all(self) -> OrchestratorResult:
        """Execute all phases in deterministic order.

        ExitStack ensures any opened ssh-tunnels / temp-files clean
        up before return, even on early-fail. A phase with
        status='failed' aborts the run; status='partial' continues
        with a recorded warning.

        Resets ``self.results`` so re-invoking the same instance does
        not duplicate prior phase outputs. ``self.state`` is left as-is
        (production callers create a fresh ``Orchestrator`` per run;
        tests may pre-seed state to skip earlier phases).
        """
        self.results = []
        with contextlib.ExitStack() as stack:
            ssh = stack.enter_context(SSHClient(self.ssh_host))
            phases: list[Callable[[SSHClient], PhaseResult]] = [
                self._phase_infisical_bootstrap,
                self._phase_services_configure,
                self._phase_gitea_configure,
                self._phase_seed,
                self._phase_kestra_register,
                self._phase_woodpecker_oauth,
                self._phase_mirror_setup,
                self._phase_secret_sync_jupyter,
                self._phase_secret_sync_marimo,
            ]
            for phase in phases:
                result = phase(ssh)
                self.results.append(result)
                if result.status == "failed":
                    break
        return OrchestratorResult(phases=tuple(self.results), state=self.state)

    # -----------------------------------------------------------------
    # Phase methods. Each calls into the existing migrated module's
    # public function with the right slice of state; failures are
    # caught and converted into PhaseResult instead of propagating
    # so the orchestrator decides whether to abort or continue.
    # -----------------------------------------------------------------

    def _phase_infisical_bootstrap(self, ssh: SSHClient) -> PhaseResult:
        """Push secrets to Infisical via :func:`infisical.compute_folders`
        + :meth:`InfisicalClient.bootstrap`. Reads
        ``self.config`` + ``self.bootstrap_env``; needs project_id +
        infisical_token from env (set up by the CLI handler)."""
        if not self.project_id or not self.infisical_token:
            return PhaseResult(
                name="infisical-bootstrap",
                status="skipped",
                detail="PROJECT_ID or INFISICAL_TOKEN missing",
            )
        try:
            client = _infisical.InfisicalClient(
                project_id=self.project_id,
                env=self.infisical_env,
                token=self.infisical_token,
                push_dir=Path("/tmp/infisical-push"),  # noqa: S108
            )
            folders = _infisical.compute_folders(self.config, self.bootstrap_env)
            result = client.bootstrap(folders)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="infisical-bootstrap",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="infisical-bootstrap",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if result.failed > 0:
            return PhaseResult(
                name="infisical-bootstrap",
                status="partial",
                detail=f"built={result.folders_built} pushed={result.pushed} failed={result.failed}",
            )
        return PhaseResult(
            name="infisical-bootstrap",
            status="ok",
            detail=f"built={result.folders_built} pushed={result.pushed}",
        )

    def _phase_services_configure(self, ssh: SSHClient) -> PhaseResult:
        """REST + exec admin-setup hooks via
        :func:`services.run_admin_setups`."""
        try:
            result = _services.run_admin_setups(
                self.config,
                self.bootstrap_env,
                self.enabled_services,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="services-configure",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="services-configure",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if result.failed > 0:
            return PhaseResult(
                name="services-configure",
                status="partial",
                detail=(
                    f"configured={result.configured} already-configured={result.already_configured} "
                    f"skipped-not-ready={result.skipped_not_ready} failed={result.failed}"
                ),
            )
        return PhaseResult(
            name="services-configure",
            status="ok",
            detail=(
                f"configured={result.configured} already-configured={result.already_configured} "
                f"skipped-not-ready={result.skipped_not_ready}"
            ),
        )

    def _phase_gitea_configure(self, ssh: SSHClient) -> PhaseResult:
        """Synchronous Gitea configure via :func:`gitea.run_configure_gitea`.
        Populates ``state.gitea_token`` + ``state.restart_services``."""
        if "gitea" not in self.enabled_services:
            return PhaseResult(name="gitea-configure", status="skipped", detail="gitea not enabled")
        if not self.config.gitea_admin_password:
            return PhaseResult(
                name="gitea-configure",
                status="partial",
                detail="GITEA_ADMIN_PASS missing — basic-auth would 401",
            )
        local_port = _allocate_free_port()
        try:
            with ssh.port_forward(local_port, "localhost", 3200) as port:
                result = _gitea.run_configure_gitea(
                    self.config,
                    base_url=f"http://localhost:{port}",
                    ssh=ssh,
                    admin_email=self.bootstrap_env.admin_email or "",
                    gitea_user_email=self.gitea_user_email,
                    gitea_user_password=self.gitea_user_password,
                    repo_name=self.repo_name,
                    gitea_repo_owner=self.gitea_repo_owner,
                    is_mirror_mode=bool(self.gh_mirror_repos),
                    enabled_services=self.enabled_services,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="gitea-configure",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="gitea-configure",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        # Populate state — token may be None on partial mint failure.
        self.state.gitea_token = result.token
        self.state.restart_services = tuple(result.restart_services)
        if not result.is_success:
            return PhaseResult(
                name="gitea-configure",
                status="partial",
                detail="some sub-step failed (see stderr)",
            )
        return PhaseResult(name="gitea-configure", status="ok")

    def _phase_seed(self, ssh: SSHClient) -> PhaseResult:
        """Push examples/workspace-seeds/ to the workspace repo via
        :func:`seeder.run_seed_for_repo`. Needs ``state.gitea_token``;
        in mirror mode the target repo is the user's fork (set by
        the mirror phase if it ran first — but in the deterministic
        order, mirror runs AFTER seed, so seed always uses the
        non-mirror repo. Mirror's own seed_workspace_files re-run is
        deferred to the surviving deploy.sh bash for now)."""
        if not self.state.gitea_token:
            return PhaseResult(
                name="seed",
                status="skipped",
                detail="no gitea_token (gitea phase did not produce one)",
            )
        seeds_root = Path("examples/workspace-seeds")
        if not seeds_root.is_dir():
            return PhaseResult(
                name="seed",
                status="skipped",
                detail="examples/workspace-seeds/ missing",
            )
        try:
            result = _seeder.run_seed_for_repo(
                repo_owner=self.gitea_repo_owner,
                repo_name=self.repo_name,
                root=seeds_root,
                token=self.state.gitea_token,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="seed",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="seed",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if result.failed > 0:
            if result.created + result.skipped == 0:
                return PhaseResult(
                    name="seed",
                    status="failed",
                    detail=f"created=0 skipped=0 failed={result.failed}",
                )
            return PhaseResult(
                name="seed",
                status="partial",
                detail=f"created={result.created} skipped={result.skipped} failed={result.failed}",
            )
        return PhaseResult(
            name="seed",
            status="ok",
            detail=f"created={result.created} skipped={result.skipped}",
        )

    def _phase_kestra_register(self, ssh: SSHClient) -> PhaseResult:
        """Register system.git-sync + system.flow-sync via
        :func:`kestra.run_register_system_flows`. Port-forwards to
        kestra's container (8085 host → 8080 inside)."""
        if "kestra" not in self.enabled_services:
            return PhaseResult(
                name="kestra-register", status="skipped", detail="kestra not enabled"
            )
        if not self.config.kestra_admin_password:
            return PhaseResult(
                name="kestra-register",
                status="partial",
                detail="KESTRA_PASS missing — basic-auth would 401",
            )
        admin_email = self.bootstrap_env.admin_email or ""
        if not admin_email:
            return PhaseResult(
                name="kestra-register",
                status="partial",
                detail="ADMIN_EMAIL missing",
            )
        local_port = _allocate_free_port()
        try:
            with ssh.port_forward(local_port, "localhost", 8085) as port:
                result = _kestra.run_register_system_flows(
                    self.config,
                    base_url=f"http://localhost:{port}",
                    repo_owner=self.gitea_repo_owner,
                    repo_name=self.repo_name,
                    branch=self.workspace_branch,
                    admin_email=admin_email,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="kestra-register",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="kestra-register",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if not result.is_success:
            return PhaseResult(
                name="kestra-register",
                status="partial",
                detail=f"execution={result.execution_state or 'skipped'}",
            )
        return PhaseResult(
            name="kestra-register",
            status="ok",
            detail=f"flows={len(result.flows)} execution={result.execution_state or 'skipped'}",
        )

    def _phase_woodpecker_oauth(self, ssh: SSHClient) -> PhaseResult:
        """Provision Woodpecker OAuth via
        :func:`gitea.run_woodpecker_oauth_setup`. Populates
        ``state.woodpecker_client_id`` + ``state.woodpecker_client_secret``."""
        if "woodpecker" not in self.enabled_services:
            return PhaseResult(
                name="woodpecker-oauth", status="skipped", detail="woodpecker not enabled"
            )
        if not self.state.gitea_token:
            return PhaseResult(
                name="woodpecker-oauth",
                status="skipped",
                detail="no gitea_token from prior phase",
            )
        domain = self.bootstrap_env.domain or ""
        if not domain:
            return PhaseResult(
                name="woodpecker-oauth",
                status="partial",
                detail="DOMAIN missing",
            )
        local_port = _allocate_free_port()
        try:
            with ssh.port_forward(local_port, "localhost", 3200) as port:
                result, error, rotation_started = _gitea.run_woodpecker_oauth_setup(
                    base_url=f"http://localhost:{port}",
                    domain=domain,
                    gitea_token=self.state.gitea_token,
                    admin_username=self.config.admin_username or "admin",
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="woodpecker-oauth",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except _gitea.GiteaError as exc:
            return PhaseResult(
                name="woodpecker-oauth",
                status="failed",
                detail=str(exc),
            )
        except Exception as exc:
            return PhaseResult(
                name="woodpecker-oauth",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if result is None:
            # Half-completed rotation = abort (delete invalidated old creds).
            if rotation_started:
                return PhaseResult(
                    name="woodpecker-oauth",
                    status="failed",
                    detail=f"rotation half-complete: {error}",
                )
            return PhaseResult(
                name="woodpecker-oauth",
                status="partial",
                detail=error or "create failed (no rotation started)",
            )
        self.state.woodpecker_client_id = result.client_id
        self.state.woodpecker_client_secret = result.client_secret
        return PhaseResult(name="woodpecker-oauth", status="ok", detail="created")

    def _phase_mirror_setup(self, ssh: SSHClient) -> PhaseResult:
        """Mirror-mode provisioning via :func:`gitea.run_mirror_setup`.
        Populates ``state.fork_name`` + ``state.fork_owner`` if a fork
        was created. Skipped when no GH_MIRROR_REPOS configured."""
        if not self.gh_mirror_repos:
            return PhaseResult(
                name="mirror-setup", status="skipped", detail="no mirrors configured"
            )
        if not self.state.gitea_token:
            return PhaseResult(
                name="mirror-setup",
                status="skipped",
                detail="no gitea_token from prior phase",
            )
        if not self.gh_mirror_token:
            return PhaseResult(
                name="mirror-setup",
                status="partial",
                detail="GH_MIRROR_TOKEN missing",
            )
        if self.gitea_user_username and not self.config.gitea_admin_password:
            return PhaseResult(
                name="mirror-setup",
                status="partial",
                detail="GITEA_ADMIN_PASS required for fork-mode mirror",
            )
        local_port = _allocate_free_port()
        try:
            with ssh.port_forward(local_port, "localhost", 3200) as port:
                result = _gitea.run_mirror_setup(
                    base_url=f"http://localhost:{port}",
                    admin_username=self.config.admin_username or "admin",
                    admin_password=self.config.gitea_admin_password or "",
                    gitea_token=self.state.gitea_token,
                    gitea_user_username=self.gitea_user_username,
                    gh_mirror_repos=self.gh_mirror_repos,
                    gh_mirror_token=self.gh_mirror_token,
                    workspace_branch=self.workspace_branch,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="mirror-setup",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except _gitea.GiteaError as exc:
            return PhaseResult(name="mirror-setup", status="failed", detail=str(exc))
        except Exception as exc:
            return PhaseResult(
                name="mirror-setup",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if result.fork is not None and result.fork.status in ("created", "already_exists"):
            self.state.fork_name = result.fork.name
            self.state.fork_owner = result.fork.owner
        if not result.is_success:
            return PhaseResult(
                name="mirror-setup",
                status="partial",
                detail=f"mirrors={len(result.mirrors)} (some failed)",
            )
        return PhaseResult(
            name="mirror-setup", status="ok", detail=f"mirrors={len(result.mirrors)}"
        )

    def _phase_secret_sync(self, ssh: SSHClient, stack: str) -> PhaseResult:
        """Common impl for jupyter + marimo secret-sync."""
        if stack not in self.enabled_services:
            return PhaseResult(
                name=f"secret-sync-{stack}",
                status="skipped",
                detail=f"{stack} not enabled",
            )
        if not self.project_id or not self.infisical_token:
            return PhaseResult(
                name=f"secret-sync-{stack}",
                status="partial",
                detail="PROJECT_ID or INFISICAL_TOKEN missing",
            )
        target = _secret_sync.StackTarget(name=stack)
        try:
            result = _secret_sync.run_sync_for_stack(
                target,
                project_id=self.project_id,
                infisical_token=self.infisical_token,
                infisical_env=self.infisical_env,
                gitea_token=self.state.gitea_token or "",
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name=f"secret-sync-{stack}",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name=f"secret-sync-{stack}",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if not result.wrote and result.failed_folders == 0 and result.succeeded_folders == 0:
            return PhaseResult(
                name=f"secret-sync-{stack}",
                status="partial",
                detail="no usable result (see prior warnings)",
            )
        if result.wrote and result.failed_folders > 0:
            return PhaseResult(
                name=f"secret-sync-{stack}",
                status="partial",
                detail=f"pushed={result.pushed} failed_folders={result.failed_folders}",
            )
        if not result.wrote:
            return PhaseResult(
                name=f"secret-sync-{stack}",
                status="ok",
                detail="kept previous (outage gate)",
            )
        return PhaseResult(
            name=f"secret-sync-{stack}", status="ok", detail=f"pushed={result.pushed}"
        )

    def _phase_secret_sync_jupyter(self, ssh: SSHClient) -> PhaseResult:
        return self._phase_secret_sync(ssh, "jupyter")

    def _phase_secret_sync_marimo(self, ssh: SSHClient) -> PhaseResult:
        return self._phase_secret_sync(ssh, "marimo")

    # ---------------------------------------------------------------------
    # Phase 4a (#505) — pre-bootstrap pipeline.
    #
    # These phases run BEFORE the existing infisical-bootstrap phase and
    # collectively replace deploy.sh's [0/7]-[7/7] CLI invocations + the
    # interleaved bash glue. Each wraps an already-migrated module's
    # public function and converts its result/exception into a PhaseResult.
    # No new logic — just a unified place to chain them with state-handoff.
    #
    # The pre-bootstrap phases come in two clusters:
    #
    # 1. **Local-only** (don't need an SSHClient): service-env render,
    #    firewall override generation.
    #
    # 2. **Server-side** (use the wrapped helper's own ssh.run_script
    #    plumbing): stack-sync, compose up, infisical provision-admin.
    #    These don't need the orchestrator's shared SSHClient because
    #    their helper functions invoke ``ssh <host>`` via subprocess
    #    independently — they accept ``host=self.ssh_host`` so a
    #    non-default ``SSH_HOST_ALIAS`` reaches every phase uniformly
    #    (caught in PR #532 R2 #2).
    #
    # Both clusters' phase methods take no parameters (no shared
    # SSHClient, no ssh arg) — the run_pre_bootstrap loop calls them as
    # ``phase()``. The previous design passed a None-cast-as-SSHClient
    # for signature consistency with the existing phases; that was a
    # runtime-contract footgun and was removed in PR #532 R1 #2.
    # ---------------------------------------------------------------------

    def _phase_service_env(self) -> PhaseResult:
        """Render per-service ``stacks/<svc>/.env`` files locally + (when
        Gitea is enabled) append the Gitea workspace block to the
        Gitea-integrated stacks. Replaces deploy.sh's
        ``service-env --enabled`` invocation (Modul 3.4c, #527).

        Local-only — no SSH context needed. Pre-bootstrap phases drop
        the ``ssh`` arg from their signature (caught in PR #532 R1 #2:
        passing a None-cast-as-SSHClient is a runtime-contract footgun
        even if the current body uses ``del ssh``).
        """
        try:
            result = _service_env.render_all_env_files(
                self.config,
                self.bootstrap_env,
                self.enabled_services,
                stacks_dir=self.project_root / "stacks",
            )
        except _service_env.ServiceEnvError as exc:
            # Hard-fail conditions (e.g. SFTPGo with empty password) —
            # the legacy bash exits 1 with a red banner; the Python
            # equivalent raises so we surface it here.
            return PhaseResult(
                name="service-env",
                status="failed",
                detail=str(exc),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="service-env",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="service-env",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )

        # Optionally append the Gitea workspace block. Mirrors the CLI
        # handler's `workspace_coords_complete` check exactly — ALL six
        # coords (repo_url, username, password, author_name, author_email,
        # repo_name) must be non-empty, otherwise we'd write a
        # broken Gitea block (empty PASSWORD/AUTHOR fields) that's
        # harder to diagnose than a missing block. Caught in PR #532
        # R1 #3.
        gitea_appended_count = 0
        gitea_user_email_value = self.gitea_user_email or self.bootstrap_env.gitea_user_email
        # Single source of truth: self.gitea_repo_owner (required
        # constructor field). The bootstrap_env mirror exists for the
        # in-script seeder/secret-sync path but should NOT diverge from
        # the orchestrator's own field. Caught in PR #532 R3 #1.
        workspace_coords_complete = all(
            (
                self.gitea_repo_owner,
                self.repo_name,
                self.gitea_user_username,
                self.gitea_user_password,
                gitea_user_email_value,
            ),
        )
        if "gitea" in self.enabled_services and workspace_coords_complete:
            gitea_repo_url = f"http://gitea:3000/{self.gitea_repo_owner}/{self.repo_name}.git"
            try:
                cfg = _service_env.GiteaWorkspaceConfig(
                    gitea_repo_url=gitea_repo_url,
                    gitea_username=self.gitea_user_username or "",
                    gitea_password=self.gitea_user_password or "",
                    git_author_name=self.gitea_user_username or "",
                    git_author_email=gitea_user_email_value or "",
                    repo_name=self.repo_name,
                    workspace_branch=self.workspace_branch,
                )
                appended = _service_env.append_gitea_workspace_block(
                    cfg,
                    self.enabled_services,
                    stacks_dir=self.project_root / "stacks",
                )
                gitea_appended_count = len(appended)
            except OSError as exc:
                return PhaseResult(
                    name="service-env",
                    status="partial",
                    detail=(
                        f"rendered={result.rendered} but gitea-block append "
                        f"failed: {type(exc).__name__}"
                    ),
                )

        if result.failed > 0:
            if result.rendered == 0:
                return PhaseResult(
                    name="service-env",
                    status="failed",
                    detail=f"rendered=0 failed={result.failed}",
                )
            return PhaseResult(
                name="service-env",
                status="partial",
                detail=(
                    f"rendered={result.rendered} skipped={result.skipped} "
                    f"failed={result.failed} gitea_appended={gitea_appended_count}"
                ),
            )
        return PhaseResult(
            name="service-env",
            status="ok",
            detail=(
                f"rendered={result.rendered} skipped={result.skipped} "
                f"gitea_appended={gitea_appended_count}"
            ),
        )

    def _phase_stack_sync(self) -> PhaseResult:
        """Rsync each enabled stack to ``/opt/docker-server/stacks/<svc>/``
        and clean up disabled stack directories on the server. Replaces
        deploy.sh's ``stack-sync --enabled`` invocation (Modul 3.3, #523).

        Uses ``run_stack_sync`` which manages its own rsync subprocess
        + cleanup ssh.run_script invocation independently — the
        orchestrator's shared SSHClient is not consumed here, so the
        signature drops the ``ssh`` arg.
        """
        try:
            result = _stack_sync.run_stack_sync(
                self.project_root / "stacks",
                self.enabled_services,
                host=self.ssh_host,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="stack-sync",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="stack-sync",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        cleanup_failed = result.cleanup.failed if result.cleanup is not None else 0
        cleanup_removed = result.cleanup.removed if result.cleanup is not None else 0
        cleanup_missing_or_unparseable = result.cleanup is None
        if cleanup_missing_or_unparseable:
            # No parseable RESULT from the cleanup script — same hard-fail
            # contract as the per-CLI handler maps to rc=2.
            return PhaseResult(
                name="stack-sync",
                status="failed",
                detail=(
                    f"rsync_synced={result.synced} rsync_failed={result.failed_rsync} "
                    "cleanup script produced no parseable RESULT"
                ),
            )
        if result.failed_rsync > 0 or cleanup_failed > 0:
            return PhaseResult(
                name="stack-sync",
                status="partial",
                detail=(
                    f"rsync_synced={result.synced} rsync_failed={result.failed_rsync} "
                    f"cleanup_removed={cleanup_removed} cleanup_failed={cleanup_failed}"
                ),
            )
        return PhaseResult(
            name="stack-sync",
            status="ok",
            detail=(f"rsync_synced={result.synced} cleanup_removed={cleanup_removed}"),
        )

    def _phase_firewall_configure(self) -> PhaseResult:
        """Generate per-service ``docker-compose.firewall.yml`` overrides
        and (when RedPanda has firewall ports) the dual-listener override
        + substituted ``redpanda-firewall.yaml``. Replaces deploy.sh's
        ``firewall configure --domain`` invocation (Modul 3.4e, #531).

        Local-only — the rendered files are written to ``stacks/<svc>/``
        on the runner. The subsequent server-side scp + orphan-cleanup
        loop stays in deploy.sh for now (Phase 4b will migrate it).
        """
        try:
            gen, write = _firewall.configure(
                firewall_json=self.firewall_json,
                stacks_dir=self.project_root,
                domain=self.domain,
            )
        except (ValueError, FileNotFoundError) as exc:
            return PhaseResult(
                name="firewall-configure",
                status="failed",
                detail=str(exc),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="firewall-configure",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="firewall-configure",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )

        if gen.zero_entry:
            if write.failed:
                return PhaseResult(
                    name="firewall-configure",
                    status="partial",
                    detail=(
                        f"zero-entry mode but stale-cleanup had {len(write.failed)} failure(s)"
                    ),
                )
            return PhaseResult(
                name="firewall-configure",
                status="ok",
                detail="zero-entry (no rules)",
            )

        if gen.skipped:
            # Per #531 R7 #4: skipped services mean Tofu requested a rule
            # but compose.yml was unparseable → existing override stays
            # in place but state may be inconsistent with Tofu.
            return PhaseResult(
                name="firewall-configure",
                status="partial",
                detail=(
                    f"rendered={len(gen.compiled)} skipped={len(gen.skipped)} "
                    f"redpanda={'yes' if gen.redpanda else 'no'}"
                ),
            )
        if write.failed:
            return PhaseResult(
                name="firewall-configure",
                status="partial",
                detail=(
                    f"rendered={len(gen.compiled)} written={len(write.written)} "
                    f"failed={len(write.failed)}"
                ),
            )
        return PhaseResult(
            name="firewall-configure",
            status="ok",
            detail=(f"rendered={len(gen.compiled)} redpanda={'yes' if gen.redpanda else 'no'}"),
        )

    def _phase_compose_up(self) -> PhaseResult:
        """Start containers in parallel via
        :func:`compose_runner.run_compose_up`. Replaces deploy.sh's
        ``compose up --enabled`` invocation (Modul 2.2a, #505).

        ``run_compose_up`` invokes ``ssh nexus 'bash -s'`` via
        subprocess internally — the orchestrator's shared SSHClient
        is not consumed here, so the signature drops the ``ssh`` arg.
        """
        try:
            result = _compose_runner.run_compose_up(
                self.enabled_services,
                host=self.ssh_host,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="compose-up",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="compose-up",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )
        if result.failed > 0:
            return PhaseResult(
                name="compose-up",
                status="partial",
                detail=f"started={result.started} failed={result.failed}",
            )
        return PhaseResult(
            name="compose-up",
            status="ok",
            detail=f"started={result.started}",
        )

    def _phase_infisical_provision(self) -> PhaseResult:
        """Bootstrap the Infisical admin + workspace. Replaces deploy.sh's
        ``infisical provision-admin`` invocation (Modul 3.4f, #530).

        Populates ``self.state.infisical_token`` and ``self.state.project_id``
        on success — those values are then consumed by the existing
        ``_phase_infisical_bootstrap`` phase. Read the constructor's
        ``project_id`` / ``infisical_token`` fields as fallback (so a
        post-bootstrap-only test can still bypass this phase).

        ``provision_admin`` manages its own ssh.run_script call —
        no shared SSHClient context, so the signature drops ``ssh``.
        """
        admin_email = self.bootstrap_env.admin_email or ""
        admin_password = self.admin_password_infisical or ""
        if not admin_email or not admin_password:
            return PhaseResult(
                name="infisical-provision",
                status="skipped",
                detail="ADMIN_EMAIL or admin_password_infisical missing",
            )
        try:
            result = _infisical.provision_admin(
                admin_email=admin_email,
                admin_password=admin_password,
                host=self.ssh_host,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            return PhaseResult(
                name="infisical-provision",
                status="failed",
                detail=f"transport ({type(exc).__name__})",
            )
        except Exception as exc:
            return PhaseResult(
                name="infisical-provision",
                status="failed",
                detail=f"unexpected ({type(exc).__name__})",
            )

        if not result.has_credentials:
            # Same contract as #530 R2 #4: rc=1 (soft-fail) when the
            # provision didn't produce usable credentials. Pre-bootstrap
            # caller should treat this as 'continue without secret push'.
            return PhaseResult(
                name="infisical-provision",
                status="partial",
                detail=f"status={result.status} (no usable credentials)",
            )

        # Populate state for downstream phases.
        self.state.infisical_token = result.token
        self.state.project_id = result.project_id
        # Also populate the orchestrator's input fields so the existing
        # _phase_infisical_bootstrap (which reads self.project_id /
        # self.infisical_token) finds the values.
        self.project_id = result.project_id
        self.infisical_token = result.token

        return PhaseResult(
            name="infisical-provision",
            status="ok",
            detail=f"status={result.status}",
        )

    def run_pre_bootstrap(self) -> OrchestratorResult:
        """Run the pre-bootstrap pipeline: service-env → stack-sync →
        firewall-configure → compose-up → infisical-provision.

        Resets ``self.results`` AND the credentials on BOTH
        ``self.state`` (``infisical_token`` + ``project_id``) and the
        orchestrator's own ``self.infisical_token`` / ``self.project_id``
        fields, so a re-invocation on the same instance can't leak
        stale credentials from a prior run — neither into the current
        rc=1 stdout emission (state) nor into a downstream call to
        :meth:`run_all` whose post-bootstrap phases gate on the fields.
        Caught in PR #532 R1 #4 (state) and R2 #3 (fields). Other state
        slots (gitea_token / woodpecker_* / fork_*) stay because they're
        populated by post-bootstrap phases and a re-run would naturally
        re-set them.

        Failure of any phase aborts the run; partial-success phases
        continue. Same rc=0/1/2 dispatch contract as :meth:`run_all`.

        The phases here don't need the orchestrator's shared SSHClient
        — the wrapped helpers each manage their own subprocess / ssh
        invocations independently. That's why this method doesn't
        open an ExitStack-managed ssh context (in contrast to
        :meth:`run_all`), and why pre-bootstrap phases drop the ``ssh``
        arg from their signature (per PR #532 R1 #2 — passing a
        None-cast-as-SSHClient is a runtime-contract footgun).
        """
        self.results = []
        # Reset credentials on BOTH state mirrors and the orchestrator's
        # own fields so a re-run can't carry over stale token / project_id
        # from a previous invocation that produced rc=1. State guards the
        # rc=1 stdout emission; the self.fields guard the post-bootstrap
        # phases (infisical-bootstrap, secret-sync) when the same instance
        # is later passed to run_all. Caught in PR #532 R2 #3.
        self.state.infisical_token = None
        self.state.project_id = None
        self.infisical_token = None
        self.project_id = None
        phases: list[Callable[[], PhaseResult]] = [
            self._phase_service_env,
            self._phase_stack_sync,
            self._phase_firewall_configure,
            self._phase_compose_up,
            self._phase_infisical_provision,
        ]
        for phase in phases:
            result = phase()
            self.results.append(result)
            if result.status == "failed":
                break
        return OrchestratorResult(phases=tuple(self.results), state=self.state)


# Module-level helper so the CLI handler can shell out cleanly.
__all__ = [
    "Orchestrator",
    "OrchestratorResult",
    "OrchestratorState",
    "PhaseResult",
]
