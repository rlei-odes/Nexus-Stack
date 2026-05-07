"""Cloudflare R2 user-token inventory + cleanup.

Audits and reconciles Cloudflare R2 user API tokens — the
:func:`build_inventory` helper paginates ``/user/tokens`` and
returns a typed view of the account's tokens; :func:`cleanup_orphan_tokens`
deletes orphan ``nexus-r2-*`` tokens left behind by earlier
destroy/setup cycles. Two reasons this lives here as a proper module
instead of a one-off bash script (issue #530):

1. The existing R2-token bug (`init-r2-state.sh` losing tokens past
   page 1 of the unpaginated `/user/tokens` listing) was caused by
   silent edge-cases in shell + curl + jq. Surfacing the same logic
   as a typed function with unit tests makes those edge-cases
   testable and protected against future regressions.

2. The migration's overall direction is bash → Python; introducing a
   brand-new utility as bash would just create more legacy to migrate
   later.

The Cloudflare User-Token API is account-wide; the token used here is
the same `TF_VAR_cloudflare_api_token` the Tofu/init-r2-state.sh
flows use. Hard cap: 50 tokens per Cloudflare account, account-wide.

Public surface:

* :func:`list_user_tokens` — paginated retrieval of every user token,
  with `?per_page=100` (Cloudflare's max).
* :func:`find_tokens_by_name` / :func:`find_tokens_by_prefix` —
  pure-python filtering against a token list.
* :func:`delete_token` — single-id delete, returns
  :class:`DeleteResult`.
* :func:`cleanup_orphan_tokens` — list + filter + delete in one call,
  returns a :class:`CleanupResult` aggregating per-token outcomes.

The CLI surfaces these via ``nexus-deploy r2-tokens list`` and
``nexus-deploy r2-tokens cleanup``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import requests

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
USER_TOKENS_PATH = "/user/tokens"
ACCOUNT_TOKEN_HARD_CAP = 50  # Cloudflare account-wide limit
DEFAULT_NEXUS_R2_PREFIX = "nexus-r2-"


@dataclass(frozen=True)
class TokenInfo:
    """Subset of the Cloudflare token JSON we care about.

    Cloudflare returns more fields (status, condition, last_used_on,
    expires_on, policies …) — we keep the minimum needed for the
    cleanup-by-name/prefix operations + the operator-facing inventory.
    Skip-empty silently on missing fields so a future Cloudflare API
    addition doesn't break parsing.
    """

    id: str
    name: str
    issued_on: str = ""  # ISO8601, may be missing on older tokens

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> TokenInfo:
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            issued_on=str(raw.get("issued_on") or ""),
        )


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of a single token-delete API call."""

    id: str
    name: str
    deleted: bool
    error: str = ""

    @property
    def is_success(self) -> bool:
        return self.deleted


@dataclass(frozen=True)
class CleanupResult:
    """Aggregate result from :func:`cleanup_orphan_tokens`."""

    total_tokens_before: int
    matched: tuple[TokenInfo, ...]
    deletions: tuple[DeleteResult, ...]
    dry_run: bool = False

    @property
    def deleted_count(self) -> int:
        return sum(1 for d in self.deletions if d.deleted)

    @property
    def failed_count(self) -> int:
        return sum(1 for d in self.deletions if not d.deleted)

    @property
    def is_success(self) -> bool:
        return self.failed_count == 0


@dataclass(frozen=True)
class TokenInventory:
    """Tokens snapshot + counters used by the audit ("list") path.

    ``total`` is the total count account-wide (across all prefixes);
    ``matched`` is the subset matching the inventory's filter prefix.
    ``remaining_slots`` is the account-wide free slots before the
    50-token hard cap kicks in — surface this in the operator log so
    we notice approaching the cap before a re-setup fails.
    """

    total: int
    matched: tuple[TokenInfo, ...]
    prefix: str = ""

    @property
    def remaining_slots(self) -> int:
        return max(0, ACCOUNT_TOKEN_HARD_CAP - self.total)

    @property
    def near_cap(self) -> bool:
        """True when fewer than 5 slots remain (matches the 'Should'
        criterion in the bug report — the cron worker should warn
        before we hit the wall)."""
        return self.remaining_slots < 5


