"""Tests for nexus_deploy.kestra — Phase 2 Modul 2.3 (#505).

Mocks HTTP via ``responses`` (already a project dep). All paths
exercised: idempotent POST→PUT register, transport-level errors,
execute + poll, the full ``run_register_system_flows`` orchestrator
including the "kestra not ready" early-return.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests
import responses

from nexus_deploy.config import NexusConfig
from nexus_deploy.kestra import (
    FLOW_SYNC_FLOW_TEMPLATE,
    GIT_SYNC_FLOW_TEMPLATE,
    KestraClient,
    KestraError,
    RegisterResult,
    SystemFlowsResult,
    register_all_system_flows,
    render_system_flow_yaml,
    render_system_flows,
    run_register_system_flows,
    trigger_flow_sync_onboarding,
)

BASE_URL = "http://localhost:8085"


def _client() -> KestraClient:
    return KestraClient(BASE_URL, username="admin@example.com", password="kp-secret")


def _make_config(**overrides: Any) -> NexusConfig:
    defaults: dict[str, Any] = {
        "admin_username": "admin",
        "kestra_admin_password": "kp-secret",
    }
    defaults.update(overrides)
    return NexusConfig.from_secrets_json(json.dumps(defaults))


# ---------------------------------------------------------------------------
# KestraClient — constructor
# ---------------------------------------------------------------------------


def test_client_rejects_empty_username() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        KestraClient(BASE_URL, username="", password="x")


def test_client_rejects_empty_password() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        KestraClient(BASE_URL, username="admin", password="")


def test_client_strips_trailing_slash_from_base_url() -> None:
    c = KestraClient("http://kestra.local/", username="u", password="p")
    assert c.base_url == "http://kestra.local"


# ---------------------------------------------------------------------------
# wait_ready — accepted status codes (200, 404, 405)
# ---------------------------------------------------------------------------


@responses.activate
def test_wait_ready_returns_true_on_200() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200, json=[])
    assert _client().wait_ready(timeout_s=2.0, interval_s=0.01) is True


@responses.activate
def test_wait_ready_returns_true_on_404() -> None:
    """404 = endpoint moved (v1.0 patch difference) but basic-auth accepted."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=404)
    assert _client().wait_ready(timeout_s=2.0, interval_s=0.01) is True


@responses.activate
def test_wait_ready_returns_true_on_405() -> None:
    """405 = GET rejected on this path, but basic-auth accepted."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=405)
    assert _client().wait_ready(timeout_s=2.0, interval_s=0.01) is True


@responses.activate
def test_wait_ready_loops_then_succeeds() -> None:
    """Two 401s then a 200 → wait_ready returns True after the third probe."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=401)
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=401)
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200, json=[])
    assert _client().wait_ready(timeout_s=5.0, interval_s=0.01) is True


@responses.activate
def test_wait_ready_returns_false_on_timeout() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=401)
    # Very short timeout, single 401 — loop bails out.
    assert _client().wait_ready(timeout_s=0.05, interval_s=0.05) is False


@responses.activate
def test_wait_ready_handles_connection_errors() -> None:
    """ConnectionError doesn't crash the loop — it just keeps polling."""
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/flows",
        body=requests.ConnectionError("boom"),
    )
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    assert _client().wait_ready(timeout_s=5.0, interval_s=0.01) is True


# ---------------------------------------------------------------------------
# register_flow — POST/PUT idempotent dance
# ---------------------------------------------------------------------------


@responses.activate
def test_register_flow_post_201_returns_created() -> None:
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    result = _client().register_flow("id: x\nnamespace: system", namespace="system", flow_id="x")
    assert result == RegisterResult(name="system.x", status="created", detail="POST 201")


@responses.activate
def test_register_flow_post_200_returns_created() -> None:
    """Some Kestra builds return 200 instead of 201 on first-create."""
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=200)
    result = _client().register_flow("y", namespace="system", flow_id="y")
    assert result.status == "created"


