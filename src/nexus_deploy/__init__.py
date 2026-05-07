"""nexus_deploy — Python orchestration for Nexus-Stack deployment.

The single entrypoint is ``python -m nexus_deploy run-pipeline``,
invoked by ``.github/workflows/spin-up.yml``. See
``docs/admin-guides/migration-to-python.md`` for historical context
on how this package came to be.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nexus-deploy")
except PackageNotFoundError:  # pragma: no cover
    # Package isn't installed (e.g. running from a source checkout without
    # `uv sync`); keep a non-empty fallback so callers always get a string.
    __version__ = "0.0.0+unknown"


def hello() -> str:
    """Smoke-test target — proves the package imports + CI runs.

    Kept as the bare ``python -m nexus_deploy`` (no subcommand)
    response so ``hello world``-style sanity checks have a fast,
    side-effect-free path. Real work goes through the subcommand
    dispatcher in :mod:`nexus_deploy.__main__`.
    """
    return "nexus_deploy ready"
