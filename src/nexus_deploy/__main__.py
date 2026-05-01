"""Entry point for `python -m nexus_deploy ...` invocations.

Phase 1 dispatch surface. Subcommands land here as their modules ship.
Currently:
- ``config dump-shell`` (#505 Modul 1.3)
- ``infisical bootstrap`` (#505 Modul 1.1)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from nexus_deploy import __version__, hello
from nexus_deploy.config import ConfigError, NexusConfig
from nexus_deploy.infisical import (
    BootstrapEnv,
    InfisicalClient,
    compute_folders,
)


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
    variables. Computes the 41 folders, writes payloads, runs the
    server-side curl loop. Mirrors deploy.sh:1996-2390.

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
        print(
            f"infisical bootstrap: unexpected error ({type(exc).__name__}); "
            "see traceback above if any",
            file=sys.stderr,
        )
        return 2
    print(
        f"infisical bootstrap: built={result.folders_built} pushed={result.pushed} failed={result.failed}",
    )
    return 0 if result.failed == 0 else 1


def main() -> int:
    """Phase-1 dispatcher. ``config`` and ``infisical`` subcommands shipped."""
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
    print(
        f"nexus_deploy {__version__}: unknown command {' '.join(args)!r}",
        file=sys.stderr,
    )
    print(
        "Available: --version, hello, "
        "config dump-shell [--tofu-dir PATH (default: tofu/stack) | --stdin], "
        "infisical bootstrap (reads SECRETS_JSON from stdin + env vars)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