@responses.activate
def test_register_flow_post_422_then_put_200_returns_updated() -> None:
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=422)
    responses.add(responses.PUT, f"{BASE_URL}/api/v1/flows/system/git-sync", status=200)
    result = _client().register_flow("y", namespace="system", flow_id="git-sync")
    assert result.status == "updated"
    assert "POST 422 → PUT 200" in result.detail


@responses.activate
def test_register_flow_post_422_then_put_4xx_returns_failed() -> None:
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=422)
    responses.add(responses.PUT, f"{BASE_URL}/api/v1/flows/system/git-sync", status=400)
    result = _client().register_flow("y", namespace="system", flow_id="git-sync")
    assert result.status == "failed"
    assert "POST 422 → PUT 400" in result.detail


@responses.activate
def test_register_flow_post_5xx_returns_failed() -> None:
    """5xx on POST → we don't fall through to PUT (PUT 5xx-prone too)."""
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=500)
    result = _client().register_flow("y", namespace="system", flow_id="y")
    assert result.status == "failed"
    assert "POST 500" in result.detail
    # No PUT call should have fired
    assert len(responses.calls) == 1


@responses.activate
def test_register_flow_post_401_returns_failed_without_put() -> None:
    """401 on POST is auth-rejected — PUT would just fail the same way."""
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=401)
    result = _client().register_flow("y", namespace="system", flow_id="y")
    assert result.status == "failed"
    assert "POST 401" in result.detail


@responses.activate
def test_register_flow_post_connection_error_returns_failed() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/flows",
        body=requests.ConnectionError("boom"),
    )
    result = _client().register_flow("y", namespace="system", flow_id="y")
    assert result.status == "failed"
    assert "transport" in result.detail
    # Detail must NOT include the exception message (could leak any
    # response body the wrapper baked in).
    assert "boom" not in result.detail


@responses.activate
def test_register_flow_post_422_then_put_connection_error_returns_failed() -> None:
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=422)
    responses.add(
        responses.PUT,
        f"{BASE_URL}/api/v1/flows/system/y",
        body=requests.Timeout("slow"),
    )
    result = _client().register_flow("y", namespace="system", flow_id="y")
    assert result.status == "failed"
    assert "transport" in result.detail
    assert "slow" not in result.detail


@responses.activate
def test_register_flow_sends_yaml_content_type() -> None:
    """Kestra requires application/x-yaml; JSON body would 400."""
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    _client().register_flow("y", namespace="system", flow_id="y")
    assert responses.calls[0].request.headers["Content-Type"] == "application/x-yaml"


@responses.activate
def test_register_flow_sends_basic_auth_in_header_not_body() -> None:
    """R4 — credentials must travel in the Authorization header,
    NEVER in the request body or query string."""
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    _client().register_flow("flow-body", namespace="system", flow_id="y")
    req = responses.calls[0].request
    auth_header = req.headers.get("Authorization", "")
    assert auth_header.startswith("Basic ")
    # body must NOT contain the password
    body = req.body or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    assert "kp-secret" not in body
    # URL must NOT contain the password (no query-string smuggle)
    assert "kp-secret" not in (req.url or "")


# ---------------------------------------------------------------------------
# execute_flow + get_execution_state + wait_for_execution
# ---------------------------------------------------------------------------


@responses.activate
def test_execute_flow_returns_id_on_201() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=201,
        json={"id": "exec-abc-123"},
    )
    assert _client().execute_flow("system", "flow-sync") == "exec-abc-123"


@responses.activate
def test_execute_flow_raises_on_5xx() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=503,
    )
    with pytest.raises(KestraError, match="HTTP 503"):
        _client().execute_flow("system", "flow-sync")


@responses.activate
def test_execute_flow_raises_on_missing_id() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=200,
        json={"not_id": "x"},
    )
    with pytest.raises(KestraError, match="missing 'id'"):
        _client().execute_flow("system", "flow-sync")


@responses.activate
def test_execute_flow_raises_on_non_json() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=200,
        body="not json",
    )
    with pytest.raises(KestraError, match="not JSON"):
        _client().execute_flow("system", "flow-sync")


