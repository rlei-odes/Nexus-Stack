"""config.tfvars parser + Gitea identity derivation (Phase 4c, #505).

Replaces the ~50 LoC bash block in scripts/deploy.sh:60-99 that
greps + sed-extracts ``domain``, ``admin_email``, ``user_email`` from
``tofu/stack/config.tfvars`` and applies the admin-email collision
fallback. Pure logic + a single file read; no subprocess.

Public surface:

* :class:`TfvarsConfig` — frozen dataclass for the 3 raw fields.
* :class:`GiteaIdentity` — frozen dataclass for the 4 derived fields.
* :func:`parse` — read + regex-extract a config.tfvars file.
* :func:`derive_gitea_identity` — apply the collision-fallback logic.

Why a regex parser instead of a real HCL parser:

config.tfvars in this project always has the same shape — one
``var = "value"`` per line, double-quoted scalars, no heredocs / no
nested objects / no interpolation. A regex parser pinned to the
project's own format is simpler than pulling in `python-hcl2` (which
would add a transitive dependency and a parse-error surface for
syntax we don't actually support).

The regex is deliberately strict: it requires double-quoted values
and rejects multi-line / heredoc / unquoted-int forms. If a future
contributor introduces those forms, ``parse()`` silently does NOT
match the line — the corresponding key returns the dataclass default
(empty string). Downstream pipeline gates (e.g. the
"domain must be non-empty" check in ``run_pipeline``) catch the
empty-after-parse case with a clear error. Tests pin this
soft-skip behavior; if you need fail-fast on malformed forms,
add a post-parse validation in ``run_pipeline`` rather than
tightening the regex (a contributor swap to e.g. heredoc syntax
shouldn't break tfvars consumption everywhere). PR #535 R1 #3
corrected the docstring; the historical "fails fast" wording was
aspirational, not what the code actually does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class TfvarsError(Exception):
    """config.tfvars file missing, unreadable, or malformed."""


@dataclass(frozen=True)
class TfvarsConfig:
    """Raw values from config.tfvars.

    All three fields default to "" so a missing key in the file
    parses to an empty string rather than raising. The pipeline's
    own gates decide which fields are required (e.g. ``domain``
    must be non-empty; ``user_email`` is optional).
    """

    domain: str = ""
    admin_email_raw: str = ""
    user_email_raw: str = ""


@dataclass(frozen=True)
class GiteaIdentity:
    """Post-derivation identity values consumed by the orchestrator.

    All four fields are post-fallback / post-trim. ``admin_email``
    is the SYNTHETIC ``gitea-admin@<domain>`` form when the original
    admin_email was empty or collided with the user email — this
    avoids the Gitea uniqueness violation on user.email.
    """

    admin_email: str
    gitea_user_email: str
    gitea_user_username: str
    om_principal_domain: str


# Match a single key=value tfvars line where value is double-quoted.
# Anchored with ``re.MULTILINE`` so each ``finditer`` match is one
# physical line. Captures the quoted-string contents only — no
# escape-handling (the project's keys never contain `"` or `\`).
_TFVARS_LINE = re.compile(
    r'^\s*(?P<key>domain|admin_email|user_email)\s*=\s*"(?P<value>[^"\n\r]*)"\s*$',
    re.MULTILINE,
)


def parse(path: Path) -> TfvarsConfig:
    """Read + regex-extract domain / admin_email / user_email.

    Returns a :class:`TfvarsConfig` with empty strings for any keys
    that didn't appear in the file. Raises :class:`TfvarsError` on
    file-missing or unreadable; passes through OSError details.
    """
    if not path.is_file():
        raise TfvarsError(f"config.tfvars not found at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TfvarsError(f"could not read {path}: {type(exc).__name__}: {exc}") from exc

    parsed: dict[str, str] = {}
    for match in _TFVARS_LINE.finditer(text):
        parsed[match.group("key")] = match.group("value")

    return TfvarsConfig(
        domain=parsed.get("domain", ""),
        admin_email_raw=parsed.get("admin_email", ""),
        user_email_raw=parsed.get("user_email", ""),
    )


def _trim(value: str) -> str:
    """Whitespace-trim. Mirrors the legacy bash
    ``sed 's/^[[:space:]]*//; s/[[:space:]]*$//'`` (deploy.sh:80-81).

    Necessary because Gitea / Windmill / Wiki.js validators reject
    space-prefixed emails, and self-provisioned tfvars commonly have
    leading spaces inside the quoted value (especially when copy-
    pasted from a list).
    """
    return value.strip()


def derive_gitea_identity(config: TfvarsConfig) -> GiteaIdentity:
    """Apply the admin-email collision fallback.

    The legacy bash block (deploy.sh:80-99) splits user_email on the
    first comma (it may be a multi-admin list for the Cloudflare
    Access allow-list, but Gitea's user.email column accepts only
    one address), trims both, and then enforces the
    admin != user invariant with a synthetic
    ``gitea-admin@<domain>`` fallback.

    Reasons (in priority order) for falling back:
    1. ``admin_email`` is empty after trim — self-provisioned tfvars
       commonly omit it.
    2. ``admin_email`` equals ``gitea_user_email`` after trim — the
       admin-panel caller (Nexus-Stack-for-Education) passes both
       values from the same source field today.

    Without the fallback, Gitea's ``CREATE`` for the user row would
    fail with "e-mail already in use" because both Admin + User
    rows would share the same email. ``gitea-admin@<domain>`` is a
    local-part no human-email scheme would produce, so it's
    guaranteed distinct from any real USER_EMAIL.
    """
    admin_email_trimmed = _trim(config.admin_email_raw)
    # Take the first comma-entry, trim. Empty → empty (no fallback to
    # admin — that's deliberate; the Gitea user-create is gated on
    # GITEA_USER_EMAIL non-empty in the orchestrator's
    # workspace-coords phase, so an empty value cleanly skips user
    # creation instead of colliding with admin).
    first_user = config.user_email_raw.split(",", 1)[0]
    gitea_user_email = _trim(first_user)
    gitea_user_username = (
        gitea_user_email.split("@", 1)[0] if "@" in gitea_user_email else gitea_user_email
    )

    if not admin_email_trimmed or admin_email_trimmed == gitea_user_email:
        # Synthesize. Same shape the legacy bash uses.
        admin_email = f"gitea-admin@{config.domain}" if config.domain else ""
    else:
        admin_email = admin_email_trimmed

    om_principal_domain = admin_email.split("@", 1)[1] if "@" in admin_email else ""

    return GiteaIdentity(
        admin_email=admin_email,
        gitea_user_email=gitea_user_email,
        gitea_user_username=gitea_user_username,
        om_principal_domain=om_principal_domain,
    )
