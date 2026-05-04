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
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
)
from nexus_deploy.kestra import run_register_system_flows
from nexus_deploy.secret_sync import StackTarget, run_sync_for_stack
from nexus_deploy.seeder import _is_safe_repo_path, run_seed_for_repo
from nexus_deploy.services import run_admin_setups
from nexus_deploy.ssh import SSHClient, SSHError


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


_VALID_STACKS = ("jupyter", "marimo")


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

    - ``GITEA_ADMIN_PASS`` — admin password (basic-auth for the
      temp user-token mint inside the fork flow)
    - ``GITEA_TOKEN`` — admin's bearer token for migrate / collab /
      mirror-sync (from earlier ``gitea configure`` invocation)
    - ``GH_MIRROR_REPOS`` — comma-separated GitHub repo URLs
    - ``GH_MIRROR_TOKEN`` — GitHub PAT (Contents:read for private
      sources)

    Optional env:

    - ``ADMIN_USERNAME`` — admin username, path-validated (default
      ``admin``). Mirrors :class:`NexusConfig`'s ``admin_username``
      default so the CLI works without the deploy.sh env-passing
      layer when invoked manually. (Same default as
      ``gitea woodpecker-oauth`` — Copilot R1 consistency fix.)
    - ``GITEA_USER_USERNAME`` — Gitea username for the per-user fork.
      If unset, the fork step is skipped (mirrors-only mode).
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
    if not admin_password:
        missing.append("GITEA_ADMIN_PASS")
    if not gitea_token:
        missing.append("GITEA_TOKEN")
    if not gh_mirror_repos_csv:
        missing.append("GH_MIRROR_REPOS")
    if not gh_mirror_token:
        missing.append("GH_MIRROR_TOKEN")
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
        sys.stderr.write("  • admin UID not found — skipping all mirrors\n")
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
        "gitea mirror-setup (env-only; emits FORK_NAME + GITEA_REPO_OWNER iff a fork was provisioned)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
