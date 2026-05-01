"""nexus_deploy — Python orchestration for Nexus-Stack deployment.

This package is being built incrementally to replace `scripts/deploy.sh`.
See issue #505 for the migration plan. Until Phase 4 lands, the bash
deploy.sh remains the entry point and shells out to
`python -m nexus_deploy <command>` for migrated functionality.
"""

__version__ = "0.1.0"


def hello() -> str:
    """Phase 0 smoke-test target — proves the package imports + CI runs.

    Replace this with real entry points in Phase 1 (`infisical`,
    `secret-sync` CLI commands).
    """
    return "nexus_deploy phase-0 ready"
