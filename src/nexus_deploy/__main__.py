"""Entry point for `python -m nexus_deploy ...` invocations.

Subcommand dispatcher. Subcommands land here as their modules ship.
Currently:
- ``config dump-shell`` (#505 Modul 1.3)
- ``infisical bootstrap`` (#505 Modul 1.1)
- ``secret-sync --stack <jupyter|marimo>`` (#505 Modul 1.2)
- ``seed --repo <owner>/<name> [--root PATH] [--prefix nexus_seeds/]``
  (#505 Modul 2.1)
- ``compose up --enabled <comma-list>`` (#505 Modul 2.2a)
- ``services configure --enabled <comma-list>`` (#505 Modul 2.2b/c/d)
- ``kestra register-system-flows`` (#505 Modul 2.3)
- ``gitea configure`` (#505 Modul 2.2e)
- ``gitea woodpecker-oauth`` (#505 Modul 2.2f)
- ``gitea mirror-setup`` (#505 Modul 2.2f part 2)
- ``stack-sync --enabled <comma-list>`` (#505 Modul 3.3)
- ``setup ssh-config`` (#505 Modul 3.4a)
- ``setup wait-ssh`` (#505 Modul 3.4a)
- ``setup ensure-jq`` (#505 Modul 3.4a)
- ``setup mount-volume`` (#505 Modul 3.4a)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import requests

from nexus_deploy import __version__, hello
from nexus_deploy.compose_runner import run_compose_up
from nexus_deploy.config import ConfigError, NexusConfig
from nexus_deploy.gitea import (
    GiteaError,
    run_configure_gitea,
    run_mirror_setup,
    run_woodpecker_oauth_setup,
)
from nexus_deploy.infisical import (
    BootstrapEnv,
    InfisicalClient,
    compute_folders,
    provision_admin,
)
from nexus_deploy.kestra import run_register_system_flows
from nexus_deploy.orchestrator import Orchestrator
from nexus_deploy.r2_tokens import (
    DEFAULT_NEXUS_R2_PREFIX,
    build_inventory,
    cleanup_orphan_tokens,
)
from nexus_deploy.secret_sync import StackTarget, run_sync_for_stack
from nexus_deploy.seeder import _is_safe_repo_path, run_seed_for_repo
from nexus_deploy.service_env import (
    GiteaWorkspaceConfig,
    ServiceEnvError,
    append_gitea_workspace_block,
    render_all_env_files,
)
from nexus_deploy.services import run_admin_setups
from nexus_deploy.setup import (
    SetupError,
    SSHConfigSpec,
    configure_ssh,
    ensure_jq,
    mount_persistent_volume,
    setup_wetty_ssh_agent,
    wait_for_service_token,
    wait_for_ssh,
)
from nexus_deploy.ssh import SSHClient, SSHError
from nexus_deploy.stack_sync import run_stack_sync


def _config_dump_shell(args: list[str]) -> int:
    """`nexus-deploy config dump-shell [--tofu-dir PATH | --stdin]`.

    Two input modes:
    - ``--tofu-dir PATH`` (default ``tofu/stack``): runs ``tofu output
      -json secrets`` inside that directory.
    - ``--stdin``: reads the SECRETS_JSON payload from stdin. Used by
      deploy.sh's strangler-fig handoff so the existing tofu call +
      empty-check stays in bash and we don't run tofu twice.

    Writes shell-eval-able ``VAR=value`` lines to stdout. Consumed via
    ``eval "$(... | python -m nexus_deploy config dump-shell --stdin)"``.
    """
    tofu_dir = Path("tofu/stack")
    tofu_dir_explicit = False
    use_stdin = False
    i = 0
    while i < len(args):
        if args[i] == "--tofu-dir":
            if i + 1 >= len(args):
                print("config dump-shell: --tofu-dir requires a PATH", file=sys.stderr)
                return 2
            tofu_dir = Path(args[i + 1])
            tofu_dir_explicit = True
            i += 2
        elif args[i] == "--stdin":
            use_stdin = True
            i += 1
        else:
            print(f"config dump-shell: unknown arg {args[i]!r}", file=sys.stderr)
            return 2
    if use_stdin and tofu_dir_explicit:
        print(
            "config dump-shell: --stdin and --tofu-dir are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    try:
        config = (
            NexusConfig.from_secrets_json(sys.stdin.read())
            if use_stdin
            else NexusConfig.from_tofu_output(tofu_dir)
        )
    except ConfigError as exc:
        print(f"config dump-shell: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(config.dump_shell())
    return 0


def _infisical_bootstrap(args: list[str]) -> int:
    """`nexus-deploy infisical bootstrap`.

    Reads SECRETS_JSON from stdin, reads the additional ``BootstrapEnv``
    fields (DOMAIN, ADMIN_EMAIL, GITEA_*, OM_PRINCIPAL_DOMAIN,
    WOODPECKER_*, SSH_KEY_BASE64) from environment variables,
    plus PROJECT_ID + INFISICAL_TOKEN + INFISICAL_ENV from environment
    variables. Computes the 39 folders, writes payloads, runs the
    server-side curl loop. Mirrors the legacy deploy.sh build_folder
    block (removed in #509).

    Note on env-var naming: the BootstrapEnv field is
    ``ssh_private_key_base64`` but the env var on the deploy.sh side
    is the bash-style ``SSH_KEY_BASE64`` (computed from
    ``SSH_PRIVATE_KEY_CONTENT`` via ``base64 | tr -d '\n'``). The
    asymmetry mirrors the legacy bash naming so deploy.sh's existing
    env-passing pattern doesn't need to be renamed in this PR.

    Required env: ``PROJECT_ID``, ``INFISICAL_TOKEN``.
    Optional env: ``INFISICAL_ENV`` (default ``dev``), the BootstrapEnv
    fields above, ``PUSH_DIR`` (default ``/tmp/infisical-push``).

    Exit codes (deploy.sh distinguishes the three so it can decide
    whether to abort):
    - 0: success, all folders pushed
    - 1: bootstrap completed but some folders reported errors
         (deploy.sh-side: warn-and-continue; the operator can fix
         partial pushes via the UI without aborting the rest of the
         spin-up)
    - 2: hard failure — input validation, transport (rsync/ssh),
         unexpected exception. deploy.sh-side: abort.
    """
    if args:
        print(f"infisical bootstrap: unexpected arg {args[0]!r}", file=sys.stderr)
        return 2
    project_id = os.environ.get("PROJECT_ID", "").strip()
    token = os.environ.get("INFISICAL_TOKEN", "").strip()
    if not project_id or not token:
        print(
            "infisical bootstrap: PROJECT_ID and INFISICAL_TOKEN env vars required",
            file=sys.stderr,
        )
        return 2
    try:
        config = NexusConfig.from_secrets_json(sys.stdin.read())
    except ConfigError as exc:
        print(f"infisical bootstrap: {exc}", file=sys.stderr)
        return 2
    bootstrap_env = BootstrapEnv(
        domain=os.environ.get("DOMAIN") or None,
        admin_email=os.environ.get("ADMIN_EMAIL") or None,
        gitea_user_email=os.environ.get("GITEA_USER_EMAIL") or None,
        gitea_user_username=os.environ.get("GITEA_USER_USERNAME") or None,
        gitea_repo_owner=os.environ.get("GITEA_REPO_OWNER") or None,
        repo_name=os.environ.get("REPO_NAME") or None,
        om_principal_domain=os.environ.get("OM_PRINCIPAL_DOMAIN") or None,
        woodpecker_gitea_client=os.environ.get("WOODPECKER_GITEA_CLIENT") or None,
        woodpecker_gitea_secret=os.environ.get("WOODPECKER_GITEA_SECRET") or None,
        ssh_private_key_base64=os.environ.get("SSH_KEY_BASE64") or None,
    )
    push_dir = Path(os.environ.get("PUSH_DIR") or "/tmp/infisical-push")  # noqa: S108
    client = InfisicalClient(
        project_id=project_id,
        env=os.environ.get("INFISICAL_ENV") or "dev",
        token=token,
        push_dir=push_dir,
    )
    try:
        folders = compute_folders(config, bootstrap_env)
        result = client.bootstrap(folders)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Hard failure: rsync/ssh exited non-zero, hit the timeout, or
        # the binary wasn't on PATH. deploy.sh sees rc=2 and aborts.
        # Avoid printing exc.cmd because TimeoutExpired/CalledProcessError
        # carry the full argv — we don't want the token (if it ever
        # leaked into argv via a future bug) to land in the workflow log.
        print(
            f"infisical bootstrap: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        # Anything else is a programming error in compute_folders/
        # bootstrap (KeyError, ValidationError, AttributeError, …).
        # Python's default exit code for an unhandled exception is 1,
        # which deploy.sh's rc-dispatch treats as "partial push" —
        # exactly what this catch prevents. Force rc=2 so deploy.sh
        # aborts instead of continuing past a broken bootstrap.
        # We print only the exception CLASS name; ``str(exc)`` and
        # ``repr(exc)`` can carry attribute values that might include
        # secret-bearing fields from a NexusConfig or BootstrapEnv
        # pydantic ValidationError.
        # Class name only (no str/repr): exception args may carry
        # secret-bearing fields from a NexusConfig/BootstrapEnv
        # ValidationError. Operators reproducing locally without
        # secret data will see the full traceback there.
        print(
            f"infisical bootstrap: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    print(
        f"infisical bootstrap: built={result.folders_built} pushed={result.pushed} failed={result.failed}",
    )
    return 0 if result.failed == 0 else 1


def _infisical_provision_admin(args: list[str]) -> int:
    """`nexus-deploy infisical provision-admin`.

    Replaces the bash readiness-probe + admin-bootstrap + project-create
    + cred-persist block at deploy.sh:792-869. Renders + runs a server-
    side bash script via SSH that:

    1. Waits for Infisical to be ready (60s container + 120s HTTP).
    2. Detects whether Infisical is already initialized.
    3. If yes: loads saved (token, project_id) from
       ``/opt/docker-server/.infisical-{token,project-id}``.
    4. If no: POST ``/api/v1/admin/bootstrap`` (admin user + org) →
       POST ``/api/v2/workspace`` (project) → save creds to disk.

    Required env: ``ADMIN_EMAIL`` + ``INFISICAL_PASS``.

    Stdout (eval-able by deploy.sh):
    - ``INFISICAL_TOKEN=<token>``
    - ``PROJECT_ID=<workspace-id>``

    Both lines are always emitted (even on the not-ready / failure
    paths, with empty values) so deploy.sh's eval doesn't leak stale
    values from a previous run.

    Exit codes:
    - 0: ``loaded-existing`` or ``freshly-bootstrapped`` —
      (token, project_id) populated, downstream push can proceed.
    - 1: ``not-ready`` / ``loaded-existing-missing-creds`` /
      ``already-bootstrapped-no-saved-creds`` /
      ``bootstrap-failed`` / ``project-create-failed`` — soft fail,
      deploy.sh warns and continues without pushing secrets.
    - 2: bad args, transport, unexpected error — deploy.sh aborts.
    """
    if args:
        print(f"infisical provision-admin: unexpected arg {args[0]!r}", file=sys.stderr)
        return 2

    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    admin_password = os.environ.get("INFISICAL_PASS", "").strip()
    if not admin_email or not admin_password:
        print(
            "infisical provision-admin: ADMIN_EMAIL and INFISICAL_PASS env vars required",
            file=sys.stderr,
        )
        return 2

    try:
        result = provision_admin(
            admin_email=admin_email,
            admin_password=admin_password,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"infisical provision-admin: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"infisical provision-admin: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    # Always emit the two values (even empty) — deploy.sh's eval relies
    # on the assignment to clear any stale value left over from prior
    # runs. shlex.quote handles the empty-string + edge cases.
    import shlex as _shlex

    sys.stdout.write(f"INFISICAL_TOKEN={_shlex.quote(result.token or '')}\n")
    sys.stdout.write(f"PROJECT_ID={_shlex.quote(result.project_id or '')}\n")

    # Per-status stderr line so the workflow log carries the human-
    # readable outcome (the eval-able stdout is for shell consumption).
    sys.stderr.write(f"infisical provision-admin: status={result.status}\n")

    # rc=0 ONLY when the provision actually produced usable credentials
    # (token AND project_id both populated). A `loaded-existing` /
    # `freshly-bootstrapped` status with a dropped token (e.g.
    # malformed-base64 → parse_provision_result returned None for
    # token) MUST be reported as soft-fail so deploy.sh doesn't print
    # "✓ Infisical provisioned" while emitting empty
    # INFISICAL_TOKEN= / PROJECT_ID= lines that downstream eval'd
    # consumers would treat as legitimate. Caught in #530 R2.
    if result.status in ("loaded-existing", "freshly-bootstrapped") and result.has_credentials:
        return 0
    return 1


_VALID_STACKS = ("jupyter", "marimo", "kestra")


def _secret_sync(args: list[str]) -> int:
    """`nexus-deploy secret-sync --stack <jupyter|marimo>`.

    Fetches Infisical secrets, filters/escapes them, and writes the
    result to ``/opt/docker-server/stacks/<stack>/.infisical.env`` on
    the server. On change, restarts the stack via ``docker compose
    up -d <stack>``. Mirrors the two legacy deploy.sh secret-sync
    heredocs (one per stack, removed in #510) — both were byte-identical
    apart from stack-name + paths, so the migration collapses them
    to one rendering layer parametrised by :class:`StackTarget`.

    Required env: ``PROJECT_ID``, ``INFISICAL_TOKEN``.
    Optional env: ``INFISICAL_ENV`` (default ``dev``), ``GITEA_TOKEN``
    (special-case append — auto-generated post-Gitea-bootstrap, not
    in Infisical at sync time).

    Exit codes (deploy.sh's case-block dispatches):
    - 0: success, OR sync correctly chose not to write (one of the
         two outage gates fired — operator sees a stderr warning,
         existing file untouched, deploy.sh continues), OR the remote
         script produced no parseable RESULT line (treated as a soft
         no-op: matches deploy.sh's pre-migration `[ -z "$JUP_PUSHED" ]`
         warn-and-continue branch; the inner script's own stderr is
         already in the workflow log for diagnosis)
    - 1: partial — file written but at least one folder fetch failed
         (deploy.sh-side: warn-and-continue; the operator can fix the
         offending folder via the Infisical UI without aborting)
    - 2: hard failure — invalid `--stack`, missing required env,
         transport (ssh) failure, unexpected exception. deploy.sh-side:
         abort.
    """
    stack: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--stack":
            if i + 1 >= len(args):
                print("secret-sync: --stack requires a value", file=sys.stderr)
                return 2
            stack = args[i + 1]
            i += 2
        else:
            print(f"secret-sync: unknown arg {args[i]!r}", file=sys.stderr)
            return 2
    if stack is None:
        print("secret-sync: --stack <jupyter|marimo> is required", file=sys.stderr)
        return 2
    if stack not in _VALID_STACKS:
        print(
            f"secret-sync: unknown stack {stack!r} (expected one of {_VALID_STACKS})",
            file=sys.stderr,
        )
        return 2

    project_id = os.environ.get("PROJECT_ID", "").strip()
    token = os.environ.get("INFISICAL_TOKEN", "").strip()
    if not project_id or not token:
        print(
            "secret-sync: PROJECT_ID and INFISICAL_TOKEN env vars required",
            file=sys.stderr,
        )
        return 2
    infisical_env = os.environ.get("INFISICAL_ENV") or "dev"
    gitea_token = os.environ.get("GITEA_TOKEN") or ""

    # Kestra writes SECRET_<KEY>=<base64> to .env directly (no separate
    # .infisical.env), and force-recreates so EnvVarSecretProvider
    # loads the new values. Jupyter/Marimo use the original
    # plaintext-to-.infisical.env shape with `up -d` (no force).
    if stack == "kestra":
        target = StackTarget(
            name="kestra",
            key_prefix="SECRET_",
            use_base64_values=True,
            env_file_basename=".env",
            legacy_env_file_basename=None,
            force_recreate=True,
        )
    else:
        target = StackTarget(name=stack)
    try:
        result = run_sync_for_stack(
            target,
            project_id=project_id,
            infisical_token=token,
            infisical_env=infisical_env,
            gitea_token=gitea_token,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Same defence-in-depth as `infisical bootstrap`: never print
        # exc.cmd (carries argv that COULD include secrets if a future
        # bug regressed _remote.ssh_run_script's stdin contract).
        print(
            f"secret-sync: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        # Programming errors (KeyError, AttributeError, etc.) — Python's
        # default rc=1 would collide with the partial-failure semantic
        # in deploy.sh, so force rc=2. Class name only — no str/repr,
        # which could embed secret-bearing values.
        print(
            f"secret-sync: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    # All-zero counters with wrote=False: either the remote script
    # printed no parseable RESULT line, OR it took the legitimate
    # jq-missing path (which intentionally emits an all-zero RESULT).
    # Both are warn-and-continue (rc=0), mirroring deploy.sh's
    # pre-migration `[ -z "$JUP_PUSHED" ]` branch — the inner script
    # already printed its own warning to stderr (workflow log).
    # Distinguishing them would require a dedicated sentinel; not
    # worth the wire-format churn given both demand the same response.
    if (
        result.pushed == 0
        and result.failed_folders == 0
        and result.succeeded_folders == 0
        and not result.wrote
    ):
        print(
            f"secret-sync: {stack} produced no usable result (see prior warnings)",
        )
        return 0

    if result.wrote and result.failed_folders == 0 and result.collisions == 0:
        print(
            f"secret-sync: {stack} wrote {result.pushed} env-vars (plaintext, exact key names)",
        )
        return 0
    if result.wrote and result.failed_folders > 0:
        print(
            f"secret-sync: {stack} wrote {result.pushed} env-vars "
            f"({result.failed_folders} folder fetch(es) failed — secret set is incomplete)",
        )
        return 1
    if result.wrote and result.collisions > 0:
        print(
            f"secret-sync: {stack} wrote {result.pushed} env-vars "
            f"({result.collisions} cross-folder collision(s) — first-wins applied)",
        )
        return 0
    # wrote=False with non-zero counters — one of the two outage gates
    # fired (succeeded==0 or pushed==0). Existing file untouched.
    # Operator already saw the cause from the inner script's stderr.
    print(
        f"secret-sync: {stack} skipped .infisical.env update (kept previous; see prior warning)",
    )
    return 0


def _seed(args: list[str]) -> int:
    """`nexus-deploy seed --repo <owner>/<name> [--root PATH] [--prefix STR]`.

    Walks the local seed tree (default ``examples/workspace-seeds/``),
    base64-encodes each file, rsyncs the JSON payloads to the server,
    and POSTs each one to Gitea's Contents API under the prefix
    (default ``nexus_seeds/``). Mirrors the legacy
    ``seed_workspace_files`` deploy.sh function (removed in #505 Modul
    2.1) which had two call-sites — non-mirror mode (admin-owned repo)
    and mirror+user mode (user's fork). Both call-sites now invoke
    this CLI with the appropriate ``--repo`` arg.

    Required env: ``GITEA_TOKEN``.

    Exit codes (deploy.sh's case-block dispatches):
    - 0: all seeds either created (HTTP 201/200) or correctly skipped
         (HTTP 422 = file already exists, user edits persist — #501
         contract).
    - 1: partial — some files failed but at least one succeeded.
         deploy.sh-side: yellow warning, continue.
    - 2: hard failure — bad ``--repo`` format, missing token, transport
         (ssh/rsync) failure, no parseable RESULT line, unexpected
         exception. deploy.sh-side: abort.
    """
    repo: str | None = None
    root_arg: str | None = None
    prefix = "nexus_seeds/"
    i = 0
    while i < len(args):
        if args[i] == "--repo":
            if i + 1 >= len(args):
                print("seed: --repo requires a value", file=sys.stderr)
                return 2
            repo = args[i + 1]
            i += 2
        elif args[i] == "--root":
            if i + 1 >= len(args):
                print("seed: --root requires a value", file=sys.stderr)
                return 2
            root_arg = args[i + 1]
            i += 2
        elif args[i] == "--prefix":
            if i + 1 >= len(args):
                print("seed: --prefix requires a value", file=sys.stderr)
                return 2
            prefix = args[i + 1]
            i += 2
        else:
            print(f"seed: unknown arg {args[i]!r}", file=sys.stderr)
            return 2

    if repo is None or "/" not in repo:
        print(
            "seed: --repo <owner>/<name> is required (must contain '/')",
            file=sys.stderr,
        )
        return 2
    repo_owner, _, repo_name = repo.partition("/")
    if not repo_owner or not repo_name:
        print(
            f"seed: invalid --repo {repo!r} — both owner and name required",
            file=sys.stderr,
        )
        return 2

    # Validate --prefix: must be empty (seed into repo root) OR a
    # safe relative directory ending with `/`. The safe-char regex
    # alone is not enough because it permits ``..``, leading ``/``,
    # and empty segments (``//``) — all of which produce dangerous
    # repo_paths when concatenated with the relative file path:
    #   ``../`` + ``kestra/x.yaml`` → ``../kestra/x.yaml``  (escape)
    #   ``/foo/`` + ``kestra/x.yaml`` → ``/foo/kestra/x.yaml`` (absolute)
    #   ``a//b/`` + ``...``           → ``a//b/...`` (empty segment)
    # Surfacing this at CLI parse time saves a wasted spin-up roundtrip.
    if prefix:
        prefix_segments = prefix.split("/")
        # Trailing "/" → last segment is empty; that's the required form.
        # We slice it off before per-segment validation.
        if prefix_segments[-1] != "":
            print(
                f"seed: invalid --prefix {prefix!r} — must end with '/'",
                file=sys.stderr,
            )
            return 2
        body_segments = prefix_segments[:-1]
        if not body_segments or any(
            seg in ("", ".", "..") or not _is_safe_repo_path(seg) for seg in body_segments
        ):
            print(
                f"seed: invalid --prefix {prefix!r} — must be empty or a "
                "safe relative path ending with '/' (no '..', no leading "
                "'/', no empty segments, only [A-Za-z0-9._-] per segment)",
                file=sys.stderr,
            )
            return 2

    token = os.environ.get("GITEA_TOKEN", "").strip()
    if not token:
        print("seed: GITEA_TOKEN env var required", file=sys.stderr)
        return 2

    root = Path(root_arg) if root_arg else Path("examples/workspace-seeds")
    if not root.is_dir():
        print(
            f"seed: root {root!s} is not a directory (skipping with rc=0)",
            file=sys.stderr,
        )
        # Mirror deploy.sh:3340 — missing seed dir is non-fatal.
        return 0

    try:
        result = run_seed_for_repo(
            repo_owner=repo_owner,
            repo_name=repo_name,
            root=root,
            token=token,
            prefix=prefix,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Same defence-in-depth as `secret-sync`: never print exc.cmd /
        # str(exc) / repr(exc) — they may carry the token.
        print(f"seed: transport failure ({type(exc).__name__})", file=sys.stderr)
        return 2
    except Exception as exc:
        # Force rc=2 (Python's default rc=1 collides with our
        # partial-failure semantic).
        print(f"seed: unexpected error ({type(exc).__name__})", file=sys.stderr)
        return 2

    print(
        f"seed: {repo_owner}/{repo_name} — created={result.created} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    if result.failed > 0:
        if result.created + result.skipped == 0:
            return 2
        return 1
    return 0


def _compose_up(args: list[str]) -> int:
    """`nexus-deploy compose up --enabled <comma-list>`.

    Renders the parallel ``docker compose up -d --build`` loop for
    every enabled service, runs it server-side via ssh, parses the
    RESULT line. Replaces the legacy 130-line ssh heredoc in
    ``scripts/deploy.sh`` (the [6/7] step). Per-service admin-setup
    hooks (Wikijs, Dify, etc.) are scoped to Modul 2.2b.

    The comma-list maps directly to deploy.sh's ``$ENABLED_SERVICES``;
    callers pass it as-is. Virtual-service expansion + parent-stack
    deduplication + deferred-services skipping happen inside the
    compose_runner module.

    Exit codes:
    - 0: all enabled services started + verified running.
    - 1: at least one service failed but at least one succeeded
         (deploy.sh continues — the operator sees the per-service
         ✗ line for diagnosis).
    - 2: hard failure — invalid args, transport (ssh) failure, no
         parseable RESULT line. deploy.sh aborts.
    """
    if not args or args[0] != "up":
        print("compose: only 'up' subcommand is supported", file=sys.stderr)
        return 2

    enabled_str: str | None = None
    i = 1
    while i < len(args):
        if args[i] == "--enabled":
            if i + 1 >= len(args):
                print("compose up: --enabled requires a value", file=sys.stderr)
                return 2
            enabled_str = args[i + 1]
            i += 2
        else:
            print(f"compose up: unknown arg {args[i]!r}", file=sys.stderr)
            return 2

    if enabled_str is None:
        print(
            "compose up: --enabled <comma-separated-services> is required",
            file=sys.stderr,
        )
        return 2

    enabled = [s.strip() for s in enabled_str.split(",") if s.strip()]
    if not enabled:
        # Empty list = nothing to do = success (mirrors deploy.sh's
        # behaviour when ENABLED_SERVICES is empty).
        print("compose up: no services enabled, nothing to do")
        return 0

    try:
        result = run_compose_up(enabled)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"compose up: transport failure ({type(exc).__name__})", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"compose up: unexpected error ({type(exc).__name__})", file=sys.stderr)
        return 2

    print(f"compose up: started={result.started} failed={result.failed}")
    if result.failed > 0:
        if result.started == 0:
            return 2
        return 1
    return 0


def _services_configure(args: list[str]) -> int:
    """`nexus-deploy services configure --enabled <comma-list>`.

    Renders + executes the per-service admin-setup hooks for the
    enabled services that have a renderer in
    ``nexus_deploy.services._HOOK_REGISTRY``. Reads NexusConfig from
    stdin (SECRETS_JSON) and reads BootstrapEnv fields (DOMAIN,
    ADMIN_EMAIL, etc.) from environment variables — same handoff
    pattern as ``infisical bootstrap``.

    Currently shipped:
    - Modul 2.2b — Portainer, n8n, Metabase, LakeFS, OpenMetadata
    - Modul 2.2c — RedPanda, Superset
    - Modul 2.2d — Filestash (python-side JSON mutation)
    Remaining admin-setup hooks (Gitea, SFTPGo, Garage, Windmill,
    Wikijs, Dify) ship in follow-up modules.

    Exit codes:
    - 0: all enabled+supported hooks ended in configured /
         already-configured / skipped-not-ready states (no failures).
    - 1: at least one hook reported status=failed but at least one
         succeeded. deploy.sh: yellow warning, continue.
    - 2: bad args, transport (ssh) failure, or unexpected exception.
         deploy.sh: red, abort.
    """
    if not args or args[0] != "configure":
        print("services: only 'configure' subcommand is supported", file=sys.stderr)
        return 2

    enabled_str: str | None = None
    i = 1
    while i < len(args):
        if args[i] == "--enabled":
            if i + 1 >= len(args):
                print(
                    "services configure: --enabled requires a value",
                    file=sys.stderr,
                )
                return 2
            enabled_str = args[i + 1]
            i += 2
        else:
            print(f"services configure: unknown arg {args[i]!r}", file=sys.stderr)
            return 2

    if enabled_str is None:
        print(
            "services configure: --enabled <comma-separated-services> is required",
            file=sys.stderr,
        )
        return 2

    enabled = [s.strip() for s in enabled_str.split(",") if s.strip()]
    if not enabled:
        print("services configure: no services enabled, nothing to do")
        return 0

    try:
        config = NexusConfig.from_secrets_json(sys.stdin.read())
    except ConfigError as exc:
        print(f"services configure: {exc}", file=sys.stderr)
        return 2
    bootstrap_env = BootstrapEnv(
        domain=os.environ.get("DOMAIN") or None,
        admin_email=os.environ.get("ADMIN_EMAIL") or None,
        gitea_user_email=os.environ.get("GITEA_USER_EMAIL") or None,
        gitea_user_username=os.environ.get("GITEA_USER_USERNAME") or None,
        gitea_repo_owner=os.environ.get("GITEA_REPO_OWNER") or None,
        repo_name=os.environ.get("REPO_NAME") or None,
        om_principal_domain=os.environ.get("OM_PRINCIPAL_DOMAIN") or None,
        woodpecker_gitea_client=os.environ.get("WOODPECKER_GITEA_CLIENT") or None,
        woodpecker_gitea_secret=os.environ.get("WOODPECKER_GITEA_SECRET") or None,
        ssh_private_key_base64=os.environ.get("SSH_KEY_BASE64") or None,
    )

    try:
        result = run_admin_setups(config, bootstrap_env, enabled)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"services configure: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"services configure: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    print(
        f"services configure: configured={result.configured} "
        f"already-configured={result.already_configured} "
        f"skipped-not-ready={result.skipped_not_ready} "
        f"failed={result.failed}"
    )
    if result.failed > 0:
        if result.configured + result.already_configured == 0:
            return 2
        return 1
    return 0


def _kestra_register_system_flows(args: list[str]) -> int:
    """`nexus-deploy kestra register-system-flows`.

    Opens an SSH port-forward to the nexus host. The Kestra container
    listens on port 8080 internally; ``stacks/kestra/docker-compose.yml``
    publishes it as ``8085:8080`` (no explicit host-IP, so it binds
    every interface on the host — but the host firewall blocks external
    8085, so the only reachable path is through ssh + the server's
    loopback). We ``ssh -L 127.0.0.1:<local>:localhost:8085`` to reach
    the host-published port through the tunnel. Once it's up we
    register ``system.git-sync`` + ``system.flow-sync`` via local HTTP
    and trigger a one-shot ``flow-sync`` execution to onboard
    user-seeded flows immediately.

    Reads ``NexusConfig`` from stdin (SECRETS_JSON) and the per-deploy
    repo coordinates from environment variables — same handoff pattern
    as ``services configure``:

    - ``ADMIN_EMAIL`` — Kestra basic-auth username
    - ``GITEA_REPO_OWNER`` — owner of the workspace repo (admin in
      non-mirror, the user in mirror+user mode)
    - ``REPO_NAME`` — workspace repo name
    - ``WORKSPACE_BRANCH`` — git branch (default ``main``)
    - ``KESTRA_HOST`` — SSH host alias (default ``nexus``); exposed
      so a future test deploy can target a different alias

    Exit codes:
    - 0: both flows registered (or already-up-to-date) AND the
         onboarding execute settled in SUCCESS within timeout.
    - 1: at least one flow registration / execution did NOT succeed
         (deploy.sh: yellow warning, continue — the cron tick will
         catch user flows later).
    - 2: bad args, ssh tunnel setup failure, or unexpected exception
         (deploy.sh: red, abort).
    """
    if args:
        print(f"kestra register-system-flows: unknown args {args!r}", file=sys.stderr)
        return 2

    repo_owner = os.environ.get("GITEA_REPO_OWNER") or ""
    repo_name = os.environ.get("REPO_NAME") or ""
    branch = os.environ.get("WORKSPACE_BRANCH") or "main"
    admin_email = os.environ.get("ADMIN_EMAIL") or ""
    ssh_host = os.environ.get("KESTRA_HOST") or "nexus"

    missing = [
        name
        for name, val in (
            ("GITEA_REPO_OWNER", repo_owner),
            ("REPO_NAME", repo_name),
            ("ADMIN_EMAIL", admin_email),
        )
        if not val
    ]
    if missing:
        print(
            f"kestra register-system-flows: missing required env: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    try:
        config = NexusConfig.from_secrets_json(sys.stdin.read())
    except ConfigError as exc:
        print(f"kestra register-system-flows: {exc}", file=sys.stderr)
        return 2

    if not config.kestra_admin_password:
        # Round-2 fix: previously rc=0 (mapped to green "registered"
        # banner in deploy.sh, misleading). rc=1 routes to the yellow-
        # warning branch — accurate signal that nothing was registered.
        print(
            "kestra register-system-flows: KESTRA_PASS missing from SECRETS_JSON — "
            "skipping (Kestra basic-auth would 401 on every call)",
            file=sys.stderr,
        )
        return 1

    # Round-2 fix: pick a free local port via socket.bind(0) instead of
    # hardcoded 8085. Hardcoded would clash with leftover ssh tunnels
    # or any local service already on 8085; the new pre-bind probe
    # asks the kernel for a free ephemeral port. Tiny race window
    # (the port is closed before ssh -L re-binds) but vastly better
    # than the previous unconditional collision.
    local_port = _allocate_free_port()

    try:
        with (
            SSHClient(ssh_host) as ssh,
            ssh.port_forward(local_port, "localhost", 8085) as port,
        ):
            result = run_register_system_flows(
                config,
                base_url=f"http://localhost:{port}",
                repo_owner=repo_owner,
                repo_name=repo_name,
                branch=branch,
                admin_email=admin_email,
            )
    except SSHError as exc:
        # SSHError is the typed transport-failure path from ssh.py.
        # str(exc) is intentional here — SSHError messages are fixed
        # format strings (no subprocess output), see ssh.py docstring.
        print(f"kestra register-system-flows: ssh tunnel failed: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"kestra register-system-flows: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"kestra register-system-flows: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    # Per-flow result lines so the operator sees the POST/PUT detail
    # (e.g. "system.git-sync: created (POST 201)") in the deploy log.
    for flow in result.flows:
        sys.stderr.write(f"  • {flow.name}: {flow.status} ({flow.detail})\n")
    if result.execution_state is not None:
        # Round-2 fix: per-state actionable warning instead of bare enum.
        # Mirrors the hint deploy.sh L3464/L3489/L3493/L3496 used to print.
        hint = _kestra_execution_hint(result.execution_state)
        sys.stderr.write(
            f"  • system.flow-sync onboarding execution: {result.execution_state}"
            f"{(' — ' + hint) if hint else ''}\n",
        )
    if result.verify_skipped_reason is not None:
        # Verification step itself didn't complete (transient 5xx /
        # transport blip during flow_exists). State stays SUCCESS but
        # the operator sees that the check wasn't actually run.
        sys.stderr.write(
            f"  • seed-flow visibility check skipped: {result.verify_skipped_reason}\n",
        )

    print(
        f"kestra register-system-flows: "
        f"created={sum(1 for f in result.flows if f.status == 'created')} "
        f"updated={sum(1 for f in result.flows if f.status == 'updated')} "
        f"failed={sum(1 for f in result.flows if f.status == 'failed')} "
        f"execution={result.execution_state or 'skipped'}",
    )
    return 0 if result.is_success else 1


def _gitea_configure(args: list[str]) -> int:
    """`nexus-deploy gitea configure`.

    Opens an SSH port-forward to nexus, runs the synchronous Gitea
    configure flow (DB password sync, admin/user create-or-sync with
    legacy email-collision PATCH, API token create with retry-via-
    delete, workspace repo + collaborator), emits stdout in
    eval-able shell form so deploy.sh can capture the token via:

    .. code-block:: bash

        GITEA_OUT=$(mktemp); python -m nexus_deploy gitea configure > "$GITEA_OUT"
        eval "$(cat "$GITEA_OUT")"  # GITEA_TOKEN=...; RESTART_SERVICES=...
        rm -f "$GITEA_OUT"

    **stdout** (eval-able):
    - ``GITEA_TOKEN=<sha1>`` — only if token was successfully minted
    - ``RESTART_SERVICES=<csv>`` — git-integrated services intersected
      with ``$ENABLED_SERVICES`` (always emitted, may be empty string)

    **stderr**: per-step status lines for the deploy log.

    Reads ``NexusConfig`` from stdin (SECRETS_JSON) and per-deploy
    coordinates from environment variables:

    - ``ADMIN_EMAIL`` — admin's email
    - ``GITEA_USER_EMAIL`` (optional) — regular user's email. Drives the
      legacy email-collision PATCH check on the admin row. The user is
      created/synced ONLY when both this AND ``GITEA_USER_PASS`` are set
      — if either is missing the user-create/sync branch is silently
      skipped (mirrors deploy.sh L2617's `[ -n "$GITEA_USER_EMAIL" ] &&
      [ -n "$GITEA_USER_PASS" ]` guard).
    - ``GITEA_USER_PASS`` (optional) — see ``GITEA_USER_EMAIL`` above
    - ``REPO_NAME`` — workspace repo name (e.g. nexus-<slug>-gitea)
    - ``GITEA_REPO_OWNER`` — owner of the workspace repo
    - ``ENABLED_SERVICES`` — comma-or-space list driving the
      RESTART_SERVICES intersection
    - ``GH_MIRROR_REPOS`` (optional) — if non-empty, skip repo+collab
      (deferred to Modul 2.2f mirror-mode)
    - ``GITEA_HOST`` — SSH host alias (default ``nexus``)

    Exit codes:
    - 0: success — admin configured, token minted, repo state OK
    - 1: partial — at least one step failed but token may be in stdout
    - 2: bad args / ssh / unexpected — NO token in stdout
    """
    if args:
        print(f"gitea configure: unknown args {args!r}", file=sys.stderr)
        return 2

    admin_email = os.environ.get("ADMIN_EMAIL") or ""
    repo_name = os.environ.get("REPO_NAME") or ""
    gitea_repo_owner = os.environ.get("GITEA_REPO_OWNER") or ""
    enabled_str = os.environ.get("ENABLED_SERVICES") or ""
    ssh_host = os.environ.get("GITEA_HOST") or "nexus"
    gitea_user_email = os.environ.get("GITEA_USER_EMAIL") or None
    gitea_user_password = os.environ.get("GITEA_USER_PASS") or None
    is_mirror_mode = bool(os.environ.get("GH_MIRROR_REPOS") or "")

    missing = [
        name
        for name, val in (
            ("ADMIN_EMAIL", admin_email),
            ("REPO_NAME", repo_name),
            ("GITEA_REPO_OWNER", gitea_repo_owner),
        )
        if not val
    ]
    if missing:
        print(
            f"gitea configure: missing required env: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    enabled = [s.strip() for s in enabled_str.replace(",", " ").split() if s.strip()]

    try:
        config = NexusConfig.from_secrets_json(sys.stdin.read())
    except ConfigError as exc:
        print(f"gitea configure: {exc}", file=sys.stderr)
        return 2

    if not config.gitea_admin_password:
        # Required for both the CLI sync_password and REST basic-auth
        # paths. Without it everything 401s; emit rc=1 so deploy.sh
        # routes to yellow warning, NOT rc=0 (which would be a silent
        # green pass — same bug class as kestra round-2).
        print(
            "gitea configure: GITEA_ADMIN_PASS missing from SECRETS_JSON — "
            "skipping (basic-auth would 401 on every call)",
            file=sys.stderr,
        )
        # Still emit empty RESTART_SERVICES line so eval doesn't
        # leave a stale value from a previous deploy.
        print('RESTART_SERVICES=""')
        return 1

    local_port = _allocate_free_port()

    try:
        with (
            SSHClient(ssh_host) as ssh,
            ssh.port_forward(local_port, "localhost", 3200) as port,
        ):
            result = run_configure_gitea(
                config,
                base_url=f"http://localhost:{port}",
                ssh=ssh,
                admin_email=admin_email,
                gitea_user_email=gitea_user_email,
                gitea_user_password=gitea_user_password,
                repo_name=repo_name,
                gitea_repo_owner=gitea_repo_owner,
                is_mirror_mode=is_mirror_mode,
                enabled_services=enabled,
            )
    except SSHError as exc:
        print(f"gitea configure: ssh tunnel failed: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"gitea configure: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"gitea configure: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    # Per-step status lines on stderr for the deploy log.
    if result.db_pw_synced:
        sys.stderr.write("  • gitea-db password synced\n")
    sys.stderr.write(
        f"  • admin: {result.admin.status}"
        f"{(' — ' + result.admin.detail) if result.admin.detail else ''}\n"
    )
    if result.user is not None:
        sys.stderr.write(
            f"  • user: {result.user.status}"
            f"{(' — ' + result.user.detail) if result.user.detail else ''}\n"
        )
    if result.repo is not None:
        sys.stderr.write(
            f"  • repo: {result.repo.status}"
            f"{(' — ' + result.repo.detail) if result.repo.detail else ''}\n"
        )
    if result.collaborator_added:
        sys.stderr.write("  • collaborator added\n")
    if result.token is None:
        # Always surface the diagnostic — the post-#519 spin-up showed
        # how a silent token-mint failure (no error string in the deploy
        # log) blocks debugging. ``token_error`` is constructed from
        # Gitea CLI error text + return codes, no secrets.
        detail = f" — {result.token_error}" if result.token_error else ""
        sys.stderr.write(f"  • token: NOT minted (downstream skipped){detail}\n")

    # Eval-able stdout. RESTART_SERVICES is always emitted (even
    # empty) so deploy.sh's ``eval`` doesn't leave a stale value
    # from a previous run in the variable. ``shlex.quote`` on every
    # value — Gitea sha1 tokens are 40 hex chars in practice (no
    # special chars), but the explicit quote contract makes
    # injection-safety unambiguous, same as config dump-shell (#508).
    import shlex as _shlex

    if result.token is not None:
        sys.stdout.write(f"GITEA_TOKEN={_shlex.quote(result.token)}\n")
    sys.stdout.write(f"RESTART_SERVICES={_shlex.quote(','.join(result.restart_services))}\n")

    return 0 if result.is_success else 1


def _gitea_woodpecker_oauth(args: list[str]) -> int:
    """`nexus-deploy gitea woodpecker-oauth`.

    Provisions Gitea's "Woodpecker CI" OAuth2 application. Idempotent
    re-run: deletes any existing app of that name, then creates fresh
    so deploy.sh sees a known-fresh client_secret on every spin-up
    (Gitea has no rotate-secret API).

    Required env:

    - ``DOMAIN`` — used to build redirect URI ``https://woodpecker.<domain>/authorize``
    - ``GITEA_TOKEN`` — token-bearer auth for the admin user
      (eval-captured by deploy.sh from the prior ``gitea configure``
      invocation, see Modul 2.2e)

    Optional env:

    - ``ADMIN_USERNAME`` — admin username, path-validated (default
      ``admin``). Mirrors :class:`NexusConfig`'s ``admin_username``
      default so the CLI works without the deploy.sh env-passing
      layer when invoked manually.
    - ``GITEA_HOST`` — SSH host alias (default ``nexus``)

    **stdout** (eval-able):

    - ``WOODPECKER_GITEA_CLIENT=<id>``
    - ``WOODPECKER_GITEA_SECRET=<secret>``

    Both lines emitted only when the create succeeds. On failure
    (rc=1), only a stderr diagnostic is emitted; deploy.sh's eval
    sees nothing new and the existing ``.env`` values stay (which
    will be either empty on first run or stale from a prior run).

    Exit codes:

    - 0: created — both env-var lines on stdout, ready to ``eval``
    - 1: partial — list/delete/create REST failure with rotation
      NOT started (Gitea state still consistent with the existing
      Woodpecker .env). Deploy continues without rotating.
    - 2: hard failure — bad args, missing required env, invalid
      ADMIN_USERNAME, SSH tunnel failure, transport/unexpected
      exception, OR rotation half-complete (delete ACK'd or
      possibly applied but create failed; Woodpecker would 401
      until next successful deploy if we continued). Abort.
    """
    if args:
        print(f"gitea woodpecker-oauth: unknown args {args!r}", file=sys.stderr)
        return 2

    domain = os.environ.get("DOMAIN") or ""
    gitea_token = os.environ.get("GITEA_TOKEN") or ""
    admin_username = os.environ.get("ADMIN_USERNAME") or "admin"
    ssh_host = os.environ.get("GITEA_HOST") or "nexus"

    missing: list[str] = []
    if not domain:
        missing.append("DOMAIN")
    if not gitea_token:
        missing.append("GITEA_TOKEN")
    if missing:
        print(
            f"gitea woodpecker-oauth: missing required env: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    try:
        # Inside the try-block (Copilot R4): _allocate_free_port can
        # raise OSError on ephemeral-port exhaustion. Without this
        # guard the traceback escapes instead of converting to rc=2.
        local_port = _allocate_free_port()
        with (
            SSHClient(ssh_host) as ssh,
            ssh.port_forward(local_port, "localhost", 3200) as port,
        ):
            _ = ssh  # tunnel kept alive for the with-block
            result, error, rotation_started = run_woodpecker_oauth_setup(
                base_url=f"http://localhost:{port}",
                domain=domain,
                gitea_token=gitea_token,
                admin_username=admin_username,
            )
    except SSHError as exc:
        print(f"gitea woodpecker-oauth: ssh tunnel failed: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"gitea woodpecker-oauth: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except GiteaError as exc:
        # Path-safety violations (unsafe admin_username) and other
        # input-validation failures inside run_woodpecker_oauth_setup
        # surface as GiteaError. Their messages are constructed from
        # fixed format strings + operator-controlled identifiers
        # (no secrets), so safe to surface verbatim. (Copilot R5 —
        # the previous catch-all collapsed these to "unexpected
        # error (GiteaError)" which lost the actionable detail.)
        print(f"gitea woodpecker-oauth: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"gitea woodpecker-oauth: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    if result is None:
        # ``error`` is constructed in :func:`run_woodpecker_oauth_setup`
        # from GiteaError format strings only (HTTP status codes,
        # type names) — never from ``gitea_token``. CodeQL's taint
        # analysis can't prove that and surfaces the line as
        # ``py/clear-text-logging-sensitive-data``. Alert dismissed
        # as "won't fix" with the same rationale (see PR #521).
        sys.stderr.write(f"  • woodpecker-oauth: NOT created — {error}\n")
        # Half-completed rotation = MUST abort. The delete already
        # invalidated the previous client_secret; if we returned
        # rc=1 (yellow warn, deploy continues), Woodpecker would
        # keep running with the now-stale secret in its .env and
        # 401 on every Gitea login until the next deploy succeeds.
        # rc=2 routes to deploy.sh's red-abort branch. (Copilot R2)
        if rotation_started:
            sys.stderr.write(
                "  • rotation half-complete — old creds invalidated, "
                "no fresh ones issued; aborting to avoid a Woodpecker login outage\n",
            )
            return 2
        return 1

    sys.stderr.write("  • woodpecker-oauth: created (fresh client_id + secret)\n")

    import shlex as _shlex

    # Eval-able stdout handoff to deploy.sh — same intentional pattern
    # as ``GITEA_TOKEN=`` in :func:`_gitea_configure`. ``shlex.quote``
    # guarantees the value can't break out of the assignment if it
    # ever contains shell metacharacters; deploy.sh writes the
    # eval'd values into Woodpecker's ``.env`` (mode 600) before
    # ``docker compose up -d``. CodeQL flags the secret-bearing line
    # because ``client_secret`` matches its sensitive-name classifier;
    # alert dismissed as "won't fix" — the eval-handoff is the
    # documented contract, mitigated by tempfile mode 600 +
    # ``$RUNNER_CLEANUP_PATHS`` trap-driven cleanup on deploy.sh side.
    sys.stdout.write(f"WOODPECKER_GITEA_CLIENT={_shlex.quote(result.client_id)}\n")
    sys.stdout.write(f"WOODPECKER_GITEA_SECRET={_shlex.quote(result.client_secret)}\n")
    return 0


def _gitea_mirror_setup(args: list[str]) -> int:
    """`nexus-deploy gitea mirror-setup`.

    Provisions GH_MIRROR_REPOS as Gitea pull-mirrors plus per-user
    forks (Modul 2.2f part 2). Mirrors deploy.sh's mirror-loop block
    (L3224-3412 pre-migration); per-mirror operations:

    1. Migrate (clone-mirror via Gitea's /api/v1/repos/migrate +
       GitHub PAT) — idempotent: already_exists is soft-success
    2. On the FIRST mirror with a configured user: fork into the
       user's namespace via temp user-token (created+deleted
       around the fork POST)
    3. Add the user as read-collaborator on every mirror
    4. On the first iteration where a fork was created/exists:
       trigger mirror-sync + merge-upstream so the fork is
       fast-forwarded from upstream

    Required env:

    - ``GITEA_TOKEN`` — admin's bearer token for migrate / collab /
      mirror-sync (from earlier ``gitea configure`` invocation)
    - ``GH_MIRROR_REPOS`` — comma-separated GitHub repo URLs
    - ``GH_MIRROR_TOKEN`` — GitHub PAT (Contents:read for private
      sources)

    Conditionally required env:

    - ``GITEA_ADMIN_PASS`` — admin password (basic-auth for the
      temp user-token mint inside the fork flow). Required ONLY
      when ``GITEA_USER_USERNAME`` is set; mirrors-only mode
      (no user, no fork) doesn't need it. (Copilot R6)

    Optional env:

    - ``ADMIN_USERNAME`` — admin username, path-validated (default
      ``admin``). Mirrors :class:`NexusConfig`'s ``admin_username``
      default so the CLI works without the deploy.sh env-passing
      layer when invoked manually. (Same default as
      ``gitea woodpecker-oauth`` — Copilot R1 consistency fix.)
    - ``GITEA_USER_USERNAME`` — Gitea username for the per-user fork.
      If unset, the fork step is skipped (mirrors-only mode);
      ``GITEA_ADMIN_PASS`` becomes optional in this case.
    - ``WORKSPACE_BRANCH`` — branch for the merge-upstream step
      (default ``main``). deploy.sh resolves this from the GitHub
      API ahead of time and exports it.
    - ``GITEA_HOST`` — SSH host alias (default ``nexus``)

    **stdout** (eval-able, only when fork was created/exists):

    - ``FORK_NAME=<name>``
    - ``GITEA_REPO_OWNER=<user>``

    These two are consumed by deploy.sh's existing
    ``seed_workspace_files`` wrapper (already migrated, #512) so the
    seed POST hits the user's fork rather than the per-iteration
    mirror name. When no fork was created/exists, no stdout output
    is emitted.

    Exit codes:

    - 0: every mirror succeeded (created or already_exists), fork
      (if attempted) succeeded too
    - 1: at least one mirror failed OR fork failed. Deploy.sh keeps
      going (next spin-up retries; mirrors are idempotent).
    - 2: bad args / missing required env / SSH tunnel / unexpected
      exception. Abort.
    """
    if args:
        print(f"gitea mirror-setup: unknown args {args!r}", file=sys.stderr)
        return 2

    admin_username = os.environ.get("ADMIN_USERNAME") or "admin"
    admin_password = os.environ.get("GITEA_ADMIN_PASS") or ""
    gitea_token = os.environ.get("GITEA_TOKEN") or ""
    gh_mirror_repos_csv = os.environ.get("GH_MIRROR_REPOS") or ""
    gh_mirror_token = os.environ.get("GH_MIRROR_TOKEN") or ""
    gitea_user_username = os.environ.get("GITEA_USER_USERNAME") or None
    workspace_branch = os.environ.get("WORKSPACE_BRANCH") or "main"
    ssh_host = os.environ.get("GITEA_HOST") or "nexus"

    missing: list[str] = []
    if not gitea_token:
        missing.append("GITEA_TOKEN")
    if not gh_mirror_repos_csv:
        missing.append("GH_MIRROR_REPOS")
    if not gh_mirror_token:
        missing.append("GH_MIRROR_TOKEN")
    # GITEA_ADMIN_PASS is only consumed by the fork flow's temp
    # user-token mint (basic-auth: admin acts on behalf of user).
    # Mirrors-only mode (no GITEA_USER_USERNAME) doesn't need it.
    # (Copilot R6)
    if gitea_user_username and not admin_password:
        missing.append("GITEA_ADMIN_PASS (required when GITEA_USER_USERNAME is set)")
    if missing:
        print(
            f"gitea mirror-setup: missing required env: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    repos = [s.strip() for s in gh_mirror_repos_csv.split(",") if s.strip()]
    if not repos:
        print("gitea mirror-setup: GH_MIRROR_REPOS contained no repo URLs", file=sys.stderr)
        return 2

    try:
        local_port = _allocate_free_port()
        with (
            SSHClient(ssh_host) as ssh,
            ssh.port_forward(local_port, "localhost", 3200) as port,
        ):
            _ = ssh
            result = run_mirror_setup(
                base_url=f"http://localhost:{port}",
                admin_username=admin_username,
                admin_password=admin_password,
                gitea_token=gitea_token,
                gitea_user_username=gitea_user_username,
                gh_mirror_repos=repos,
                gh_mirror_token=gh_mirror_token,
                workspace_branch=workspace_branch,
            )
    except SSHError as exc:
        print(f"gitea mirror-setup: ssh tunnel failed: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"gitea mirror-setup: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except GiteaError as exc:
        # Path-safety violations + REST-layer errors not caught by
        # the orchestrator's per-call try/except. Surface verbatim
        # (messages are constructed from format strings only).
        print(f"gitea mirror-setup: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"gitea mirror-setup: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    # Per-step status lines on stderr.
    if result.admin_uid is None:
        # Distinguish "admin user genuinely doesn't exist (404)" from
        # auth/transport/5xx failures via admin_uid_error. Without
        # this, the message was misleadingly the same for all paths.
        # (Copilot R4)
        if result.admin_uid_error:
            sys.stderr.write(
                f"  • admin UID lookup failed ({result.admin_uid_error}) — skipping all mirrors\n"
            )
        else:
            sys.stderr.write("  • admin user not found in Gitea — skipping all mirrors\n")
        return 1
    sys.stderr.write(f"  • admin UID: {result.admin_uid}\n")
    for m in result.mirrors:
        sys.stderr.write(
            f"  • mirror: {m.name} → {m.status}{(' — ' + m.detail) if m.detail else ''}\n"
        )
    if result.fork is not None:
        sys.stderr.write(
            f"  • fork: {result.fork.owner}/{result.fork.name} → {result.fork.status}"
            f"{(' — ' + result.fork.detail) if result.fork.detail else ''}\n"
        )
    if result.collaborator_added_count > 0:
        sys.stderr.write(f"  • collaborator added on {result.collaborator_added_count} mirror(s)\n")
    if result.fork_synced:
        sys.stderr.write("  • fork merge-upstream attempted\n")

    # Eval-able stdout: emit FORK_NAME + GITEA_REPO_OWNER iff the
    # fork is in a usable state. seed_workspace_files (deploy.sh side)
    # uses these to point its seed POST at the user's fork rather
    # than the most recently-mutated $REPO_NAME from the legacy
    # mirror loop.
    import shlex as _shlex

    if result.fork is not None and result.fork.status in ("created", "already_exists"):
        sys.stdout.write(f"FORK_NAME={_shlex.quote(result.fork.name)}\n")
        sys.stdout.write(f"GITEA_REPO_OWNER={_shlex.quote(result.fork.owner)}\n")

    return 0 if result.is_success else 1


def _stack_sync(args: list[str]) -> int:
    """`nexus-deploy stack-sync --enabled <comma-list> [--stacks-dir PATH]`.

    Replaces deploy.sh L1401-1444 (#505 Modul 3.3): per-stack rsync of
    ``stacks/<svc>/`` → ``nexus:/opt/docker-server/stacks/<svc>/``,
    plus disabled-stack cleanup (server-side ``docker compose down``
    + ``rm -rf`` for any folder NOT in the enabled list).

    The comma-list maps directly to deploy.sh's ``$ENABLED_SERVICES``
    (``tr '\\n ' ',,'`` already happens on the bash side, same wire-
    format as compose_runner).

    Optional ``--stacks-dir`` defaults to ``stacks`` relative to the
    repo root — exposed for tests. Production callers leave it off.

    Exit codes:

    - 0: every enabled service was either rsynced successfully or
      reported missing-local (deploy.sh's pre-migration warning
      branch, kept as soft-success); the cleanup script ran and
      returned RESULT with failed=0.
    - 1: at least one rsync failed OR the cleanup loop reported
      ``failed > 0``, but at least one operation succeeded. deploy.sh:
      yellow warning, continue.
    - 2: bad args, transport (ssh/rsync) failure, no parseable RESULT
      line, or unexpected exception. deploy.sh: red, abort.
    """
    enabled_str: str | None = None
    stacks_dir_arg: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--enabled":
            if i + 1 >= len(args):
                print("stack-sync: --enabled requires a value", file=sys.stderr)
                return 2
            enabled_str = args[i + 1]
            i += 2
        elif args[i] == "--stacks-dir":
            if i + 1 >= len(args):
                print("stack-sync: --stacks-dir requires a value", file=sys.stderr)
                return 2
            stacks_dir_arg = args[i + 1]
            i += 2
        else:
            print(f"stack-sync: unknown arg {args[i]!r}", file=sys.stderr)
            return 2

    if enabled_str is None:
        print(
            "stack-sync: --enabled <comma-separated-services> is required",
            file=sys.stderr,
        )
        return 2

    enabled = [s.strip() for s in enabled_str.split(",") if s.strip()]
    if not enabled:
        # Empty list: nothing to rsync, but the cleanup loop still
        # has work — every existing folder on the server is "not in
        # the enabled list" and gets removed. That matches deploy.sh's
        # behaviour: a deploy with zero enabled services would tear
        # down every stack on the server.
        pass

    stacks_dir = Path(stacks_dir_arg) if stacks_dir_arg else Path("stacks")
    if not stacks_dir.is_dir():
        print(
            f"stack-sync: stacks dir {stacks_dir!s} is not a directory",
            file=sys.stderr,
        )
        return 2

    try:
        result = run_stack_sync(stacks_dir, enabled)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"stack-sync: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        # Force rc=2 (Python's default rc=1 collides with our
        # partial-failure semantic). Class name only — no str/repr,
        # which could embed secret-bearing values from a future
        # config-aware helper.
        print(f"stack-sync: unexpected error ({type(exc).__name__})", file=sys.stderr)
        return 2

    # Per-service rsync diagnostics on stderr — same pattern as the
    # cleanup script (which streams its own diagnostics). On rsync
    # failure we ALSO surface the captured stderr excerpt as an
    # indented block — Round-2 PR #523 finding: a bare "rc=23"
    # gave operators no actionable signal, the underlying rsync
    # error message ("Permission denied", "No space left on device",
    # "ssh: connect to host nexus port 22: Connection refused", etc.)
    # is what they need to see.
    for r in result.rsync:
        if r.status == "synced":
            sys.stderr.write(f"  ✓ {r.service} synced\n")
        elif r.status == "missing-local":
            sys.stderr.write(f"  ⚠ {r.service}: local stack folder not found - skipping\n")
        else:
            detail = f" ({r.detail})" if r.detail else ""
            sys.stderr.write(f"  ✗ {r.service} rsync failed{detail}\n")
            if r.stderr_excerpt:
                for line in r.stderr_excerpt.splitlines():
                    sys.stderr.write(f"      {line}\n")

    cleanup_summary = (
        f"stopped={result.cleanup.stopped} removed={result.cleanup.removed} "
        f"failed={result.cleanup.failed}"
        if result.cleanup is not None
        else "stopped=? removed=? failed=? (cleanup did not return RESULT)"
    )
    print(
        f"stack-sync: synced={result.synced} missing={result.missing} "
        f"failed_rsync={result.failed_rsync} cleanup: {cleanup_summary}",
    )

    if result.cleanup is None:
        # No parseable RESULT: hard failure (same defensive parse as
        # compose_runner / seeder).
        return 2
    if result.is_success:
        return 0
    # Partial: at least one rsync OR cleanup failure. Distinguish
    # "everything failed" (rc=2) from "some succeeded" (rc=1).
    if result.synced == 0 and result.cleanup.stopped + result.cleanup.removed == 0:
        return 2
    return 1


def _setup_ssh_config(args: list[str]) -> int:
    """`nexus-deploy setup ssh-config`.

    Replaces deploy.sh L173-231 (#505 Modul 3.4a). Renders the
    ``Host nexus`` block in ``~/.ssh/config`` with the Cloudflare
    Access ProxyCommand. Atomic write, mode 0o600.

    Required env: ``SSH_HOST`` (the tunnel hostname),
    ``CF_ACCESS_CLIENT_ID``, ``CF_ACCESS_CLIENT_SECRET``.

    Aborts (rc=2) when either Service Token component is missing —
    browser-login fallback is impossible in CI.

    Exit codes:
    - 0: ssh-config block written
    - 2: missing required env, missing Service Token, or write failure
    """
    if args:
        print(f"setup ssh-config: unknown args {args!r}", file=sys.stderr)
        return 2
    ssh_host = os.environ.get("SSH_HOST", "").strip()
    cf_id = os.environ.get("CF_ACCESS_CLIENT_ID", "").strip() or None
    cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "").strip() or None
    if not ssh_host:
        print("setup ssh-config: SSH_HOST env var required", file=sys.stderr)
        return 2
    spec = SSHConfigSpec(ssh_host=ssh_host, cf_client_id=cf_id, cf_client_secret=cf_secret)
    try:
        configure_ssh(spec)
    except SetupError as exc:
        print(f"setup ssh-config: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # Filesystem error (permission, disk full, etc.). Class name
        # only so a future bug embedding secrets in the path doesn't
        # leak into the deploy log.
        print(
            f"setup ssh-config: write failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"setup ssh-config: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    auth_mode = "Service Token" if spec.has_service_token else "browser login"
    print(f"setup ssh-config: wrote Host {spec.host_alias} block (auth={auth_mode})")
    return 0


def _setup_wait_ssh(args: list[str]) -> int:
    """`nexus-deploy setup wait-ssh`.

    Replaces deploy.sh L236-377 (#505 Modul 3.4a). Polls
    Cloudflare-Access-tunneled SSH until the host accepts a
    ``BatchMode=yes`` connection.

    When ``CF_ACCESS_CLIENT_ID`` + ``CF_ACCESS_CLIENT_SECRET`` are
    set in the environment, we do the Service Token propagation
    wait first (linear backoff 5/10/15/20/25s after a 10s initial
    sleep). Then the standard SSH-readiness loop (15 retries,
    exponential timeout).

    Optional env: ``SSH_HOST_ALIAS`` (default ``nexus``),
    ``CF_ACCESS_CLIENT_ID``, ``CF_ACCESS_CLIENT_SECRET``.

    Exit codes:
    - 0: SSH connection established
    - 2: max retries exhausted (Token-test OR readiness loop)
    """
    if args:
        print(f"setup wait-ssh: unknown args {args!r}", file=sys.stderr)
        return 2
    host_alias = os.environ.get("SSH_HOST_ALIAS") or "nexus"
    has_token = bool(os.environ.get("CF_ACCESS_CLIENT_ID")) and bool(
        os.environ.get("CF_ACCESS_CLIENT_SECRET"),
    )
    if has_token:
        sys.stderr.write("  Testing Service Token authentication...\n")
        token_result = wait_for_service_token(host_alias=host_alias)
        if not token_result.succeeded:
            sys.stderr.write(
                f"  ✗ Service Token authentication failed after {token_result.attempts} attempts\n",
            )
            if token_result.last_error:
                for line in token_result.last_error.splitlines():
                    sys.stderr.write(f"      {line}\n")
            return 2
        sys.stderr.write(
            f"  ✓ Service Token authentication successful (attempt {token_result.attempts})\n",
        )

    sys.stderr.write("  Waiting for SSH via Cloudflare Tunnel...\n")
    ssh_result = wait_for_ssh(host_alias=host_alias)
    if not ssh_result.succeeded:
        sys.stderr.write(
            f"  ✗ SSH connection failed after {ssh_result.attempts} attempts\n",
        )
        if ssh_result.last_error:
            for line in ssh_result.last_error.splitlines():
                sys.stderr.write(f"      {line}\n")
        return 2
    print(
        f"setup wait-ssh: SSH connection established (attempt {ssh_result.attempts})",
    )
    return 0


def _setup_ensure_jq(args: list[str]) -> int:
    """`nexus-deploy setup ensure-jq`.

    Replaces deploy.sh L391-398 (#505 Modul 3.4a). Idempotent
    ``apt-get install -y jq`` on the remote — bootstrap for VMs
    that pre-date the cloud-init jq install.

    Optional env: ``SSH_HOST_ALIAS`` (default ``nexus``).

    Exit codes:
    - 0: jq present (already-installed or newly-installed)
    - 2: install failed (transport, sudo permission, dpkg lock, etc.)
    """
    if args:
        print(f"setup ensure-jq: unknown args {args!r}", file=sys.stderr)
        return 2
    host_alias = os.environ.get("SSH_HOST_ALIAS") or "nexus"
    try:
        with SSHClient(host_alias) as ssh:
            installed = ensure_jq(ssh)
    except subprocess.CalledProcessError as exc:
        # Round-5 PR #524: jq install failures are usually NOT transport
        # (apt repo down, dpkg lock, missing sudo) — labelling them as
        # such misleads operators. Plus the captured remote output (in
        # exc.output thanks to ssh.run's stdout=PIPE+merge_stderr=True
        # default) carries the actionable error message but was being
        # silently dropped. Now: distinct label + truncated tail
        # forwarded to local stderr. exc.cmd is NOT echoed (defence in
        # depth: a future bug embedding secrets in argv shouldn't leak).
        print(
            f"setup ensure-jq: remote command failed (rc={exc.returncode})",
            file=sys.stderr,
        )
        if exc.output:
            excerpt = exc.output[-2000:].rstrip()
            for line in excerpt.splitlines():
                sys.stderr.write(f"      {line}\n")
        return 2
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"setup ensure-jq: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"setup ensure-jq: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    if installed:
        print("setup ensure-jq: jq newly installed")
    else:
        print("setup ensure-jq: jq already present")
    return 0


def _setup_mount_volume(args: list[str]) -> int:
    """`nexus-deploy setup mount-volume`.

    Replaces deploy.sh L403-459 (#505 Modul 3.4a). Mounts the
    Hetzner persistent volume at ``/mnt/nexus-data`` with three-stage
    device discovery (scsi-id → automount → /dev/sdb fallback) and
    idempotent fstab entry.

    Required env: ``PERSISTENT_VOLUME_ID`` (Hetzner volume ID, or
    ``0`` / empty to skip).
    Optional env: ``SSH_HOST_ALIAS`` (default ``nexus``).

    Exit codes:
    - 0: mounted, OR skipped (volume_id empty/0), OR already-mounted
    - 1: every device-discovery fallback failed (deploy continues —
         downstream stacks that don't need the volume can still come
         up healthy; operator gets a yellow warning)
    - 2: invalid volume_id, transport failure, unexpected exception
    """
    if args:
        print(f"setup mount-volume: unknown args {args!r}", file=sys.stderr)
        return 2
    volume_id = os.environ.get("PERSISTENT_VOLUME_ID", "").strip()
    host_alias = os.environ.get("SSH_HOST_ALIAS") or "nexus"
    try:
        with SSHClient(host_alias) as ssh:
            result = mount_persistent_volume(volume_id, ssh)
    except SetupError as exc:
        print(f"setup mount-volume: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        # Round-5 PR #524: mount-volume failures are usually
        # remote-script issues (mount permission denied, missing
        # mount utility, fstab parse error), not transport. The
        # rendered script contains no secrets — volume_id is
        # validated as digits-only upstream, every other shell
        # token is hardcoded — so forwarding the captured tail is
        # safe and operationally useful. exc.cmd NOT echoed
        # (defence in depth).
        print(
            f"setup mount-volume: remote script failed (rc={exc.returncode})",
            file=sys.stderr,
        )
        if exc.output:
            excerpt = exc.output[-2000:].rstrip()
            for line in excerpt.splitlines():
                sys.stderr.write(f"      {line}\n")
        return 2
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"setup mount-volume: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"setup mount-volume: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    if result.detail == "skipped":
        print("setup mount-volume: skipped (no PERSISTENT_VOLUME_ID)")
        return 0
    if result.mounted:
        fstab = " (fstab updated)" if result.fstab_added else ""
        print(f"setup mount-volume: mounted{fstab}")
        return 0
    # Every fallback failed — yellow warning, deploy continues.
    print(
        f"setup mount-volume: fallback-failed ({result.detail}); "
        "deploy continues but stacks needing the volume may fail",
    )
    return 1


def _setup_wetty_ssh_agent(args: list[str]) -> int:
    """`nexus-deploy setup wetty-ssh-agent`.

    Replaces deploy.sh:439-540 (the ``[5.5/7]`` block). Renders +
    runs a server-side bash that:

    1. ssh-keygen the wetty key pair (idempotent — only if absent).
    2. Append the public key to ``authorized_keys`` (idempotent).
    3. Start ``ssh-agent`` with a known socket path (handles
       dead-socket cleanup if the agent crashed previously).
    4. ssh-add the key to the agent (idempotent — fingerprint check).
    5. Write ``SSH_AUTH_SOCK=`` to ``stacks/wetty/.env``.

    Optional env: ``SSH_HOST_ALIAS`` (default ``nexus``).

    Exit codes:
    - 0: all 5 steps completed (whether they were no-ops or made changes)
         AND the .env file was written (i.e. ``auth_sock_written=1``).
         A no-op idempotent run is still rc=0 because the .env append
         is unconditional on the happy path.
    - 1: soft failure — either (a) the script ran but emitted no
         parseable RESULT, or (b) ``auth_sock_written=0`` (the fail-fast
         paths in render_wetty_agent_script emit a parseable
         all-zero RESULT line, so the absence of the .env write is a
         real failure even though the script returned 0). Deploy
         continues since Wetty is non-critical, but the operator sees
         the forwarded stderr.
    - 2: hard transport / unexpected error
    """
    if args:
        print(f"setup wetty-ssh-agent: unknown args {args!r}", file=sys.stderr)
        return 2
    host_alias = os.environ.get("SSH_HOST_ALIAS") or "nexus"
    try:
        with SSHClient(host_alias) as ssh:
            result = setup_wetty_ssh_agent(ssh)
    except subprocess.CalledProcessError as exc:
        # Same defence-in-depth as setup ensure-jq: forward the
        # captured tail to local stderr but DON'T print exc.cmd.
        print(
            f"setup wetty-ssh-agent: remote command failed (rc={exc.returncode})",
            file=sys.stderr,
        )
        if exc.output:
            excerpt = exc.output[-2000:].rstrip()
            for line in excerpt.splitlines():
                sys.stderr.write(f"      {line}\n")
        return 2
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"setup wetty-ssh-agent: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"setup wetty-ssh-agent: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    if result is None:
        print(
            "setup wetty-ssh-agent: script ran but produced no RESULT_WETTY line",
            file=sys.stderr,
        )
        return 1
    # Per-step summary on stdout (workflow log) — same one-line shape
    # as the other setup CLIs.
    parts = []
    if result.keypair_generated:
        parts.append("key-generated")
    if result.pubkey_added:
        parts.append("pubkey-added")
    if result.agent_started:
        parts.append("agent-started")
    if result.key_added_to_agent:
        parts.append("key-added")
    if result.auth_sock_written:
        parts.append("env-written")
    summary = "+".join(parts) if parts else "all-noop"
    print(f"setup wetty-ssh-agent: {summary}")
    # auth_sock_written=0 means render_wetty_agent_script's fail-fast
    # paths fired (ssh-agent unresponsive OR sed/printf to .env failed).
    # Surface as rc=1 so the workflow log shows the soft-fail signal —
    # deploy.sh continues since Wetty is non-critical but the operator
    # sees that the agent socket isn't actually plumbed through.
    if not result.auth_sock_written:
        print(
            "setup wetty-ssh-agent: soft-fail — SSH_AUTH_SOCK not written "
            "to wetty/.env (Wetty container won't see agent socket)",
            file=sys.stderr,
        )
        return 1
    return 0


def _setup(args: list[str]) -> int:
    """Dispatch ``nexus-deploy setup <subcommand>``."""
    if not args:
        print(
            "setup: subcommand required (ssh-config | wait-ssh | ensure-jq "
            "| mount-volume | wetty-ssh-agent)",
            file=sys.stderr,
        )
        return 2
    sub = args[0]
    rest = args[1:]
    if sub == "ssh-config":
        return _setup_ssh_config(rest)
    if sub == "wait-ssh":
        return _setup_wait_ssh(rest)
    if sub == "ensure-jq":
        return _setup_ensure_jq(rest)
    if sub == "mount-volume":
        return _setup_mount_volume(rest)
    if sub == "wetty-ssh-agent":
        return _setup_wetty_ssh_agent(rest)
    print(f"setup: unknown subcommand {sub!r}", file=sys.stderr)
    return 2


def _service_env(args: list[str]) -> int:
    """`nexus-deploy service-env --enabled <csv> [--stacks-dir PATH]`.

    Replaces deploy.sh L233-1170 (#505 Modul 3.4c). Reads
    ``SECRETS_JSON`` from stdin + ``BootstrapEnv`` fields from
    environment variables, renders the per-service ``.env`` files
    for every enabled service, optionally appends the Gitea
    workspace block to git-integrated stacks (jupyter / marimo /
    code-server / meltano / prefect) when Gitea is enabled and
    the workspace-repo coordinates are provided via env-vars.

    Required env: ``DOMAIN``, ``ADMIN_EMAIL``.
    Optional env (drives the Gitea workspace append):
    ``GITEA_REPO_URL``, ``GITEA_USERNAME``, ``GITEA_PASSWORD``,
    ``GIT_AUTHOR_NAME``, ``GIT_AUTHOR_EMAIL``, ``REPO_NAME``.
    Optional env (BootstrapEnv): ``GITEA_USER_EMAIL``, ``GITEA_USER_USERNAME``,
    ``GITEA_REPO_OWNER``, ``OM_PRINCIPAL_DOMAIN``, ``WOODPECKER_GITEA_CLIENT``,
    ``WOODPECKER_GITEA_SECRET``, ``SSH_KEY_BASE64``.

    Exit codes:
    - 0: every enabled spec rendered (or skipped per its guard)
    - 1: at least one render failed but at least one succeeded
    - 2: hard failure (SFTPGo password missing, write error,
         unexpected exception)
    """
    enabled_str: str | None = None
    stacks_dir_arg: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--enabled":
            if i + 1 >= len(args):
                print("service-env: --enabled requires a value", file=sys.stderr)
                return 2
            enabled_str = args[i + 1]
            i += 2
        elif args[i] == "--stacks-dir":
            if i + 1 >= len(args):
                print("service-env: --stacks-dir requires a value", file=sys.stderr)
                return 2
            stacks_dir_arg = args[i + 1]
            i += 2
        else:
            print(f"service-env: unknown arg {args[i]!r}", file=sys.stderr)
            return 2
    if enabled_str is None:
        print(
            "service-env: --enabled <comma-separated-services> is required",
            file=sys.stderr,
        )
        return 2
    enabled = [s.strip() for s in enabled_str.split(",") if s.strip()]
    stacks_dir = Path(stacks_dir_arg) if stacks_dir_arg else Path("stacks")
    if not stacks_dir.is_dir():
        print(
            f"service-env: stacks dir {stacks_dir!s} is not a directory",
            file=sys.stderr,
        )
        return 2

    try:
        config = NexusConfig.from_secrets_json(sys.stdin.read())
    except ConfigError as exc:
        print(f"service-env: {exc}", file=sys.stderr)
        return 2
    missing = [
        name for name in ("DOMAIN", "ADMIN_EMAIL") if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        print(
            f"service-env: missing required env vars: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2
    bootstrap_env = BootstrapEnv(
        domain=os.environ.get("DOMAIN") or None,
        admin_email=os.environ.get("ADMIN_EMAIL") or None,
        gitea_user_email=os.environ.get("GITEA_USER_EMAIL") or None,
        gitea_user_username=os.environ.get("GITEA_USER_USERNAME") or None,
        gitea_repo_owner=os.environ.get("GITEA_REPO_OWNER") or None,
        repo_name=os.environ.get("REPO_NAME") or None,
        om_principal_domain=os.environ.get("OM_PRINCIPAL_DOMAIN") or None,
        woodpecker_gitea_client=os.environ.get("WOODPECKER_GITEA_CLIENT") or None,
        woodpecker_gitea_secret=os.environ.get("WOODPECKER_GITEA_SECRET") or None,
        ssh_private_key_base64=os.environ.get("SSH_KEY_BASE64") or None,
    )

    try:
        result = render_all_env_files(config, bootstrap_env, enabled, stacks_dir=stacks_dir)
    except ServiceEnvError as exc:
        print(f"service-env: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"service-env: unexpected error ({type(exc).__name__})", file=sys.stderr)
        return 2

    # Per-service stderr log so operators see what was rendered.
    for r in result.services:
        if r.status == "rendered":
            sys.stderr.write(f"  ✓ {r.service}\n")
        elif r.status == "skipped-not-enabled":
            pass  # too noisy to log every disabled service
        elif r.status == "skipped-guard":
            sys.stderr.write(f"  ⚠ {r.service}: skipped ({r.detail})\n")
        else:
            sys.stderr.write(f"  ✗ {r.service}: {r.detail}\n")

    # Optional: append Gitea workspace block. Driven by env-vars
    # — deploy.sh's bash side derives these from mirror/non-mirror
    # logic; we just consume them when present.
    gitea_repo_url = os.environ.get("GITEA_REPO_URL") or ""
    gitea_username = os.environ.get("GITEA_USERNAME") or ""
    gitea_password = os.environ.get("GITEA_PASSWORD") or ""
    git_author_name = os.environ.get("GIT_AUTHOR_NAME") or ""
    git_author_email = os.environ.get("GIT_AUTHOR_EMAIL") or ""
    repo_name = os.environ.get("REPO_NAME") or ""
    # Require the full set of workspace coords before appending the
    # block — a partial set would write a broken .env (empty
    # PASSWORD or author fields) that's harder to diagnose than a
    # missing block. deploy.sh's bash derives all six in lockstep,
    # so requiring all of them here just hardens against direct
    # CLI invocation with partial env-vars.
    workspace_coords_complete = all(
        (
            gitea_repo_url,
            gitea_username,
            gitea_password,
            git_author_name,
            git_author_email,
            repo_name,
        ),
    )
    if workspace_coords_complete and "gitea" in enabled:
        cfg = GiteaWorkspaceConfig(
            gitea_repo_url=gitea_repo_url,
            gitea_username=gitea_username,
            gitea_password=gitea_password,
            git_author_name=git_author_name,
            git_author_email=git_author_email,
            repo_name=repo_name,
        )
        appended = append_gitea_workspace_block(cfg, enabled, stacks_dir=stacks_dir)
        for svc in appended:
            sys.stderr.write(f"  ✓ {svc} Gitea workspace block appended\n")

    print(
        f"service-env: rendered={result.rendered} skipped={result.skipped} failed={result.failed}",
    )
    if result.failed > 0:
        if result.rendered == 0:
            return 2
        return 1
    return 0


def _r2_tokens(args: list[str]) -> int:
    """`nexus-deploy r2-tokens <list|cleanup>`.

    Audit + reconciliation utility for Cloudflare R2 user API tokens.
    Surfaces the 50-token-per-account hard cap and lets operators
    proactively delete orphan ``nexus-r2-*`` tokens left behind by
    earlier destroy/setup cycles (see #530 for the bug history).

    Subcommands:

    - ``list``: dry-run inventory. Prints account-wide token total +
      remaining slots + the matched ``nexus-r2-*`` subset. Always
      exit 0; deploy.sh / cron can scrape the output.
    - ``cleanup --name <name>``: delete every token whose name equals
      <name>. Used by re-setup to ensure no orphan exists before
      ``init-r2-state.sh`` mints a fresh token.
    - ``cleanup --prefix <prefix>``: delete every token whose name
      starts with <prefix>. Refuses unless prefix begins with
      ``nexus-r2-`` (defence-in-depth: prevents wiping the
      ``Nexus-Stack`` / ``Nexus2`` / build tokens documented as
      protected in CLAUDE.md).

    Required env: ``TF_VAR_cloudflare_api_token`` (or
    ``CLOUDFLARE_API_TOKEN``).

    Exit codes:
    - 0: ``list`` always returns 0; ``cleanup`` returns 0 only when
         every matched token deleted successfully (or dry-run with no
         per-token attempts). Backed by ``CleanupResult.is_success``.
    - 1: ``cleanup`` completed but at least one per-token delete
         failed (the loop continues — every attempt is reported in
         stdout — but the rc reflects the partial-failure so callers
         like deploy.sh / a follow-up cron run can re-attempt).
    - 2: bad args / missing env / network error / API listing failed
         / safety guard hit (e.g. ``--prefix`` doesn't start with
         ``nexus-r2-``).
    """
    if not args:
        print(
            "r2-tokens: subcommand required (list | cleanup --name|--prefix VALUE [--apply])",
            file=sys.stderr,
        )
        return 2

    # Tofu convention is lowercase TF_VAR_*; the upper-case alias is
    # the more common dotenv style. SIM112 wants UPPERCASE only — but
    # the lowercase form is the one Tofu / our setup-control-plane
    # workflow already exports. Honor both with a noqa so SIM112's
    # blanket rule doesn't conflict with the established convention.
    api_token = (
        os.environ.get("TF_VAR_cloudflare_api_token")  # noqa: SIM112
        or os.environ.get("CLOUDFLARE_API_TOKEN")
        or ""
    ).strip()
    if not api_token:
        print(
            "r2-tokens: TF_VAR_cloudflare_api_token (or CLOUDFLARE_API_TOKEN) required",
            file=sys.stderr,
        )
        return 2

    sub = args[0]
    rest = args[1:]

    if sub == "list":
        list_prefix = DEFAULT_NEXUS_R2_PREFIX
        i = 0
        while i < len(rest):
            if rest[i] == "--prefix":
                if i + 1 >= len(rest):
                    print("r2-tokens list: --prefix requires a value", file=sys.stderr)
                    return 2
                list_prefix = rest[i + 1]
                i += 2
            else:
                print(f"r2-tokens list: unknown arg {rest[i]!r}", file=sys.stderr)
                return 2
        try:
            inventory = build_inventory(api_token=api_token, prefix=list_prefix)
        except (RuntimeError, requests.RequestException) as exc:
            print(f"r2-tokens list: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(
            f"r2-tokens list: total={inventory.total} / 50  "
            f"remaining={inventory.remaining_slots}  "
            f"prefix={list_prefix!r}  matched={len(inventory.matched)}",
        )
        if inventory.near_cap:
            sys.stderr.write(
                f"  ⚠ Approaching the 50-token cap (remaining={inventory.remaining_slots})\n",
            )
        for token in inventory.matched:
            issued = token.issued_on or "?"
            print(f"  {token.id}  {issued}  {token.name}")
        return 0

    if sub == "cleanup":
        name: str | None = None
        prefix: str | None = None
        apply_changes = False
        i = 0
        while i < len(rest):
            if rest[i] == "--name":
                if i + 1 >= len(rest):
                    print("r2-tokens cleanup: --name requires a value", file=sys.stderr)
                    return 2
                name = rest[i + 1]
                i += 2
            elif rest[i] == "--prefix":
                if i + 1 >= len(rest):
                    print("r2-tokens cleanup: --prefix requires a value", file=sys.stderr)
                    return 2
                prefix = rest[i + 1]
                i += 2
            elif rest[i] == "--apply":
                apply_changes = True
                i += 1
            else:
                print(f"r2-tokens cleanup: unknown arg {rest[i]!r}", file=sys.stderr)
                return 2
        if (name is None) == (prefix is None):
            print(
                "r2-tokens cleanup: pass exactly one of --name VALUE or --prefix VALUE",
                file=sys.stderr,
            )
            return 2
        try:
            result = cleanup_orphan_tokens(
                api_token=api_token,
                name=name,
                prefix=prefix,
                dry_run=not apply_changes,
            )
        except ValueError as exc:
            # Validation error (e.g. prefix doesn't start with nexus-r2-).
            print(f"r2-tokens cleanup: {exc}", file=sys.stderr)
            return 2
        except (RuntimeError, requests.RequestException) as exc:
            print(f"r2-tokens cleanup: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(
            f"r2-tokens cleanup: total_before={result.total_tokens_before}  "
            f"matched={len(result.matched)}  "
            f"deleted={result.deleted_count}  failed={result.failed_count}  "
            f"dry_run={result.dry_run}",
        )
        for token in result.matched:
            issued = token.issued_on or "?"
            print(f"  matched: {token.id}  {issued}  {token.name}")
        for d in result.deletions:
            status = "OK" if d.deleted else f"FAILED ({d.error})"
            print(f"  delete: {d.id}  {d.name}  {status}")
        if not apply_changes:
            sys.stderr.write(
                "  (dry-run; pass --apply to actually delete)\n",
            )
        return 0 if result.is_success else 1

    print(f"r2-tokens: unknown subcommand {sub!r}", file=sys.stderr)
    return 2


def _run_all(args: list[str]) -> int:
    """`nexus-deploy run-all`.

    Replaces deploy.sh's eval-handoff dance (#505 Modul 3.4b) — calls
    all migrated module functions in sequence with in-process state
    handoff, then emits 3 values to stdout (eval-able by surviving
    deploy.sh bash):

    - ``RESTART_SERVICES=<csv>`` — bash compose-restart loop
    - ``WOODPECKER_GITEA_CLIENT=<id>`` — written into stacks/woodpecker/.env
    - ``WOODPECKER_GITEA_SECRET=<secret>`` — written into stacks/woodpecker/.env

    Other state (GITEA_TOKEN, FORK_NAME, FORK_OWNER) is consumed
    entirely inside the orchestrator and never exits Python.

    Required env: ``ADMIN_EMAIL``, ``REPO_NAME``, ``GITEA_REPO_OWNER``,
    ``ENABLED_SERVICES``, ``DOMAIN``, ``PROJECT_ID``, ``INFISICAL_TOKEN``.
    Optional env: ``WORKSPACE_BRANCH`` (default ``main``),
    ``GH_MIRROR_REPOS``, ``GH_MIRROR_TOKEN``, ``GITEA_USER_USERNAME``,
    ``GITEA_USER_EMAIL``, ``GITEA_USER_PASS``, ``OM_PRINCIPAL_DOMAIN``,
    ``INFISICAL_ENV`` (default ``dev``), ``SSH_HOST_ALIAS`` (default ``nexus``).

    Exit codes:
    - 0: every phase ok or skipped
    - 1: at least one phase produced status='partial'
    - 2: at least one phase failed (orchestrator aborted)
    """
    if args:
        print(f"run-all: unknown args {args!r}", file=sys.stderr)
        return 2

    admin_email = os.environ.get("ADMIN_EMAIL") or ""
    repo_name = os.environ.get("REPO_NAME") or ""
    gitea_repo_owner = os.environ.get("GITEA_REPO_OWNER") or ""
    enabled_str = os.environ.get("ENABLED_SERVICES") or ""
    domain = os.environ.get("DOMAIN") or ""
    project_id = os.environ.get("PROJECT_ID") or ""
    infisical_token = os.environ.get("INFISICAL_TOKEN") or ""

    missing = [
        name
        for name, val in (
            ("ADMIN_EMAIL", admin_email),
            ("REPO_NAME", repo_name),
            ("GITEA_REPO_OWNER", gitea_repo_owner),
            ("ENABLED_SERVICES", enabled_str),
            ("DOMAIN", domain),
            ("PROJECT_ID", project_id),
            ("INFISICAL_TOKEN", infisical_token),
        )
        if not val
    ]
    if missing:
        print(f"run-all: missing required env: {', '.join(missing)}", file=sys.stderr)
        return 2

    enabled = [s.strip() for s in enabled_str.replace(",", " ").split() if s.strip()]
    workspace_branch = os.environ.get("WORKSPACE_BRANCH") or "main"
    gh_mirror_repos_csv = os.environ.get("GH_MIRROR_REPOS") or ""
    gh_mirror_token = os.environ.get("GH_MIRROR_TOKEN") or None
    gitea_user_username = os.environ.get("GITEA_USER_USERNAME") or None
    gitea_user_email = os.environ.get("GITEA_USER_EMAIL") or None
    gitea_user_password = os.environ.get("GITEA_USER_PASS") or None
    ssh_host = os.environ.get("SSH_HOST_ALIAS") or "nexus"
    infisical_env = os.environ.get("INFISICAL_ENV") or "dev"
    gh_mirror_repos = [s.strip() for s in gh_mirror_repos_csv.split(",") if s.strip()]

    try:
        config = NexusConfig.from_secrets_json(sys.stdin.read())
    except ConfigError as exc:
        print(f"run-all: {exc}", file=sys.stderr)
        return 2
    bootstrap_env = BootstrapEnv(
        domain=domain,
        admin_email=admin_email,
        gitea_user_email=gitea_user_email,
        gitea_user_username=gitea_user_username,
        gitea_repo_owner=gitea_repo_owner,
        repo_name=repo_name,
        om_principal_domain=os.environ.get("OM_PRINCIPAL_DOMAIN") or None,
        woodpecker_gitea_client=os.environ.get("WOODPECKER_GITEA_CLIENT") or None,
        woodpecker_gitea_secret=os.environ.get("WOODPECKER_GITEA_SECRET") or None,
        ssh_private_key_base64=os.environ.get("SSH_KEY_BASE64") or None,
    )

    orchestrator = Orchestrator(
        config=config,
        bootstrap_env=bootstrap_env,
        enabled_services=enabled,
        repo_name=repo_name,
        gitea_repo_owner=gitea_repo_owner,
        workspace_branch=workspace_branch,
        gh_mirror_repos=gh_mirror_repos,
        gh_mirror_token=gh_mirror_token,
        gitea_user_username=gitea_user_username,
        gitea_user_email=gitea_user_email,
        gitea_user_password=gitea_user_password,
        ssh_host=ssh_host,
        project_id=project_id,
        infisical_token=infisical_token,
        infisical_env=infisical_env,
    )

    try:
        result = orchestrator.run_all()
    except SSHError as exc:
        print(f"run-all: ssh setup failed: {exc}", file=sys.stderr)
        return 2
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(
            f"run-all: transport failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"run-all: unexpected error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    # Per-phase log to stderr.
    for phase in result.phases:
        marker = {"ok": "✓", "partial": "⚠", "failed": "✗", "skipped": "—"}.get(phase.status, "?")
        detail = f" — {phase.detail}" if phase.detail else ""
        sys.stderr.write(f"  {marker} {phase.name}: {phase.status}{detail}\n")

    # Eval-able stdout: 3 values for the surviving deploy.sh bash.
    import shlex as _shlex

    # Always emit all 3 lines so a previous run's shell vars don't
    # leak into the next deploy via `eval` reading a stale value
    # when a phase skipped or failed early.
    sys.stdout.write(
        f"RESTART_SERVICES={_shlex.quote(','.join(result.state.restart_services))}\n",
    )
    sys.stdout.write(
        f"WOODPECKER_GITEA_CLIENT={_shlex.quote(result.state.woodpecker_client_id or '')}\n",
    )
    sys.stdout.write(
        f"WOODPECKER_GITEA_SECRET={_shlex.quote(result.state.woodpecker_client_secret or '')}\n",
    )

    if result.has_hard_failure:
        return 2
    if result.has_partial:
        return 1
    return 0


def _allocate_free_port() -> int:
    """Ask the kernel for a free IPv4 ephemeral port on the loopback.

    Bind a socket to ``127.0.0.1:0`` (kernel picks free), record the
    assigned port, immediately close. The returned port is then handed
    to ``ssh -L 127.0.0.1:<port>:…`` to re-bind. Race window between
    close and ssh-rebind is microseconds; for production-deploy-
    frequency that's fine. If a future contributor needs zero-race,
    paramiko's port-forward has a callback API but we explicitly chose
    subprocess + system ssh in Modul 3.1, so this is the right
    primitive for now.

    Note: IPv4-only by design. ``ssh.SSHClient.port_forward`` matches
    by passing the explicit ``127.0.0.1:`` bind address — without it,
    ssh on a dual-stack host would also bind ``::1`` and a port that
    looked free here could still be taken on IPv6, causing intermittent
    ExitOnForwardFailure aborts (round-4 PR #517 finding).
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


# Hints emitted alongside execution_state in stderr — actionable
# replacements for the bare-enum output. Mirrors deploy.sh's
# per-case warnings (L3464 cron-tick / L3489 seed-not-visible /
# L3493 open-execution-in-UI / L3496 didn't-complete).
_KESTRA_EXECUTION_HINTS: dict[str, str] = {
    "SUCCESS": "",
    "FAILED": "open the execution in the Kestra UI for the error log",
    "KILLED": "open the execution in the Kestra UI for the error log",
    "RUNNING": "did not complete within the timeout — first regular cron tick will retry within 15 min",
    "CREATED": "execution stuck in CREATED state — check Kestra worker logs",
    "UNKNOWN": "execution state could not be determined — check Kestra UI",
    "TRIGGER_FAILED": "could not trigger execution — first sync will run on the next 15-min cron tick",
    "SEED_FLOW_MISSING": "system.flow-sync ran but the seeded flow is not visible — "
    "check that nexus_seeds/kestra/flows/r2-taxi-pipeline.yaml is in the workspace repo "
    "and re-execute system.flow-sync from the Kestra UI",
}


def _kestra_execution_hint(state: str) -> str:
    """Return the actionable warning string for a given ExecutionState."""
    return _KESTRA_EXECUTION_HINTS.get(state, "")


def main() -> int:
    """Subcommand dispatcher. Shipped subcommands by phase:

    - Phase 1: ``config dump-shell``, ``infisical bootstrap``,
      ``secret-sync``
    - Phase 2: ``seed``, ``compose up``, ``services configure``,
      ``kestra register-system-flows``

    More land as the migration progresses; see #505.
    """
    args = sys.argv[1:]
    if args == ["--version"]:
        print(__version__)
        return 0
    if args in ([], ["hello"]):
        print(hello())
        return 0
    if args[:2] == ["config", "dump-shell"]:
        return _config_dump_shell(args[2:])
    if args[:2] == ["infisical", "bootstrap"]:
        return _infisical_bootstrap(args[2:])
    if args[:2] == ["infisical", "provision-admin"]:
        return _infisical_provision_admin(args[2:])
    if args[:1] == ["secret-sync"]:
        return _secret_sync(args[1:])
    if args[:1] == ["seed"]:
        return _seed(args[1:])
    if args[:1] == ["compose"]:
        return _compose_up(args[1:])
    if args[:1] == ["services"]:
        return _services_configure(args[1:])
    if args[:2] == ["kestra", "register-system-flows"]:
        return _kestra_register_system_flows(args[2:])
    if args[:2] == ["gitea", "configure"]:
        return _gitea_configure(args[2:])
    if args[:2] == ["gitea", "woodpecker-oauth"]:
        return _gitea_woodpecker_oauth(args[2:])
    if args[:2] == ["gitea", "mirror-setup"]:
        return _gitea_mirror_setup(args[2:])
    if args[:1] == ["stack-sync"]:
        return _stack_sync(args[1:])
    if args[:1] == ["setup"]:
        return _setup(args[1:])
    if args[:1] == ["service-env"]:
        return _service_env(args[1:])
    if args[:1] == ["run-all"]:
        return _run_all(args[1:])
    if args[:1] == ["r2-tokens"]:
        return _r2_tokens(args[1:])
    print(
        f"nexus_deploy {__version__}: unknown command {' '.join(args)!r}",
        file=sys.stderr,
    )
    print(
        "Available: --version, hello, "
        "config dump-shell [--tofu-dir PATH (default: tofu/stack) | --stdin], "
        "infisical bootstrap (reads SECRETS_JSON from stdin + env vars), "
        "secret-sync --stack <jupyter|marimo>, "
        "seed --repo <owner>/<name> [--root PATH] [--prefix nexus_seeds/], "
        "compose up --enabled <comma-list>, "
        "services configure --enabled <comma-list> (reads SECRETS_JSON from stdin), "
        "kestra register-system-flows (reads SECRETS_JSON from stdin + env vars), "
        "gitea configure (reads SECRETS_JSON from stdin + env vars; emits eval-able stdout), "
        "gitea woodpecker-oauth (env-only; emits WOODPECKER_GITEA_CLIENT + WOODPECKER_GITEA_SECRET), "
        "gitea mirror-setup (env-only; emits FORK_NAME + GITEA_REPO_OWNER iff a fork was provisioned), "
        "stack-sync --enabled <comma-list> [--stacks-dir PATH], "
        "setup ssh-config | wait-ssh | ensure-jq | mount-volume, "
        "service-env --enabled <comma-list> [--stacks-dir PATH] (reads SECRETS_JSON from stdin), "
        "run-all (reads SECRETS_JSON from stdin + env vars; emits eval-able stdout: "
        "RESTART_SERVICES + WOODPECKER_GITEA_CLIENT + WOODPECKER_GITEA_SECRET)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