@responses.activate
def test_execute_flow_raises_on_connection_error() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        body=requests.ConnectionError("boom"),
    )
    with pytest.raises(KestraError, match="transport"):
        _client().execute_flow("system", "flow-sync")


@responses.activate
def test_get_execution_state_success() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    assert _client().get_execution_state("exec-1") == "SUCCESS"


@responses.activate
def test_get_execution_state_unknown_for_unrecognised_state() -> None:
    """Future Kestra state names (PAUSED, etc.) map to UNKNOWN so the
    poller doesn't loop forever."""
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"state": {"current": "PAUSED"}},
    )
    assert _client().get_execution_state("exec-1") == "UNKNOWN"


@responses.activate
def test_get_execution_state_unknown_for_malformed_response() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"foo": "bar"},  # no .state.current
    )
    assert _client().get_execution_state("exec-1") == "UNKNOWN"


@responses.activate
def test_get_execution_state_raises_on_4xx() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=404,
    )
    with pytest.raises(KestraError, match="HTTP 404"):
        _client().get_execution_state("exec-1")


@responses.activate
def test_get_execution_state_raises_on_connection_error() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        body=requests.ConnectionError("boom"),
    )
    with pytest.raises(KestraError, match="transport"):
        _client().get_execution_state("exec-1")


@responses.activate
def test_get_execution_state_unknown_for_non_json_response() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        body="not json at all",
    )
    assert _client().get_execution_state("exec-1") == "UNKNOWN"


@responses.activate
def test_get_execution_state_unknown_for_top_level_list_response() -> None:
    """Defensive: Kestra returns dict-shaped payload; a list is unexpected
    but the poller must not crash on it."""
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json=[1, 2, 3],
    )
    assert _client().get_execution_state("exec-1") == "UNKNOWN"


@responses.activate
def test_wait_for_execution_returns_terminal_state() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"state": {"current": "RUNNING"}},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    assert _client().wait_for_execution("exec-1", timeout_s=5.0, interval_s=0.01) == "SUCCESS"


@responses.activate
def test_wait_for_execution_returns_running_on_timeout() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"state": {"current": "RUNNING"}},
    )
    # 0.05s timeout, all responses RUNNING → returns RUNNING (caller treats as warning)
    state = _client().wait_for_execution("exec-1", timeout_s=0.05, interval_s=0.05)
    assert state == "RUNNING"


@responses.activate
def test_wait_for_execution_handles_kestra_error_then_recovers() -> None:
    """Transient KestraError from get_execution_state → poll continues."""
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=503,  # raises KestraError on first call
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    assert _client().wait_for_execution("exec-1", timeout_s=5.0, interval_s=0.01) == "SUCCESS"


# ---------------------------------------------------------------------------
# render_system_flow_yaml + render_system_flows
# ---------------------------------------------------------------------------


def test_render_git_sync_substitutes_placeholders() -> None:
    yaml_body = render_system_flow_yaml(
        GIT_SYNC_FLOW_TEMPLATE,
        repo_owner="alice",
        repo_name="ws-repo",
        branch="main",
        admin_username="admin",
    )
    assert "url: http://gitea:3000/alice/ws-repo.git" in yaml_body
    assert "branch: main" in yaml_body
    assert "username: admin" in yaml_body
    # Pebble template must reach Kestra verbatim — single-brace form
    # after Python's str.format processes the double-brace escape.
    assert "{{ secret('GITEA_TOKEN') }}" in yaml_body
    assert "gitDirectory: nexus_seeds/kestra/workflows" in yaml_body


def test_render_flow_sync_pins_target_namespace() -> None:
    """v1.0 plugin requires targetNamespace; our template MUST set it
    to nexus-tutorials. A regression here would bring back the
    'tasks[0].targetNamespace: must not be null' deploy failure
    that PR (cited in deploy.sh comments) chased down."""
    yaml_body = render_system_flow_yaml(
        FLOW_SYNC_FLOW_TEMPLATE,
        repo_owner="bob",
        repo_name="r",
        branch="dev",
        admin_username="admin",
    )
    assert "targetNamespace: nexus-tutorials" in yaml_body
    assert "includeChildNamespaces: true" in yaml_body
    assert "delete: true" in yaml_body
    assert "gitDirectory: nexus_seeds/kestra/flows" in yaml_body


