"""OpenTofu CLI wrapper for nexus_deploy (Phase 3 Modul 3.2, #505).

Thin typed wrapper around ``tofu output``. Replaces deploy.sh's
ad-hoc ``$(cd "$TOFU_DIR" && tofu output -json X 2>/dev/null || echo
"{}")`` pattern (8 call-sites in deploy.sh) with a single class whose
fallback behavior is explicit per-call.

``tofu apply`` is intentionally NOT wrapped here — that lands with the
orchestrator (Modul 3.4) so the streaming-output and per-stage logging
concerns live next to where they're consumed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Final, overload

# Sentinel for "default not supplied" — distinguishes "caller passed
# default=None" (use None on failure) from "caller passed nothing"
# (raise TofuError on failure). Plain ``None`` won't do; the legacy
# bash uses an empty-string default in some places and json `{}` /
# `0` in others, all of which are valid user-supplied defaults.
_MISSING: Final = object()


class TofuError(Exception):
    """Raised when ``tofu output`` fails AND no default was supplied."""


class TofuRunner:
    """Run ``tofu output`` in a fixed working directory.

    The default ``tofu_dir`` matches deploy.sh's ``$TOFU_DIR``
    (``tofu/stack``). Pass an explicit path for tests or when wrapping
    the secondary ``tofu/control-plane`` state.
    """

    def __init__(self, tofu_dir: Path = Path("tofu/stack")) -> None:
        self.tofu_dir = tofu_dir

    @overload
    def output_raw(self, name: str) -> str: ...
    @overload
    def output_raw(self, name: str, *, default: str) -> str: ...

    def output_raw(self, name: str, *, default: Any = _MISSING) -> str:
        """``tofu output -raw <name>``.

        Mirror of deploy.sh's ``tofu output -raw <name> 2>/dev/null ||
        echo "<fallback>"`` pattern. Pass ``default=""`` for the silent
        fallback semantic; omit it to make a missing/erroring output
        raise :class:`TofuError`.
        """
        try:
            completed = subprocess.run(
                ["tofu", "output", "-raw", name],
                cwd=self.tofu_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            if default is _MISSING:
                raise TofuError(f"tofu output -raw {name} failed in {self.tofu_dir}") from exc
            return str(default)
        return completed.stdout

    @overload
    def output_json(self, name: str) -> Any: ...
    @overload
    def output_json(self, name: str, *, default: Any) -> Any: ...

    def output_json(self, name: str, *, default: Any = _MISSING) -> Any:
        """``tofu output -json <name>``, parsed.

        Three failure modes are collapsed into ``default`` when
        provided: tofu binary missing, tofu exited non-zero, stdout
        not valid JSON. Without ``default`` any of those raise
        :class:`TofuError`.
        """
        try:
            completed = subprocess.run(
                ["tofu", "output", "-json", name],
                cwd=self.tofu_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            if default is _MISSING:
                raise TofuError(f"tofu output -json {name} failed in {self.tofu_dir}") from exc
            return default
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            if default is _MISSING:
                raise TofuError(f"tofu output -json {name} returned non-JSON stdout") from exc
            return default
