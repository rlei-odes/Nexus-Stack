"""CLI dispatch — Phase 0 stub, Phase 1 plugs in real subcommands.

Imported by the `[project.scripts]` entry in pyproject.toml so
`uv sync` exposes a `nexus-deploy` shell command equivalent to
`python -m nexus_deploy`.
"""

from __future__ import annotations

from nexus_deploy.__main__ import main as _main


def main() -> int:
    """Re-export of `__main__.main` for the console-script entry point."""
    return _main()