def test_render_system_flows_returns_both() -> None:
    flows = render_system_flows(
        repo_owner="alice", repo_name="r", branch="main", admin_username="admin"
    )
    assert set(flows.keys()) == {"system.git-sync", "system.flow-sync"}
    assert "git-sync" in flows["system.git-sync"]
    assert "flow-sync" in flows["system.flow-sync"]


def test_render_system_flows_does_not_double_substitute_secret_pebble() -> None:
    """The Pebble syntax {{ secret('GITEA_TOKEN') }} must remain as
    single-braces in the rendered YAML so Kestra's templating engine
    can interpret it. Python str.format escape uses double-braces in
    the template; if a future contributor accidentally drops the
    escape, .format would treat 'secret' as a placeholder and raise
    KeyError. This test pins the contract."""
    flows = render_system_flows(repo_owner="o", repo_name="r", branch="b", admin_username="a")
    for body in flows.values():
        assert "{{ secret('GITEA_TOKEN') }}" in body
        # No double-braces should remain (those would be a Python escape
        # leaking into the rendered Kestra YAML).
        assert "{{{{ secret" not in body
        assert "}}}}" not in body


# ---------------------------------------------------------------------------
# register_all_system_flows + trigger_flow_sync_onboarding
# ---------------------------------------------------------------------------


@responses.activate
def test_register_all_system_flows_returns_one_result_per_flow() -> None:
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    flows = render_system_flows(repo_owner="o", repo_name="r", branch="b", admin_username="a")
    results = register_all_system_flows(_client(), flows)
    assert len(results) == 2
    assert {r.name for r in results} == {"system.git-sync", "system.flow-sync"}
    assert all(r.status == "created" for r in results)


@responses.activate
def test_trigger_flow_sync_onboarding_returns_terminal_state() -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=201,
        json={"id": "exec-1"},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-1",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    assert trigger_flow_sync_onboarding(_client(), timeout_s=5.0) == "SUCCESS"


# ---------------------------------------------------------------------------
# run_register_system_flows — top-level orchestrator
# ---------------------------------------------------------------------------


@responses.activate
def test_run_register_system_flows_happy_path_with_onboarding() -> None:
    """Wait → register both → execute flow-sync → poll SUCCESS → seed-flow visible."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=201,
        json={"id": "exec-99"},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-99",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    # Post-execute verification: seed flow IS visible
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/flows/nexus-tutorials/r2-taxi-pipeline",
        status=200,
        json={"id": "r2-taxi-pipeline"},
    )

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        ready_timeout_s=0.05,
        onboarding_timeout_s=2.0,
    )
    assert result.is_success
    assert result.execution_state == "SUCCESS"
    assert all(f.status in ("created", "updated") for f in result.flows)


@responses.activate
def test_run_register_system_flows_kestra_not_ready_returns_failed_results() -> None:
    """wait_ready times out → both flows reported failed with 'kestra not ready' detail."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=401)

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        ready_timeout_s=0.05,
        onboarding_timeout_s=2.0,
    )
    assert not result.is_success
    assert all(f.status == "failed" for f in result.flows)
    assert all("not ready" in f.detail for f in result.flows)
    assert result.execution_state is None  # no execute attempt


@responses.activate
def test_run_register_system_flows_skips_onboarding_if_register_failed() -> None:
    """If even ONE register failed, don't trigger flow-sync (would race
    against stale flow definition)."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=500)  # 2nd register fails

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        ready_timeout_s=0.05,
        onboarding_timeout_s=2.0,
    )
    assert not result.is_success
    assert any(f.status == "failed" for f in result.flows)
    assert result.execution_state is None  # NOT triggered


@responses.activate
def test_run_register_system_flows_trigger_onboarding_false_skips_execute() -> None:
    """Caller can opt out of the post-register flow-sync trigger."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        trigger_onboarding=False,
        ready_timeout_s=2.0,
    )
    assert result.is_success
    assert result.execution_state is None


