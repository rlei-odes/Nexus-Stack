"""CLI dispatch — thin re-export of ``__main__.main``.

Imported by the ``[project.scripts]`` entry in ``pyproject.toml``
so ``uv sync`` exposes a ``nexus-deploy`` shell command equivalent
to ``python -m nexus_deploy``. The actual subcommand routing lives
in :mod:`nexus_deploy.__main__`; this module exists only so the
console-script entry point has a stable import path that doesn't
read like ``__main__:main``.
"""

from __future__ import annotations

from nexus_deploy.__main__ import main as _main


def main() -> int:
    """Re-export of `__main__.main` for the console-script entry point."""
    return _main()
