"""Hetzner Cloud capacity-aware server selection (Issue #536).

The legacy deploy used a single (server_type, server_location) pair
and failed at ``tofu apply`` time when Hetzner had no stock for that
exact combination — common during the 2025/26 capacity crunches.
This module queries the Hetzner Cloud API BEFORE tofu runs and picks
the first available pair from an operator-provided preference list,
falling through to the next entry when the preferred one is sold
out.

Public surface:

* :class:`ServerSpec` — frozen ``(server_type, location)`` pair.
* :class:`HetznerCapacityError` — API/auth/network/schema failure.
* :func:`parse_preferences` — comma-list parser with validation.
* :func:`fetch_availability` — calls ``/v1/server_types`` +
  ``/v1/datacenters`` and returns ``location -> {available type names}``.
* :func:`select` — walk preferences in order, return first match.

Default preference list lives in :data:`DEFAULT_PREFERENCES` — used
when neither config.tfvars nor the workflow override provides one.

Why two API calls instead of one: ``/v1/datacenters`` returns
server-type IDs as integers; the per-stock list is keyed by ID, not
name. We resolve names→IDs via ``/v1/server_types`` once, then walk
the datacenters response. Both endpoints are stable and the round-
trip is small (a few KB each).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_API_BASE = "https://api.hetzner.cloud/v1"
_DEFAULT_TIMEOUT = 30.0

# Default preference list — picked in Issue #536:
#   1. cx43 (Intel-shared, project default since 2026-05) tried in
#      three EU regions before falling back to ccx33 (dedicated AMD,
#      same vCPU/RAM class but ~30% pricier).
#   2. Region order hel1 → fsn1 → nbg1 — matches the historical
#      project default ``server_location = "hel1"`` from
#      ``tofu/stack/variables.tf``, so a fresh install that doesn't
#      configure SERVER_PREFERENCES at all lands in the same region
#      as before #537. Falkenstein and Nuremberg follow as failovers.
#      (PR #537 R2 #2 — reordered so the built-in default doesn't
#      silently change the region for new installs.)
#   3. ARM (cax*) deliberately excluded — Hetzner ARM EU has been
#      chronically constrained and is no longer cheaper (per the
#      2026-05 note in CLAUDE.md).
DEFAULT_PREFERENCES = (
    "cx43:hel1",
    "cx43:fsn1",
    "cx43:nbg1",
    "ccx33:hel1",
    "ccx33:fsn1",
    "ccx33:nbg1",
)


class HetznerCapacityError(Exception):
    """Hetzner API call failed (auth / network / schema drift / timeout)."""


@dataclass(frozen=True)
class ServerSpec:
    """A ``(server_type, location)`` pair, normalised to lowercase.

    Hetzner's API returns names lowercase already (``cx43``, ``fsn1``);
    we normalise here so the in-memory match in :func:`select` is
    case-insensitive against operator input that may have a stray
    upper-case (e.g. ``CX43:FSN1`` from a copy-paste).
    """

    server_type: str
    location: str

    def __str__(self) -> str:
        return f"{self.server_type}:{self.location}"


def parse_preferences(value: str) -> tuple[ServerSpec, ...]:
    """Parse a comma-list of ``<server_type>:<location>`` tokens.

    Whitespace around tokens / inside the colon-separated halves is
    stripped. Empty tokens (e.g. trailing comma) are skipped. The
    result is a non-empty tuple in input order.

    Raises :class:`ValueError` on:

    * empty / whitespace-only input
    * any non-empty token without ``:``
    * a token with empty type or empty location
    * a duplicate ``(type, location)`` pair (would just waste an API
      lookup; almost certainly a typo)
    """
    if not value or not value.strip():
        raise ValueError("server_preferences is empty")
    seen: set[tuple[str, str]] = set()
    specs: list[ServerSpec] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        # PR #537 R3 #1: reject tokens with !=1 colon. ``partition(":")``
        # silently consumes only the first colon, so ``cx43:fsn1:dc14``
        # would parse to location=``fsn1:dc14`` — a value that never
        # matches the Hetzner location-name keys, producing confusing
        # "out of stock" outcomes for the operator.
        if token.count(":") != 1:
            raise ValueError(
                f"server_preferences token must have exactly one ':' separator: {token!r}",
            )
        server_type, _, location = token.partition(":")
        server_type = server_type.strip().lower()
        location = location.strip().lower()
        if not server_type or not location:
            raise ValueError(
                f"server_preferences token has empty type or location: {token!r}",
            )
        key = (server_type, location)
        if key in seen:
            raise ValueError(
                f"duplicate server_preferences entry: {server_type}:{location}",
            )
        seen.add(key)
        specs.append(ServerSpec(server_type=server_type, location=location))
    if not specs:
        raise ValueError("server_preferences contained no valid entries")
    return tuple(specs)


# DI seam: production uses :func:`_default_http_get`; tests inject a
# fake. Signature: (url, bearer_token) -> parsed JSON object.
HttpGet = Callable[[str, str], Any]


def _default_http_get(url: str, token: str) -> Any:
    """Production HTTP GET. Returns parsed JSON.

    Raises :class:`HetznerCapacityError` for every failure mode
    (HTTP 4xx/5xx, network, timeout, malformed JSON) so the pipeline
    can surface a single error class. The original exception is
    chained via ``__cause__`` so a debugger pass still has the full
    detail.
    """
    req = urllib.request.Request(  # noqa: S310 — URL is hard-coded literal _API_BASE
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise HetznerCapacityError(
            f"Hetzner API HTTP {exc.code} for {url}: {exc.reason}",
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HetznerCapacityError(
            f"Hetzner API request failed for {url}: {type(exc).__name__}: {exc}",
        ) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HetznerCapacityError(
            f"Hetzner API returned non-JSON for {url}: {exc}",
        ) from exc


def fetch_availability(
    token: str,
    *,
    http_get: HttpGet | None = None,
) -> dict[str, set[str]]:
    """Query ``/v1/server_types`` + ``/v1/datacenters``.

    Returns a map ``{location_name: {available_server_type_name, ...}}``.
    A location is considered to "have" a server type if ANY datacenter
    at that location lists the type's ID in ``server_types.available``.
    (A single location like ``fsn1`` typically has 1-2 datacenters
    such as ``fsn1-dc14``; treating any-DC-available as
    location-available matches what ``tofu apply`` would actually
    succeed at.)

    Raises :class:`HetznerCapacityError` on auth/network failures or
    when either response is missing the expected top-level field
    (defensive against a future API schema change).
    """
    if not token:
        raise HetznerCapacityError("HCLOUD_TOKEN not set")
    get = http_get if http_get is not None else _default_http_get

    types_payload = get(f"{_API_BASE}/server_types?per_page=200", token)
    server_types = types_payload.get("server_types") if isinstance(types_payload, dict) else None
    if not isinstance(server_types, list):
        raise HetznerCapacityError(
            "Hetzner /v1/server_types response missing 'server_types' list",
        )
    id_to_name: dict[int, str] = {}
    for st in server_types:
        if not isinstance(st, dict):
            continue
        st_id = st.get("id")
        st_name = st.get("name")
        if isinstance(st_id, int) and isinstance(st_name, str):
            id_to_name[st_id] = st_name.lower()

    dc_payload = get(f"{_API_BASE}/datacenters?per_page=50", token)
    datacenters = dc_payload.get("datacenters") if isinstance(dc_payload, dict) else None
    if not isinstance(datacenters, list):
        raise HetznerCapacityError(
            "Hetzner /v1/datacenters response missing 'datacenters' list",
        )

    by_location: dict[str, set[str]] = {}
    for dc in datacenters:
        if not isinstance(dc, dict):
            continue
        location = dc.get("location")
        if not isinstance(location, dict):
            continue
        loc_name = location.get("name")
        if not isinstance(loc_name, str):
            continue
        loc_name = loc_name.lower()
        st_field = dc.get("server_types")
        if not isinstance(st_field, dict):
            continue
        available = st_field.get("available")
        if not isinstance(available, list):
            continue
        names = {id_to_name[i] for i in available if isinstance(i, int) and i in id_to_name}
        by_location.setdefault(loc_name, set()).update(names)
    return by_location


def select(
    preferences: tuple[ServerSpec, ...],
    availability: dict[str, set[str]],
) -> ServerSpec | None:
    """Walk ``preferences`` in order; return the first spec whose
    type is listed as available at the corresponding location.

    Returns ``None`` when every preference is out of stock — the
    caller (CLI handler) is responsible for turning that into a
    user-facing error with the per-pair status, since "list
    exhausted" is the operator-actionable case.
    """
    for spec in preferences:
        types_at_loc = availability.get(spec.location, set())
        if spec.server_type in types_at_loc:
            return spec
    return None


def render_status_lines(
    preferences: tuple[ServerSpec, ...],
    availability: dict[str, set[str]],
    selected: ServerSpec | None,
) -> list[str]:
    """Build a per-preference status block for operator-facing logs.

    One line per preference, marking the selected one with ``→``,
    available-but-not-picked with ``✓``, and unavailable with ``✗``.
    Used by the CLI handler so the operator can see WHY a particular
    pair was chosen (or why all of them failed).
    """
    lines: list[str] = []
    for idx, spec in enumerate(preferences, start=1):
        marker = (
            "→"
            if spec == selected
            else ("✓" if spec.server_type in availability.get(spec.location, set()) else "✗")
        )
        lines.append(f"  {marker} {idx}. {spec}")
    return lines