@responses.activate
def test_run_register_system_flows_onboarding_kestra_error_recorded_as_trigger_failed() -> None:
    """Execute throws KestraError → execution_state=TRIGGER_FAILED (NOT None).

    Round-2 fix: previously this collapsed to None, which made
    is_success return True even though onboarding never ran. Now the
    distinct sentinel makes deploy.sh route to the yellow-warning
    branch (rc=1) instead of silently green.
    """
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=503,
    )

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        ready_timeout_s=0.05,
        onboarding_timeout_s=2.0,
    )
    assert result.execution_state == "TRIGGER_FAILED"
    # All registers succeeded — the failure is purely the onboarding execute
    assert all(f.status in ("created", "updated") for f in result.flows)
    # is_success must be False so the CLI returns rc=1
    assert result.is_success is False


@responses.activate
def test_run_register_system_flows_seed_flow_missing_after_success() -> None:
    """SUCCESS execution but the canonical seed flow isn't in Kestra → SEED_FLOW_MISSING.

    Mirrors deploy.sh L3479-3490: a SUCCESS execution against an empty
    seed tree (no flows in the workspace repo) wouldn't surface as
    FAILED. Without the post-execute verify, deploy would falsely
    print green "registered" while operators couldn't find the
    tutorial flow.
    """
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=201,
        json={"id": "exec-99"},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-99",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    # Verification call: 404 — flow not registered
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/flows/nexus-tutorials/r2-taxi-pipeline",
        status=404,
    )

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        ready_timeout_s=0.05,
        onboarding_timeout_s=2.0,
    )
    assert result.execution_state == "SEED_FLOW_MISSING"
    assert result.is_success is False


@responses.activate
def test_run_register_system_flows_seed_flow_visible_after_success() -> None:
    """SUCCESS + seed flow visible (200) → execution_state stays SUCCESS."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=201,
        json={"id": "exec-99"},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-99",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/flows/nexus-tutorials/r2-taxi-pipeline",
        status=200,
        json={"id": "r2-taxi-pipeline"},
    )

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        ready_timeout_s=0.05,
        onboarding_timeout_s=2.0,
    )
    assert result.execution_state == "SUCCESS"
    assert result.is_success is True


@responses.activate
def test_run_register_system_flows_seed_verify_transport_error_keeps_success() -> None:
    """Verification HTTP 5xx → don't downgrade a SUCCESS execution.

    Network blip during the verify call shouldn't reclassify a
    perfectly-valid SUCCESS execution as a failure — a transient
    glitch is recoverable on the next deploy.
    """
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows", status=200)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(responses.POST, f"{BASE_URL}/api/v1/flows", status=201)
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/executions/system/flow-sync",
        status=201,
        json={"id": "exec-99"},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/executions/exec-99",
        status=200,
        json={"state": {"current": "SUCCESS"}},
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/flows/nexus-tutorials/r2-taxi-pipeline",
        status=503,
    )

    result = run_register_system_flows(
        _make_config(),
        base_url=BASE_URL,
        repo_owner="o",
        repo_name="r",
        branch="main",
        admin_email="admin@example.com",
        ready_timeout_s=0.05,
        onboarding_timeout_s=2.0,
    )
    # Stays SUCCESS despite the verification failure
    assert result.execution_state == "SUCCESS"
    assert result.is_success is True


# ---------------------------------------------------------------------------
# SystemFlowsResult.is_success edge cases
# ---------------------------------------------------------------------------


def test_is_success_true_when_no_execution_triggered() -> None:
    r = SystemFlowsResult(
        flows=(
            RegisterResult(name="a", status="created"),
            RegisterResult(name="b", status="updated"),
        ),
        execution_state=None,
    )
    assert r.is_success is True


def test_is_success_false_on_running_at_timeout() -> None:
    """Onboarding execution never settled — deploy shouldn't claim success."""
    r = SystemFlowsResult(
        flows=(RegisterResult(name="a", status="created"),),
        execution_state="RUNNING",
    )
    assert r.is_success is False


