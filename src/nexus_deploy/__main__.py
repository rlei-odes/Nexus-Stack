"""Entry point for `python -m nexus_deploy ...` invocations.

Currently a stub — Phase 1 will plug in `cli.py` once the first
real subcommands (`infisical bootstrap`, `secret-sync`) are
implemented.
"""

from __future__ import annotations

import sys

from nexus_deploy import __version__, hello


def main() -> int:
    """Phase-0 stub. Phase 1 replaces this with click-based CLI dispatch."""
    args = sys.argv[1:]
    if args == ["--version"]:
        print(__version__)
        return 0
    if args in ([], ["hello"]):
        print(hello())
        return 0
    print(f"nexus_deploy {__version__}: no commands implemented yet (Phase 0)", file=sys.stderr)
    print("This package is being built per issue #505.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