# Sentinel type for the ``client`` test seam. Callers in production
# leave ``client=None`` and get the standard ``requests`` module;
# tests pass a ``MagicMock`` (or a custom stub) that implements
# ``.get()`` / ``.delete()`` with the same shape as requests' API.
HttpClient = Any


def _default_client() -> HttpClient:
    """Production HTTP client. Module-level ``requests`` reference;
    the indirection exists so tests can swap in a mock without
    monkey-patching the requests module globally."""
    return requests


def list_user_tokens(
    *,
    api_token: str,
    base_url: str = CLOUDFLARE_API_BASE,
    per_page: int = 100,
    client: HttpClient | None = None,
    timeout_s: float = 15.0,
) -> list[TokenInfo]:
    """Fetch every Cloudflare user API token, walking ``result_info.total_pages``.

    The 50-token cap means a single ``per_page=100`` request always
    returns the full list — but we still paginate defensively so the
    function stays correct if Cloudflare ever raises the cap or if
    a future caller bypasses it for a different account.

    Raises ``requests.HTTPError`` on non-2xx responses (no-retry — this
    is a one-shot inventory, not a retry surface). Per-token
    parsing is lenient: unknown-shape entries silently skip.
    """
    http = client if client is not None else _default_client()
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    tokens: list[TokenInfo] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        resp = http.get(
            f"{base_url}{USER_TOKENS_PATH}",
            params={"per_page": per_page, "page": page},
            headers=headers,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            errs = body.get("errors") or []
            msg = errs[0].get("message") if errs else "unknown"
            raise RuntimeError(f"Cloudflare /user/tokens failed: {msg}")
        for raw in body.get("result") or []:
            if isinstance(raw, dict):
                tokens.append(TokenInfo.from_api(cast("dict[str, Any]", raw)))
        info = body.get("result_info") or {}
        total_pages = int(info.get("total_pages") or 1)
        page += 1
    return tokens


def find_tokens_by_name(tokens: list[TokenInfo], name: str) -> list[TokenInfo]:
    """ALL tokens with exact-name match. Returns multiple entries if
    the account somehow ended up with duplicates — this is the case
    we want to clean up after the bug report's orphan scenario."""
    return [t for t in tokens if t.name == name]


def find_tokens_by_prefix(tokens: list[TokenInfo], prefix: str) -> list[TokenInfo]:
    """All tokens whose name starts with ``prefix``."""
    return [t for t in tokens if t.name.startswith(prefix)]


def delete_token(
    token_id: str,
    *,
    api_token: str,
    name: str = "",
    base_url: str = CLOUDFLARE_API_BASE,
    client: HttpClient | None = None,
    timeout_s: float = 15.0,
) -> DeleteResult:
    """Delete a single token by id. ``name`` is purely cosmetic
    (carried into the result for log lines); the API only needs the id.

    Returns a :class:`DeleteResult` with ``deleted=True`` on
    ``success: true`` from Cloudflare; otherwise extracts the first
    error message and surfaces it via ``error``. Network errors
    propagate as ``requests.RequestException`` (caller decides whether
    to abort the whole cleanup or continue with the next id)."""
    http = client if client is not None else _default_client()
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    resp = http.delete(
        f"{base_url}{USER_TOKENS_PATH}/{token_id}",
        headers=headers,
        timeout=timeout_s,
    )
    body: dict[str, Any]
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if body.get("success"):
        return DeleteResult(id=token_id, name=name, deleted=True)
    errs = body.get("errors") or []
    msg = (errs[0] or {}).get("message") if errs else f"HTTP {resp.status_code}"
    return DeleteResult(id=token_id, name=name, deleted=False, error=str(msg))


def cleanup_orphan_tokens(
    *,
    api_token: str,
    name: str | None = None,
    prefix: str | None = None,
    dry_run: bool = True,
    base_url: str = CLOUDFLARE_API_BASE,
    client: HttpClient | None = None,
) -> CleanupResult:
    """One-shot reconciliation: list + filter + delete.

    Exactly one of ``name`` / ``prefix`` must be set. ``dry_run=True``
    (the default) just lists; ``dry_run=False`` calls
    :func:`delete_token` for each match. Per-id delete failures are
    recorded in :class:`CleanupResult.deletions` but don't stop
    the loop — the operator sees per-token outcomes in the audit log.

    **Safety invariant**: ``prefix`` MUST start with ``nexus-r2-``.
    Without this guard, an operator typo could wipe the protected
    ``Nexus-Stack`` / ``Nexus2`` / ``Nexus-Stack Template`` /
    ``nexus-stack-ch build token`` entries documented in CLAUDE.md.
    The function raises ``ValueError`` on a non-conforming prefix
    rather than silently broadening the scope.
    """
    if (name is None) == (prefix is None):
        raise ValueError("cleanup_orphan_tokens: pass exactly one of name= / prefix=")
    if prefix is not None and not prefix.startswith(DEFAULT_NEXUS_R2_PREFIX):
        raise ValueError(
            f"cleanup_orphan_tokens: prefix={prefix!r} must start with "
            f"{DEFAULT_NEXUS_R2_PREFIX!r} — refusing to broaden scope to "
            "tokens outside the nexus-r2-* family (protected per CLAUDE.md)",
        )
    tokens = list_user_tokens(api_token=api_token, base_url=base_url, client=client)
    if name is not None:
        matched = find_tokens_by_name(tokens, name)
    else:
        # `prefix` is guaranteed non-None here by the XOR check above; cast
        # for mypy strictness (Optional[str] → str).
        matched = find_tokens_by_prefix(tokens, cast("str", prefix))
    if dry_run:
        return CleanupResult(
            total_tokens_before=len(tokens),
            matched=tuple(matched),
            deletions=(),
            dry_run=True,
        )
    deletions: list[DeleteResult] = []
    for t in matched:
        deletions.append(
            delete_token(
                t.id,
                api_token=api_token,
                name=t.name,
                base_url=base_url,
                client=client,
            ),
        )
    return CleanupResult(
        total_tokens_before=len(tokens),
        matched=tuple(matched),
        deletions=tuple(deletions),
        dry_run=False,
    )


def build_inventory(
    *,
    api_token: str,
    prefix: str = DEFAULT_NEXUS_R2_PREFIX,
    base_url: str = CLOUDFLARE_API_BASE,
    client: HttpClient | None = None,
) -> TokenInventory:
    """Audit shape: full account count + filtered subset + remaining
    slots. Backs the ``nexus-deploy r2-tokens list`` CLI and the
    optional admin-status panel called out in the bug report."""
    tokens = list_user_tokens(api_token=api_token, base_url=base_url, client=client)
    matched = find_tokens_by_prefix(tokens, prefix)
    return TokenInventory(total=len(tokens), matched=tuple(matched), prefix=prefix)


__all__ = [
    "ACCOUNT_TOKEN_HARD_CAP",
    "CLOUDFLARE_API_BASE",
    "DEFAULT_NEXUS_R2_PREFIX",
    "CleanupResult",
    "DeleteResult",
    "TokenInfo",
    "TokenInventory",
    "build_inventory",
    "cleanup_orphan_tokens",
    "delete_token",
    "find_tokens_by_name",
    "find_tokens_by_prefix",
    "list_user_tokens",
]


# Defensive: reference the unused-on-this-path field annotation so
# mypy --strict doesn't flag the dataclass(field) helper as unused.
_ = field