def test_is_success_false_on_register_failure_even_with_success_execution() -> None:
    """Defensive: a register-fail can't be masked by a later SUCCESS execution."""
    r = SystemFlowsResult(
        flows=(
            RegisterResult(name="a", status="failed"),
            RegisterResult(name="b", status="created"),
        ),
        execution_state="SUCCESS",
    )
    assert r.is_success is False


def test_is_success_false_on_trigger_failed() -> None:
    """TRIGGER_FAILED → onboarding never even started → is_success False.

    Round-2 round of #517: previously this collapsed to None and
    silently passed. The dedicated sentinel pins the contract.
    """
    r = SystemFlowsResult(
        flows=(RegisterResult(name="a", status="created"),),
        execution_state="TRIGGER_FAILED",
    )
    assert r.is_success is False


def test_is_success_false_on_seed_flow_missing() -> None:
    """SEED_FLOW_MISSING → SUCCESS execution but no user flow → is_success False."""
    r = SystemFlowsResult(
        flows=(RegisterResult(name="a", status="created"),),
        execution_state="SEED_FLOW_MISSING",
    )
    assert r.is_success is False


# ---------------------------------------------------------------------------
# flow_exists — post-execute seed verification
# ---------------------------------------------------------------------------


@responses.activate
def test_flow_exists_returns_true_on_200() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/flows/system/git-sync",
        status=200,
        json={"id": "git-sync"},
    )
    assert _client().flow_exists("system", "git-sync") is True


@responses.activate
def test_flow_exists_returns_false_on_404() -> None:
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows/nexus-tutorials/missing", status=404)
    assert _client().flow_exists("nexus-tutorials", "missing") is False


@responses.activate
def test_flow_exists_raises_on_5xx() -> None:
    """5xx is neither yes-it-exists nor no-it-doesn't — raise to caller."""
    responses.add(responses.GET, f"{BASE_URL}/api/v1/flows/system/x", status=503)
    with pytest.raises(KestraError, match="HTTP 503"):
        _client().flow_exists("system", "x")


@responses.activate
def test_flow_exists_raises_on_connection_error() -> None:
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/flows/system/x",
        body=requests.ConnectionError("boom"),
    )
    with pytest.raises(KestraError, match="transport"):
        _client().flow_exists("system", "x")


# ---------------------------------------------------------------------------
# CLI: _kestra_register_system_flows
# ---------------------------------------------------------------------------


def _set_required_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Default env var set for the CLI tests; overrides override."""
    defaults: dict[str, str] = {
        "GITEA_REPO_OWNER": "alice",
        "REPO_NAME": "ws-repo",
        "WORKSPACE_BRANCH": "main",
        "ADMIN_EMAIL": "admin@example.com",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)


def test_cli_kestra_unknown_arg_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _kestra_register_system_flows

    rc = _kestra_register_system_flows(["--bogus"])
    assert rc == 2
    assert "unknown args" in capsys.readouterr().err


def test_cli_kestra_missing_required_env_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _kestra_register_system_flows

    monkeypatch.delenv("GITEA_REPO_OWNER", raising=False)
    monkeypatch.delenv("REPO_NAME", raising=False)
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    rc = _kestra_register_system_flows([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing required env" in err


def test_cli_kestra_missing_kestra_pass_returns_zero_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No Kestra password in SECRETS_JSON → log warning, exit 0 (deploy continues)."""
    from nexus_deploy.__main__ import _kestra_register_system_flows

    _set_required_env(monkeypatch)
    monkeypatch.setattr("sys.stdin.read", lambda: "{}")
    rc = _kestra_register_system_flows([])
    assert rc == 0
    err = capsys.readouterr().err
    assert "KESTRA_PASS missing" in err


def test_cli_kestra_invalid_json_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _kestra_register_system_flows

    _set_required_env(monkeypatch)
    monkeypatch.setattr("sys.stdin.read", lambda: "not json {")
    rc = _kestra_register_system_flows([])
    assert rc == 2


