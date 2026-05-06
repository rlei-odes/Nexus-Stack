"""Top-level deploy pipeline (Phase 4c, #505).

Replaces ``scripts/deploy.sh`` entirely. The orchestrator's
``run_pre_bootstrap`` + ``run_all`` already cover the per-stack /
per-service phases; this module covers everything that previously
sat above and around them in deploy.sh:

1. R2 credentials load + ``os.environ`` injection
2. ``tofu state list`` pre-flight
3. config.tfvars parse + Gitea identity derivation
4. Read 7 tofu outputs (secrets, image_versions, enabled_services,
   firewall_rules, ssh_service_token, server_ip, persistent_volume_id)
5. SSH known_hosts cleanup (``ssh-keygen -R``)
6. ``setup.configure_ssh`` → ``setup.wait_for_ssh`` →
   ``setup.ensure_jq`` → ``setup.mount_persistent_volume``
7. Docker Hub login (when creds set)
8. ``setup.setup_wetty_ssh_agent`` (when wetty enabled)
9. ``Orchestrator.run_pre_bootstrap``
10. ``Orchestrator.run_all``
11. Display service URLs from ``tofu output service_urls``

Everything runs in-process — no subprocess CLI invocations of
``python -m nexus_deploy <subcommand>``, no ``eval`` of stdout
payloads. State flows through Python objects between steps.

The ``run_pipeline`` function is the public entry; the CLI handler
in ``__main__.py:_run_pipeline`` is a thin wrapper that reads
workflow-secret env vars and calls this.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from nexus_deploy import setup as _setup
from nexus_deploy import tfvars as _tfvars
from nexus_deploy import tofu as _tofu
from nexus_deploy.config import ConfigError, NexusConfig
from nexus_deploy.infisical import BootstrapEnv
from nexus_deploy.orchestrator import Orchestrator, OrchestratorResult
from nexus_deploy.ssh import SSHClient

# Cloudflare-Tunnel SSH endpoint; the legacy bash builds it as
# ``ssh.${DOMAIN}``. Same shape used by setup_ssh_config.
_SSH_HOST_DNS_TEMPLATE = "ssh.{domain}"


class PipelineError(Exception):
    """Pipeline pre-flight or step failed unrecoverably.

    Distinct from PhaseResult.status='failed' which the orchestrator
    uses for in-pipeline phase outcomes — this exception is raised by
    the wrapper code that runs BEFORE / AROUND the orchestrator
    (tofu reads, R2 creds, ssh setup) and SHOULD abort the deploy.
    """


@dataclass(frozen=True)
class PipelineResult:
    """Bundle of the orchestrator's two outcomes + the service URLs.

    Returned by :func:`run_pipeline` for the CLI handler to format
    the post-deploy banner. Tests assert against this directly
    instead of capturing stdout.
    """

    pre_bootstrap: OrchestratorResult
    run_all: OrchestratorResult
    service_urls: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineOptions:
    """Workflow-secret inputs the CLI handler reads from env vars.

    Bundled into a frozen dataclass so callers can construct
    deterministic test fixtures and so the function signature stays
    short. ``infisical_env`` defaults to "dev" — the legacy bash
    treats anything else as opt-in.
    """

    ssh_private_key_content: str | None = None
    gh_mirror_token: str | None = None
    gh_mirror_repos: str | None = None
    dockerhub_user: str | None = None
    dockerhub_token: str | None = None
    infisical_env: str = "dev"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ssh_keygen_cleanup(*targets: str) -> None:
    """Run ``ssh-keygen -R <target>`` for each non-empty target.

    Mirrors deploy.sh:165-166. Failures are silent (the legacy bash
    used ``|| true``): if the entry doesn't exist in known_hosts,
    ssh-keygen exits non-zero, but that's expected on a fresh runner.
    Captured output is discarded — operators don't need to see the
    "Host added/removed" diagnostic for this prep step.
    """
    for target in targets:
        if not target:
            continue
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            subprocess.run(
                ["ssh-keygen", "-R", target],
                check=False,
                capture_output=True,
                timeout=10.0,
            )


def _docker_hub_login(host: str, dockerhub_user: str, dockerhub_token: str) -> None:
    """Pipe the token over ssh-stdin into ``docker login --password-stdin``.

    PR #533 R2 #2 / R3 #2 lessons: we DON'T use ``cat > <path>``-style
    redirects with potentially-untrusted values, but the docker CLI
    itself reads ``--password-stdin`` for exactly this case (token
    via stdin, never argv → never visible in ``ps``). Username goes
    through argv (it's not a secret per Docker Hub's threat model)
    BUT must be shell-quoted: ssh receives the third argv element as
    a single shell command string, and an unquoted username with a
    space / metachar would be parsed by the remote shell. PR #535 R1
    #1: shlex.quote prevents an attacker who controls DOCKERHUB_USER
    (e.g. via a compromised CI secret) from injecting arbitrary
    commands into the remote ``docker login`` line.
    """
    quoted_user = shlex.quote(dockerhub_user)
    subprocess.run(
        ["ssh", host, f"docker login -u {quoted_user} --password-stdin"],
        input=dockerhub_token,
        check=True,
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _b64_encode_ssh_key(content: str | None) -> str:
    """Base64-encode the SSH private key for the BootstrapEnv.

    Legacy bash (deploy.sh:404-408) used ``echo "$X" | base64`` which
    appends a trailing newline before the pipe — so the legacy bytes
    are ``base64(<key>+\\n)``. We match that exactly: empty/None
    input returns empty string (preventing
    ``echo "" | base64`` → ``Cg==`` → BootstrapEnv treating it as a
    populated key). Non-empty: encode + strip trailing newline from
    the base64 output (``base64`` itself wraps).
    """
    import base64

    if not content:
        return ""
    # Match legacy bash semantic: append trailing newline before encoding.
    # ``echo "$X"`` adds the newline; ``printf '%s' "$X"`` would not.
    encoded = base64.b64encode((content + "\n").encode("utf-8"))
    return encoded.decode("ascii").replace("\n", "")


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    *,
    project_root: Path,
    options: PipelineOptions,
    # DI seams (production callers leave these None):
    tofu_runner: _tofu.TofuRunner | None = None,
    docker_hub_login: Callable[[str, str, str], None] | None = None,
) -> PipelineResult:
    """Run the full deploy pipeline.

    Exit-code semantics as the CLI handler maps them:
    - Hard failure (PipelineError raised) → CLI returns rc=2 (abort).
    - Orchestrator hard failure (any phase status='failed') →
      PipelineError raised, rc=2.
    - Orchestrator partial (any phase status='partial') OR clean run
      → CLI returns rc=0 (deploy succeeded; partial surfaces as
      stderr warning, NOT non-zero exit).

    The rc=0-on-partial contract was tightened in PR #535 R0/R1 — a
    non-zero exit in spin-up.yml's ``shell: bash -e`` step would
    fail the workflow even when the deploy completed successfully.
    Partial is operator-visible via the orchestrator's per-phase
    log emitted to stderr.

    ``tofu_runner`` and ``docker_hub_login`` are DI seams for tests;
    production callers pass None.
    """
    # 1. R2 credentials env-injection (BEFORE any tofu call — the R2
    #    backend reads AWS_* from os.environ at tofu-binary startup).
    creds_file = project_root / "tofu" / ".r2-credentials"
    creds = _tofu.load_r2_credentials(creds_file)
    if creds is not None:
        os.environ["AWS_ACCESS_KEY_ID"] = creds.access_key_id
        os.environ["AWS_SECRET_ACCESS_KEY"] = creds.secret_access_key

    # 2. tofu state pre-flight.
    tofu_dir = project_root / "tofu" / "stack"
    runner = tofu_runner if tofu_runner is not None else _tofu.TofuRunner(tofu_dir=tofu_dir)
    if not runner.state_list_ok():
        raise PipelineError(
            f"OpenTofu state at {tofu_dir} is not initialised — "
            "run the initial-setup workflow first",
        )

    # 3. config.tfvars + identity derivation.
    tfvars_path = tofu_dir / "config.tfvars"
    tfvars_config = _tfvars.parse(tfvars_path)
    if not tfvars_config.domain:
        raise PipelineError(
            f"{tfvars_path} is missing a non-empty 'domain' value",
        )
    identity = _tfvars.derive_gitea_identity(tfvars_config)

    # 4. Read tofu outputs. Required ones use no default → raise on
    #    missing. Optional ones default to safe empty values.
    secrets_json = runner.output_json("secrets", default={})
    if not secrets_json:
        raise PipelineError(
            "tofu output -json secrets is empty — state corrupt or Tofu not yet applied",
        )
    try:
        config = NexusConfig.from_secrets_json(json.dumps(secrets_json))
    except ConfigError as exc:
        raise PipelineError(f"could not parse secrets JSON: {exc}") from exc

    image_versions = runner.output_json("image_versions", default={})
    enabled_services_raw = runner.output_json("enabled_services", default=[])
    firewall_rules = runner.output_json("firewall_rules", default={})
    ssh_service_token = runner.output_json("ssh_service_token", default={})
    server_ip = runner.output_raw("server_ip", default="")
    persistent_volume_id = runner.output_raw("persistent_volume_id", default="0")

    if not isinstance(enabled_services_raw, list):
        raise PipelineError(
            f"tofu output enabled_services is {type(enabled_services_raw).__name__}, expected list",
        )
    enabled_services: list[str] = [str(s) for s in enabled_services_raw]

    # 5. SSH known_hosts cleanup — best-effort, never fatal.
    ssh_host_dns = _SSH_HOST_DNS_TEMPLATE.format(domain=tfvars_config.domain)
    _ssh_keygen_cleanup(ssh_host_dns, server_ip)

    # 6-10. Setup chain + orchestrator. Single ExitStack owns the
    # SSHClient lifetime so an early exception still tears it down.
    with contextlib.ExitStack() as stack:
        cf_client_id = ""
        cf_client_secret = ""
        if isinstance(ssh_service_token, dict):
            cf_client_id = str(ssh_service_token.get("client_id") or "")
            cf_client_secret = str(ssh_service_token.get("client_secret") or "")
        _setup.configure_ssh(
            _setup.SSHConfigSpec(
                ssh_host=ssh_host_dns,
                cf_client_id=cf_client_id,
                cf_client_secret=cf_client_secret,
            ),
        )
        readiness = _setup.wait_for_ssh()
        if not readiness.succeeded:
            raise PipelineError(
                f"SSH did not become ready after {readiness.attempts} attempts: "
                f"{readiness.last_error[:500]}",
            )

        ssh = stack.enter_context(SSHClient("nexus"))
        _setup.ensure_jq(ssh)
        _setup.mount_persistent_volume(persistent_volume_id, ssh)

        if options.dockerhub_user and options.dockerhub_token:
            login_fn = docker_hub_login if docker_hub_login is not None else _docker_hub_login
            login_fn("nexus", options.dockerhub_user, options.dockerhub_token)

        if "wetty" in enabled_services:
            _setup.setup_wetty_ssh_agent(ssh)

        # Build the BootstrapEnv + Orchestrator. workspace-coords
        # phase fills repo_name / gitea_repo_owner / etc. inside
        # run_pre_bootstrap; here we pre-populate the inputs it needs.
        bootstrap_env = BootstrapEnv(
            domain=tfvars_config.domain,
            admin_email=identity.admin_email,
            gitea_user_email=identity.gitea_user_email or None,
            gitea_user_username=identity.gitea_user_username or None,
            om_principal_domain=identity.om_principal_domain or None,
            ssh_private_key_base64=_b64_encode_ssh_key(options.ssh_private_key_content),
        )
        gh_mirror_repos_list = (
            [s.strip() for s in (options.gh_mirror_repos or "").split(",") if s.strip()]
            if options.gh_mirror_repos
            else []
        )
        orchestrator = Orchestrator(
            config=config,
            bootstrap_env=bootstrap_env,
            enabled_services=enabled_services,
            domain=tfvars_config.domain,
            admin_username=config.admin_username or "",
            user_email=tfvars_config.user_email_raw,
            gitea_admin_pass=config.gitea_admin_password,
            admin_password_infisical=config.infisical_admin_password,
            gitea_user_email=identity.gitea_user_email or None,
            gitea_user_username=identity.gitea_user_username or None,
            gitea_user_password=config.gitea_user_password,
            firewall_json=json.dumps(firewall_rules),
            image_versions_json=json.dumps(image_versions),
            gh_mirror_token=options.gh_mirror_token,
            gh_mirror_repos=gh_mirror_repos_list,
            woodpecker_agent_secret=config.woodpecker_agent_secret,
            project_root=project_root,
            infisical_env=options.infisical_env,
        )

        pre_result = orchestrator.run_pre_bootstrap()
        if pre_result.has_hard_failure:
            raise PipelineError(
                "pre-bootstrap pipeline aborted (see per-phase log above)",
            )

        all_result = orchestrator.run_all()
        if all_result.has_hard_failure:
            raise PipelineError(
                "post-bootstrap pipeline aborted (see per-phase log above)",
            )

    # 11. Service URLs (display only — failure is non-fatal).
    service_urls_raw = runner.output_json("service_urls", default={})
    if isinstance(service_urls_raw, dict):
        service_urls: dict[str, str] = {str(k): str(v) for k, v in service_urls_raw.items()}
    else:
        service_urls = {}

    return PipelineResult(
        pre_bootstrap=pre_result,
        run_all=all_result,
        service_urls=service_urls,
    )


def format_done_banner(result: PipelineResult) -> str:
    """Render the post-deploy banner that the legacy bash echoed at
    deploy.sh:449-469. Returns the banner as a single string for
    the CLI handler to print to stdout.
    """
    lines: list[str] = [
        "",
        "╔═══════════════════════════════════════════════════════════════╗",
        "║                    ✅ Deployment Complete!                    ║",
        "╚═══════════════════════════════════════════════════════════════╝",
        "",
        "🔗 Your Services:",
    ]
    if result.service_urls:
        for name in sorted(result.service_urls):
            lines.append(f"   {name}: {result.service_urls[name]}")
    else:
        lines.append("   (service URLs not available)")
    lines.extend(
        [
            "",
            "📌 SSH Access:",
            "   ssh nexus",
            "",
            "🔐 View credentials:",
            "   Credentials available in Infisical",
            "",
        ],
    )
    return "\n".join(lines)
