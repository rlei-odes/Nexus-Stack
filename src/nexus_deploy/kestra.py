"""Kestra flow-registration client (Phase 2 Modul 2.3, #505).

Replaces deploy.sh's L3343-3508 — the ``REMOTE_REGISTER_EOF`` heredoc
that ssh-piped a server-side bash script which (a) wrote a curl
``--config`` file with basic-auth, (b) defined a ``register_flow``
shell function with POST-then-PUT idempotent dispatch, (c) registered
the two ``system.git-sync`` / ``system.flow-sync`` flows, and (d)
optionally triggered a one-shot ``flow-sync`` execution to onboard
user-seeded flows immediately instead of waiting for the 15-min cron.

The new architecture demonstrates the Phase 3 pattern that
``ssh.SSHClient.port_forward`` was built for: open a tunnel, talk to
the service via local ``requests`` calls, surface HTTP errors as
typed Python exceptions. No rendered server-side bash, no
heredoc-quoting, no escape-twice escape-thrice quoting hell — and
the entire flow logic is unit-testable against ``responses``-mocked
HTTP without ever running ssh.

API:

- :class:`KestraClient` — basic-auth REST client. ``wait_ready``,
  ``register_flow`` (POST 200/201 / 422 → PUT 200/201 / failed),
  ``execute_flow``, ``wait_for_execution``.
- :func:`render_system_flow_yaml` — string-template-based YAML
  builder for the two system flows. Templates ship verbatim from
  deploy.sh so the registered flow body is byte-equivalent (modulo
  the substituted variables).
- :func:`run_register_system_flows` — top-level orchestrator: opens
  port-forward, waits for Kestra, registers both flows, optionally
  triggers ``flow-sync``. Returns the full set of register results
  for the CLI to map to rc=0/1/2.

Auth note (R4): basic-auth credentials are passed to ``requests`` via
the ``auth=(user, pass)`` keyword, which puts them in the
``Authorization`` header — never in argv on either host (we don't
shell out at all here, except to ssh for the tunnel which carries
no service-side credentials in argv).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import requests

from nexus_deploy.config import NexusConfig

# HTTP timeouts:
# - connect: short (TCP setup) — Kestra is local via tunnel; if it
#   doesn't accept connection in 3s, something is structurally wrong.
# - read: longer (Kestra v1.0 OSS can pause on heavy plugin work
#   especially first-call after restart) — 15s gives the JVM time to
#   warm up the request handler without hanging the deploy.
_CONNECT_TIMEOUT_S: float = 3.0
_READ_TIMEOUT_S: float = 15.0
_HTTP_TIMEOUT: tuple[float, float] = (_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S)


RegisterStatus = Literal["created", "updated", "failed"]
ExecutionState = Literal["SUCCESS", "FAILED", "KILLED", "RUNNING", "CREATED", "UNKNOWN"]


@dataclass(frozen=True)
class RegisterResult:
    """Outcome of one ``register_flow`` call.

    ``name`` is the fully-qualified ``<namespace>.<flow_id>`` form
    used in deploy logs. ``detail`` carries the HTTP status the
    operator needs to debug a ``failed`` (e.g. ``"POST 401"``).
    """

    name: str
    status: RegisterStatus
    detail: str = ""


@dataclass(frozen=True)
class SystemFlowsResult:
    """Aggregate of the system-flow registration + onboarding execution."""

    flows: tuple[RegisterResult, ...]
    execution_state: ExecutionState | None = None  # None if not triggered

    @property
    def is_success(self) -> bool:
        """All flows registered/updated AND (if triggered) execution succeeded."""
        flows_ok = all(f.status != "failed" for f in self.flows)
        # ExecutionState=None means we didn't trigger (a flow registration
        # failed, so triggering would race against a stale flow definition);
        # that's reflected in flows_ok=False already. RUNNING/CREATED at
        # timeout count as failure for the deploy-time "everything ready"
        # contract — the deploy shouldn't claim success if the onboarding
        # didn't actually finish.
        if not flows_ok:
            return False
        if self.execution_state is None:
            return True
        return self.execution_state == "SUCCESS"


class KestraError(Exception):
    """Transport-level failure (connection refused, timeout, malformed JSON).

    Distinct from a ``failed`` :class:`RegisterResult` — those represent
    a server response we understood but rejected (4xx/5xx after both
    POST and PUT). KestraError is "we never got a meaningful response".
    Carries no response body in its message: response bodies on auth
    failures can include the credentials we just sent.
    """


class KestraClient:
    """Minimal REST client for Kestra OSS v1.0+.

    Basic-auth via ``requests`` — the credentials live in the
    ``Authorization`` header per request, never in argv (no shell-out
    here). Read/write timeouts are bounded so a stuck JVM during
    plugin load can't deadlock the deploy.
    """

    def __init__(self, base_url: str, *, username: str, password: str) -> None:
        if not username or not password:
            raise ValueError("KestraClient requires non-empty username + password")
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)

    def wait_ready(self, *, timeout_s: float = 60.0, interval_s: float = 3.0) -> bool:
        """Poll ``GET /api/v1/flows`` until basic-auth-accepted.

        Kestra v1.0 OSS has no health endpoint that respects basic-auth
        without listing data; ``/api/v1/flows`` is the canonical probe.
        Accepted status codes:

        - **200** — fully ready, returns flow list
        - **404** — /api/v1/flows endpoint shape changed in some
          v1.0 patches (read path moved under tenant prefix), but
          basic-auth was accepted to evaluate the path
        - **405** — same endpoint may reject GET in some configs;
          again basic-auth was accepted

        These three == "Kestra is ready and our credentials work".
        Anything else (000/401/403/5xx) keeps the loop running until
        timeout. Returns True on first ready, False on timeout.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                resp = requests.get(
                    f"{self.base_url}/api/v1/flows",
                    auth=self._auth,
                    timeout=_HTTP_TIMEOUT,
                )
            except (requests.ConnectionError, requests.Timeout):
                resp = None
            if resp is not None and resp.status_code in (200, 404, 405):
                return True
            time.sleep(interval_s)
        return False

    def register_flow(
        self,
        yaml_body: str,
        *,
        namespace: str,
        flow_id: str,
    ) -> RegisterResult:
        """Idempotent register: POST first, fall back to PUT on 422.

        Kestra v1.0 OSS does NOT have an upsert verb — POST is
        create-only (returns 422 with ``"Flow id already exists"`` if
        the flow is there) and PUT is update-only (returns 404 if the
        flow doesn't exist). Neither alone covers re-runs. The
        deploy.sh logic combined them; we mirror that exactly.
        """
        full_name = f"{namespace}.{flow_id}"
        try:
            post_resp = requests.post(
                f"{self.base_url}/api/v1/flows",
                auth=self._auth,
                headers={"Content-Type": "application/x-yaml"},
                data=yaml_body.encode("utf-8"),
                timeout=_HTTP_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            return RegisterResult(
                name=full_name,
                status="failed",
                detail=f"POST transport ({type(exc).__name__})",
            )

        if post_resp.status_code in (200, 201):
            return RegisterResult(
                name=full_name, status="created", detail=f"POST {post_resp.status_code}"
            )
        if post_resp.status_code != 422:
            return RegisterResult(
                name=full_name,
                status="failed",
                detail=f"POST {post_resp.status_code}",
            )

        # POST 422 → exists → PUT to update
        try:
            put_resp = requests.put(
                f"{self.base_url}/api/v1/flows/{namespace}/{flow_id}",
                auth=self._auth,
                headers={"Content-Type": "application/x-yaml"},
                data=yaml_body.encode("utf-8"),
                timeout=_HTTP_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            return RegisterResult(
                name=full_name,
                status="failed",
                detail=f"PUT transport ({type(exc).__name__})",
            )

        if put_resp.status_code in (200, 201):
            return RegisterResult(
                name=full_name,
                status="updated",
                detail=f"POST 422 → PUT {put_resp.status_code}",
            )
        return RegisterResult(
            name=full_name,
            status="failed",
            detail=f"POST 422 → PUT {put_resp.status_code}",
        )

    def execute_flow(self, namespace: str, flow_id: str) -> str:
        """Trigger an execution. Returns the execution ID.

        Raises :class:`KestraError` if Kestra doesn't return a parseable
        execution ID — that's a transport-level failure, not the same
        as the execution running and ending in FAILED state (which
        would surface via :meth:`get_execution_state`).
        """
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/executions/{namespace}/{flow_id}",
                auth=self._auth,
                timeout=_HTTP_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise KestraError(f"execute_flow transport ({type(exc).__name__})") from exc
        if resp.status_code not in (200, 201):
            raise KestraError(
                f"execute_flow {namespace}.{flow_id} returned HTTP {resp.status_code}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise KestraError("execute_flow response was not JSON") from exc
        exec_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(exec_id, str) or not exec_id:
            raise KestraError("execute_flow response missing 'id'")
        return exec_id

    def get_execution_state(self, exec_id: str) -> ExecutionState:
        """Read the current execution state. Returns ``"UNKNOWN"`` if Kestra
        responded but the JSON shape is unexpected (don't raise — pollers
        keep going if a transient deserialisation glitch happens)."""
        try:
            resp = requests.get(
                f"{self.base_url}/api/v1/executions/{exec_id}",
                auth=self._auth,
                timeout=_HTTP_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise KestraError(f"get_execution_state transport ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise KestraError(f"get_execution_state HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            return "UNKNOWN"
        if not isinstance(payload, dict):
            return "UNKNOWN"
        state_obj = payload.get("state")
        if not isinstance(state_obj, dict):
            return "UNKNOWN"
        current = state_obj.get("current")
        # Kestra-side states we recognise; others (PAUSED, etc.) coalesce to UNKNOWN
        # so the caller's poll-until-terminal logic doesn't loop forever.
        if current in ("SUCCESS", "FAILED", "KILLED", "RUNNING", "CREATED"):
            return current  # type: ignore[no-any-return]
        return "UNKNOWN"

    def wait_for_execution(
        self,
        exec_id: str,
        *,
        timeout_s: float = 60.0,
        interval_s: float = 2.0,
    ) -> ExecutionState:
        """Poll ``get_execution_state`` until terminal or timeout.

        Terminal states: ``SUCCESS``, ``FAILED``, ``KILLED``. Returns
        whichever was reached, or ``"RUNNING"`` if the timeout fired
        before the execution settled (caller maps to a warning, not a
        deploy failure — the execution may finish in the next minute).
        """
        deadline = time.monotonic() + timeout_s
        last: ExecutionState = "CREATED"
        while time.monotonic() < deadline:
            try:
                last = self.get_execution_state(exec_id)
            except KestraError:
                # Transient — retry. wait_for_execution itself only raises
                # on timeout-with-no-terminal.
                last = "UNKNOWN"
            if last in ("SUCCESS", "FAILED", "KILLED"):
                return last
            time.sleep(interval_s)
        return last


# ---------------------------------------------------------------------------
# System-flow YAML templates. Verbatim from deploy.sh L3410-3443 with
# {placeholder} substitutions for the per-deploy fields. Schema is
# pinned to Kestra v1.0 OSS plugin shape (SyncNamespaceFiles +
# SyncFlows from io.kestra.plugin.git, both of which require
# targetNamespace / namespace fields on v1.0).
# ---------------------------------------------------------------------------

GIT_SYNC_FLOW_TEMPLATE = """\
id: git-sync
namespace: system
tasks:
  - id: sync
    type: io.kestra.plugin.git.SyncNamespaceFiles
    url: http://gitea:3000/{repo_owner}/{repo_name}.git
    branch: {branch}
    username: {admin_username}
    password: "{{{{ secret('GITEA_TOKEN') }}}}"
    namespace: "{{{{ flow.namespace }}}}"
    gitDirectory: nexus_seeds/kestra/workflows
triggers:
  - id: schedule
    type: io.kestra.core.models.triggers.types.Schedule
    cron: "*/15 * * * *"
"""

FLOW_SYNC_FLOW_TEMPLATE = """\
id: flow-sync
namespace: system
description: Pull flow definitions from internal Gitea, register them in Kestra. Git is source of truth.
tasks:
  - id: sync
    type: io.kestra.plugin.git.SyncFlows
    url: http://gitea:3000/{repo_owner}/{repo_name}.git
    branch: {branch}
    username: {admin_username}
    password: "{{{{ secret('GITEA_TOKEN') }}}}"
    gitDirectory: nexus_seeds/kestra/flows
    targetNamespace: nexus-tutorials
    includeChildNamespaces: true
    delete: true
triggers:
  - id: schedule
    type: io.kestra.core.models.triggers.types.Schedule
    cron: "*/15 * * * *"
"""


def render_system_flow_yaml(
    template: str,
    *,
    repo_owner: str,
    repo_name: str,
    branch: str,
    admin_username: str,
) -> str:
    """Substitute placeholders into a system-flow YAML template.

    The double-brace ``{{{{ secret('GITEA_TOKEN') }}}}`` in the templates
    becomes a single ``{{ secret('GITEA_TOKEN') }}`` after format —
    that's intentional, it's the Kestra Pebble template syntax that
    must reach the registered flow verbatim.
    """
    return template.format(
        repo_owner=repo_owner,
        repo_name=repo_name,
        branch=branch,
        admin_username=admin_username,
    )


def render_system_flows(
    *,
    repo_owner: str,
    repo_name: str,
    branch: str,
    admin_username: str,
) -> dict[str, str]:
    """Return ``{full_name: yaml_body}`` for both system flows."""
    common = {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "admin_username": admin_username,
    }
    return {
        "system.git-sync": render_system_flow_yaml(GIT_SYNC_FLOW_TEMPLATE, **common),
        "system.flow-sync": render_system_flow_yaml(FLOW_SYNC_FLOW_TEMPLATE, **common),
    }


def register_all_system_flows(
    client: KestraClient,
    flows: dict[str, str],
) -> tuple[RegisterResult, ...]:
    """Register every flow in ``flows``. Order = caller-provided dict order."""
    results: list[RegisterResult] = []
    for full_name, yaml in flows.items():
        ns, _, flow_id = full_name.partition(".")
        results.append(client.register_flow(yaml, namespace=ns, flow_id=flow_id))
    return tuple(results)


def trigger_flow_sync_onboarding(
    client: KestraClient,
    *,
    timeout_s: float = 60.0,
) -> ExecutionState:
    """One-shot execute ``system.flow-sync`` and wait for terminal state.

    Without this, user-seeded flows in ``nexus_seeds/kestra/flows/`` only
    appear after the next 15-min cron tick — the deploy would print
    "Deployment Complete" with no user flows visible in the Kestra UI,
    causing reasonable "where are my flows?" confusion. The trigger here
    is best-effort: if the execute call or polling fails, the cron will
    still tick eventually, so we surface the failure as a warning, not
    a deploy abort.

    Raises :class:`KestraError` only on the initial execute_flow call;
    polling failures coalesce to ``"UNKNOWN"`` then ``"RUNNING"`` at
    timeout (callers treat both as warnings).
    """
    exec_id = client.execute_flow("system", "flow-sync")
    return client.wait_for_execution(exec_id, timeout_s=timeout_s)


def run_register_system_flows(
    config: NexusConfig,
    *,
    base_url: str,
    repo_owner: str,
    repo_name: str,
    branch: str,
    admin_email: str,
    trigger_onboarding: bool = True,
    ready_timeout_s: float = 60.0,
    onboarding_timeout_s: float = 60.0,
) -> SystemFlowsResult:
    """End-to-end: instantiate client, wait, register both flows, optionally
    trigger ``system.flow-sync`` execution.

    Caller is responsible for opening the SSH port-forward to Kestra and
    passing the local ``base_url`` (e.g. ``http://localhost:8085``).
    Keeping the tunnel concern outside this function makes the logic
    testable against ``responses``-mocked HTTP without an ssh roundtrip.

    ``ready_timeout_s`` / ``onboarding_timeout_s`` are exposed primarily
    so unit tests can drive the orchestrator to completion in
    sub-second wall-clock; production callers use the defaults.
    """
    client = KestraClient(
        base_url=base_url,
        username=admin_email,
        password=config.kestra_admin_password or "",
    )
    if not client.wait_ready(timeout_s=ready_timeout_s):
        # Kestra never reached basic-auth-accepted state. Both flows
        # would 401; surface a clean partial result so deploy.sh sees
        # rc=1 (yellow warning, continue).
        return SystemFlowsResult(
            flows=(
                RegisterResult(name="system.git-sync", status="failed", detail="kestra not ready"),
                RegisterResult(name="system.flow-sync", status="failed", detail="kestra not ready"),
            ),
        )

    flows = render_system_flows(
        repo_owner=repo_owner,
        repo_name=repo_name,
        branch=branch,
        admin_username=config.admin_username or "admin",
    )
    register_results = register_all_system_flows(client, flows)

    if not trigger_onboarding:
        return SystemFlowsResult(flows=register_results)

    # Only trigger the onboarding execute when both register calls
    # succeeded — otherwise we'd execute a flow-sync against a stale
    # flow definition.
    if any(r.status == "failed" for r in register_results):
        return SystemFlowsResult(flows=register_results)

    try:
        exec_state: ExecutionState | None = trigger_flow_sync_onboarding(
            client, timeout_s=onboarding_timeout_s
        )
    except KestraError:
        exec_state = None
    return SystemFlowsResult(flows=register_results, execution_state=exec_state)