def test_cli_kestra_ssh_tunnel_failure_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tunnel setup failure → typed SSHError → rc=2."""
    from nexus_deploy.__main__ import _kestra_register_system_flows
    from nexus_deploy.ssh import SSHError

    _set_required_env(monkeypatch)
    monkeypatch.setattr("sys.stdin.read", lambda: '{"kestra_admin_password": "kp"}')

    class _BoomSSH:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _BoomSSH:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def port_forward(self, *_args: Any, **_kwargs: Any) -> Any:
            raise SSHError("ssh tunnel to local port 8085 did not come up within 10.0s")

    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", _BoomSSH)
    rc = _kestra_register_system_flows([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ssh tunnel failed" in err
    # SSHError carries safe fixed-format text — its message IS forwarded
    # because ssh.py guarantees no subprocess output is in there.
    assert "did not come up" in err


def test_cli_kestra_unexpected_exception_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any non-SSH exception class-name only, no str(exc) leak."""
    from nexus_deploy.__main__ import _kestra_register_system_flows

    _set_required_env(monkeypatch)
    monkeypatch.setattr("sys.stdin.read", lambda: '{"kestra_admin_password": "kp"}')

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("secret-do-not-print")

    monkeypatch.setattr("nexus_deploy.__main__.run_register_system_flows", boom)
    # Make SSHClient + port_forward succeed so we reach the run call
    from contextlib import contextmanager

    class _OkSSH:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _OkSSH:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        @contextmanager
        def port_forward(self, *_args: Any, **_kwargs: Any) -> Any:
            yield 8085

    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", _OkSSH)
    rc = _kestra_register_system_flows([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "secret-do-not-print" not in err


def test_cli_kestra_happy_path_returns_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _kestra_register_system_flows

    _set_required_env(monkeypatch)
    monkeypatch.setattr("sys.stdin.read", lambda: '{"kestra_admin_password": "kp"}')

    def fake_run(*_args: Any, **_kwargs: Any) -> SystemFlowsResult:
        return SystemFlowsResult(
            flows=(
                RegisterResult(name="system.git-sync", status="created", detail="POST 201"),
                RegisterResult(
                    name="system.flow-sync", status="updated", detail="POST 422 → PUT 200"
                ),
            ),
            execution_state="SUCCESS",
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_register_system_flows", fake_run)

    from contextlib import contextmanager

    class _OkSSH:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _OkSSH:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        @contextmanager
        def port_forward(self, *_args: Any, **_kwargs: Any) -> Any:
            yield 8085

    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", _OkSSH)
    rc = _kestra_register_system_flows([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "created=1" in captured.out
    assert "updated=1" in captured.out
    assert "execution=SUCCESS" in captured.out
    assert "system.git-sync: created" in captured.err
    assert "system.flow-sync: updated" in captured.err


def test_cli_kestra_partial_failure_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_deploy.__main__ import _kestra_register_system_flows

    _set_required_env(monkeypatch)
    monkeypatch.setattr("sys.stdin.read", lambda: '{"kestra_admin_password": "kp"}')

    def fake_run(*_args: Any, **_kwargs: Any) -> SystemFlowsResult:
        return SystemFlowsResult(
            flows=(
                RegisterResult(name="system.git-sync", status="created", detail="POST 201"),
                RegisterResult(name="system.flow-sync", status="failed", detail="POST 500"),
            ),
            execution_state=None,
        )

    monkeypatch.setattr("nexus_deploy.__main__.run_register_system_flows", fake_run)

    from contextlib import contextmanager

    class _OkSSH:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _OkSSH:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        @contextmanager
        def port_forward(self, *_args: Any, **_kwargs: Any) -> Any:
            yield 8085

    monkeypatch.setattr("nexus_deploy.__main__.SSHClient", _OkSSH)
    rc = _kestra_register_system_flows([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "failed=1" in captured.out
    assert "execution=skipped" in captured.out
