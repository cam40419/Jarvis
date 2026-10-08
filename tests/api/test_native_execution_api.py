"""Real human sessions and isolated worker credentials around durable native runs."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from simon.domain.identity import DEV_WORKSPACE_ID
from simon.domain.native_projects import NativeProjectMember
from simon.services import model_usage, native_execution
from tests.api.test_native_models_api import (
    configure_model_http,
    enroll_model,
    grant_budget,
    probe_model,
)
from tests.api.test_native_project_api import create_project, sign_in
from tests.api.test_native_team_api import role_body
from tests.api.test_native_team_api import worker as worker
from tests.unit.test_intake_planner import wire_result


def enable_execution(h, **changes):
    current = h.client.get(h.path).json()["policy"]
    values = {
        key: value
        for key, value in current.items()
        if key
        not in {
            "workspace_id",
            "project_id",
            "version",
            "issued_by",
            "updated_at",
        }
    }
    response = h.client.put(
        h.path + "/policy",
        headers=h.headers,
        json={
            **values,
            "enabled": True,
            "expected_version": current["version"],
            "idempotency_key": f"execution-policy-{uuid4()}",
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_execution_task(h, *, title="Prepare launch brief", agent_id=None):
    response = h.client.post(
        h.project_path + "/tasks",
        headers=h.headers,
        json={
            "title": title,
            "description": "Prepare a concrete candidate for owner review.",
            "assignment": {"kind": "agent", "agent_id": agent_id or h.agent["id"]},
            "idempotency_key": f"task-{uuid4()}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def start_execution(h, task=None, **changes):
    task = task or h.task
    command = {
        "expected_task_version": task["version"],
        "idempotency_key": f"start-{uuid4()}",
        **changes,
    }
    response = h.client.post(h.path + f"/tasks/{task['id']}/runs", headers=h.headers, json=command)
    assert response.status_code == 202, response.text
    return response.json(), command


def claim_execution(h, *, key=None):
    response = h.runner_client.post(
        "/v2/execution-worker/claim",
        headers=h.runner_headers,
        json={"idempotency_key": key or f"claim-{uuid4()}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def lease_command(lease, **changes):
    return {
        "run_id": lease["run_id"],
        "fence": lease["fence"],
        "lease_token": lease["lease_token"],
        "idempotency_key": f"step-{uuid4()}",
        **changes,
    }


def execute_step(h, lease, **changes):
    response = h.runner_client.post(
        "/v2/execution-worker/step", headers=h.runner_headers, json=lease_command(lease, **changes)
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def execution_api(client, container, auth_headers):
    configure_model_http(container)
    project = create_project(client, auth_headers)
    project_path = f"/v2/projects/{project['id']}"
    models_path = project_path + "/models"
    model, _ = enroll_model(client, models_path, auth_headers)
    probe_model(client, models_path, auth_headers, model)
    response = client.post(
        project_path + "/agents", headers=auth_headers, json=role_body(manager=True)
    )
    assert response.status_code == 201, response.text
    h = SimpleNamespace(
        client=client,
        container=container,
        headers=auth_headers,
        project=project,
        project_path=project_path,
        path=project_path + "/execution",
        agent=response.json(),
        calls=[],
        hook=None,
        actions=[
            {
                "kind": "draft",
                "summary": "Prepare the source-backed launch brief.",
                "output": "Candidate launch brief. The owner must review the unresolved direction.",
            }
        ],
    )

    def respond(request):
        h.calls.append(request)
        if h.hook:
            h.hook(request)
        result = h.actions.pop(0)
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=wire_result("openai_compatible", json.dumps(result)))

    container.project_models.transport = httpx.MockTransport(respond)
    enable_execution(h)
    h.task = create_execution_task(h)
    h.runner_command = {
        "name": "Synthetic outbound runner",
        "idempotency_key": "enroll-api-runner",
        "ttl_seconds": 900,
    }
    response = client.post(h.path + "/runners", headers=auth_headers, json=h.runner_command)
    assert response.status_code == 201, response.text
    h.runner = response.json()
    h.runner_headers = {"Authorization": "Bearer " + h.runner["token"]}
    h.runner_client = TestClient(client.app, base_url="http://localhost:8000")
    try:
        yield h
    finally:
        h.runner_client.close()


def test_browser_start_is_durable_fast_and_draft_enters_board_review(execution_api):
    h = execution_api
    run, command = start_execution(h)
    assert run["status"] == "queued" and h.calls == []
    again = h.client.post(h.path + f"/tasks/{h.task['id']}/runs", headers=h.headers, json=command)
    assert again.status_code == 202 and again.json()["id"] == run["id"]
    receipt = h.client.get(h.path + "/operations/" + command["idempotency_key"]).json()
    assert receipt == {"kind": "run", "id": run["id"]}
    lease = claim_execution(h)
    assert lease["run_id"] == run["id"]
    finished = execute_step(h, lease)
    assert finished["status"] == "completed"
    assert len(h.calls) == 1
    task = h.client.get(h.project_path + f"/tasks/{h.task['id']}").json()
    assert task["status"] == "in_review"
    detail = h.client.get(h.path + f"/runs/{run['id']}").json()
    assert detail["run"]["result_text"].startswith("Candidate launch brief")
    assert detail["totals"]["model_calls"] == 1
    assert detail["totals"]["held_microusd"] == 0
    assert "context" not in detail["run"] and "lease_token_hash" not in detail["run"]
    assert all("request" not in step for step in detail["steps"])
    assert h.runner["token"] not in json.dumps(detail)
    assert lease["lease_token"] not in json.dumps(detail)
    status = h.runner_client.get("/v2/execution-worker/runs/" + run["id"], headers=h.runner_headers)
    assert status.status_code == 200 and status.json()["status"] == "completed"


def test_runner_secret_is_one_time_and_never_in_receipt_or_project_view(execution_api):
    h = execution_api
    replay = h.client.post(h.path + "/runners", headers=h.headers, json=h.runner_command)
    assert replay.status_code == 201
    assert replay.json()["replayed"] and replay.json()["token"] is None
    receipt = h.client.get(h.path + "/operations/" + h.runner_command["idempotency_key"]).json()
    assert receipt == {"kind": "runner", "id": h.runner["runner"]["id"]}
    response = h.client.get(h.path)
    assert response.status_code == 200 and "token_hash" not in response.text
    assert h.runner["token"] not in response.text + json.dumps(receipt)


def test_lost_claim_receipt_cannot_reissue_lease_token(execution_api):
    h = execution_api
    start_execution(h)
    first = claim_execution(h, key="claim-once-private")
    assert first["lease_token"]
    assert claim_execution(h, key="claim-once-private") is None
    assert claim_execution(h) is None
    assert not h.calls


@pytest.mark.parametrize("endpoint", ["/claim", "/heartbeat", "/step"])
def test_worker_routes_reject_human_cookie_even_with_valid_runner_bearer(execution_api, endpoint):
    h = execution_api
    body = (
        {"idempotency_key": "invalid-mixed-auth"}
        if endpoint == "/claim"
        else {
            "run_id": str(uuid4()),
            "fence": 1,
            "lease_token": "private",
            "idempotency_key": "invalid-mixed-auth",
        }
    )
    response = h.client.post("/v2/execution-worker" + endpoint, headers=h.runner_headers, json=body)
    assert response.status_code == 403
    assert not h.calls


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic private"},
        {"Authorization": "Bearer simon_runner_private value"},
    ],
)
def test_worker_authentication_errors_are_safe_without_human_fallback(execution_api, headers):
    h = execution_api
    response = h.runner_client.post(
        "/v2/execution-worker/claim", headers=headers, json={"idempotency_key": "reject-bad-auth"}
    )
    assert response.status_code == 403
    assert "private" not in response.text


def test_worker_rejects_duplicate_auth_headers_and_csrf_mix(execution_api):
    h = execution_api
    for headers in [
        [("Authorization", h.runner_headers["Authorization"]), ("Authorization", "Bearer other")],
        h.runner_headers | {"X-CSRF-Token": "irrelevant"},
        h.runner_headers | {"Cookie": ""},
    ]:
        response = h.runner_client.post(
            "/v2/execution-worker/claim",
            headers=headers,
            json={"idempotency_key": "reject-mixed-auth"},
        )
        assert response.status_code == 403


def test_human_control_requires_csrf_origin_workspace_and_session(execution_api):
    h = execution_api
    command = {"expected_task_version": h.task["version"], "idempotency_key": "auth-rejected-start"}
    path = h.path + f"/tasks/{h.task['id']}/runs"
    assert h.client.post(path, json=command).status_code == 403
    for changes in [
        {"Origin": "https://untrusted.example"},
        {"X-CSRF-Token": "wrong"},
        {"X-Workspace-ID": str(uuid4())},
    ]:
        assert h.client.post(path, headers=h.headers | changes, json=command).status_code == 403
    assert h.client.get(h.path, headers={"X-Workspace-ID": str(uuid4())}).status_code == 403
    h.client.cookies.clear()
    assert h.client.get(h.path).status_code == 401
    assert h.client.post(path, headers=h.headers, json=command).status_code == 401
    assert h.calls == []


@pytest.mark.parametrize("keep_cookie", [True, False])
def test_runner_cannot_use_human_or_employee_board_authority(execution_api, keep_cookie):
    h = execution_api
    if not keep_cookie:
        h.client.cookies.clear()
    assert h.client.get(h.path, headers=h.runner_headers).status_code == 401
    assert h.client.get(h.project_path + "/team", headers=h.runner_headers).status_code == 401


def test_employee_bearer_cannot_claim_execution(client, worker):
    client.cookies.clear()
    response = client.post(
        "/v2/execution-worker/claim",
        headers=worker["headers"],
        json={"idempotency_key": "reject-employee-bearer"},
    )
    assert response.status_code == 403


def test_project_member_can_observe_but_cannot_admit_or_grant(execution_api):
    h = execution_api
    run, _ = start_execution(h)
    actor_id, headers = sign_in(h.client, h.container)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(h.project["id"]),
            actor_id=actor_id,
            role="member",
        )
    )
    assert h.client.get(h.path).status_code == 200
    assert h.client.get(h.path + f"/runs/{run['id']}").status_code == 200
    response = h.client.post(
        h.path + "/runners",
        headers=headers,
        json={"name": "Unauthorized", "idempotency_key": "member-cannot-run"},
    )
    assert response.status_code == 403
    assert h.calls == []


def test_foreign_project_cannot_observe_run_or_use_another_projects_runner(execution_api):
    h = execution_api
    run, _ = start_execution(h)
    project = create_project(h.client, h.headers, key="other-project")
    path = f"/v2/projects/{project['id']}/execution"
    assert h.client.get(path + f"/runs/{run['id']}").status_code == 404
    second = h.client.post(
        path + "/runners",
        headers=h.headers,
        json={"name": "Other project", "idempotency_key": "other-project-runner"},
    ).json()
    headers = {"Authorization": "Bearer " + second["token"]}
    response = h.runner_client.get("/v2/execution-worker/runs/" + run["id"], headers=headers)
    assert response.status_code == 404
    assert (
        h.runner_client.post(
            "/v2/execution-worker/claim",
            headers=headers,
            json={"idempotency_key": "other-project-claim"},
        ).json()
        is None
    )


def test_question_and_human_signal_resume_the_durable_run(execution_api):
    h = execution_api
    h.actions.insert(
        0,
        {
            "kind": "question",
            "summary": "Confirm the collection scope.",
            "question": "What is the approved launch deadline?",
        },
    )
    run, _ = start_execution(h)
    first_lease = claim_execution(h)
    waiting = execute_step(h, first_lease)
    assert waiting["status"] == "waiting"
    detail = h.client.get(h.path + f"/runs/{run['id']}").json()
    assert detail["can_signal"]
    correlation = detail["correlation_id"]
    assert correlation
    signal = {
        "correlation_id": correlation,
        "text": "The pilot deadline is December 1.",
        "idempotency_key": "owner-answer-deadline",
    }
    answered = h.client.post(h.path + f"/runs/{run['id']}/signals", headers=h.headers, json=signal)
    assert answered.status_code == 200, answered.text
    replay = h.client.post(h.path + f"/runs/{run['id']}/signals", headers=h.headers, json=signal)
    assert replay.status_code == 200
    next_lease = claim_execution(h)
    assert next_lease["fence"] > first_lease["fence"]
    assert execute_step(h, next_lease)["status"] == "completed"
    assert "December 1" in h.calls[-1].content.decode()


def test_revoked_runner_cannot_heartbeat_dispatch_or_observe(execution_api):
    h = execution_api
    run, _ = start_execution(h)
    lease = claim_execution(h)
    response = h.client.post(
        h.path + f"/runners/{h.runner['runner']['id']}/revoke",
        headers=h.headers,
        json={
            "expected_version": h.runner["runner"]["version"],
            "idempotency_key": "revoke-api-runner",
        },
    )
    # Claim heartbeats version the runner; use the current version for the human command.
    if response.status_code == 409:
        current = h.client.get(h.path).json()["runners"][0]
        response = h.client.post(
            h.path + f"/runners/{current['id']}/revoke",
            headers=h.headers,
            json={
                "expected_version": current["version"],
                "idempotency_key": "revoke-api-runner-current",
            },
        )
    assert response.status_code == 200, response.text
    for path in ("/heartbeat", "/step"):
        response = h.runner_client.post(
            "/v2/execution-worker" + path, headers=h.runner_headers, json=lease_command(lease)
        )
        assert response.status_code == 403
    assert (
        h.runner_client.get(
            "/v2/execution-worker/runs/" + run["id"], headers=h.runner_headers
        ).status_code
        == 403
    )
    assert h.calls == []


def test_cancel_fences_existing_lease_and_retry_requires_safe_state(execution_api):
    h = execution_api
    run, _ = start_execution(h)
    lease = claim_execution(h)
    current = h.client.get(h.path + f"/runs/{run['id']}").json()["run"]
    response = h.client.post(
        h.path + f"/runs/{run['id']}/cancel",
        headers=h.headers,
        json={"expected_version": current["version"], "idempotency_key": "cancel-current-run"},
    )
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    rejected = h.runner_client.post(
        "/v2/execution-worker/step", headers=h.runner_headers, json=lease_command(lease)
    )
    assert rejected.status_code in {403, 409}
    retry = h.client.post(
        h.path + f"/runs/{run['id']}/retry",
        headers=h.headers,
        json={
            "expected_version": response.json()["version"],
            "idempotency_key": "retry-cancelled-run",
        },
    )
    assert retry.status_code == 409
    assert h.calls == []


def test_workflow_dependencies_cycles_schedule_config_and_pagination(execution_api):
    h = execution_api
    second = create_execution_task(h, title="Prepare launch calendar")
    response = h.client.put(
        h.path + f"/tasks/{second['id']}/workflow",
        headers=h.headers,
        json={
            "expected_version": 0,
            "dependency_ids": [h.task["id"]],
            "idempotency_key": "dependency-save",
        },
    )
    assert response.status_code == 200, response.text
    cycle = h.client.put(
        h.path + f"/tasks/{h.task['id']}/workflow",
        headers=h.headers,
        json={
            "expected_version": 0,
            "dependency_ids": [second["id"]],
            "idempotency_key": "reject-cycle",
        },
    )
    assert cycle.status_code == 422
    config = h.client.get(h.path + f"/tasks/{second['id']}/workflow").json()
    assert config["config"]["dependency_ids"] == [h.task["id"]]
    schedule = h.client.post(
        h.path + "/schedules",
        headers=h.headers,
        json={
            "task_id": h.task["id"],
            "expected_task_version": h.task["version"],
            "next_run_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "interval_seconds": 86400,
            "timezone": "America/New_York",
            "max_occurrences": 2,
            "idempotency_key": "create-recurring-schedule",
        },
    )
    assert schedule.status_code == 201, schedule.text
    saved = schedule.json()
    disabled = h.client.put(
        h.path + "/schedules/" + saved["id"],
        headers=h.headers,
        json={
            "expected_version": saved["version"],
            "enabled": False,
            "idempotency_key": "disable-recurring-schedule",
        },
    )
    assert disabled.status_code == 200
    assert disabled.json()["definition_version"] == saved["definition_version"] + 1
    assert h.client.get(h.path + "/schedules?limit=1").json()["items"][0]["id"] == saved["id"]
    run, _ = start_execution(h)
    assert h.client.get(h.path + "/runs?limit=1").json()["items"][0]["id"] == run["id"]
    assert h.client.get(h.path + f"/runs/{run['id']}/events?limit=1").json()["items"]
    assert h.client.get(h.path + "/runs?limit=101").status_code == 422


def test_validation_never_echoes_lease_token_or_permits_hidden_dispatch_fields(execution_api):
    h = execution_api
    for body in [
        {
            "run_id": "invalid",
            "fence": 1,
            "lease_token": "private-lease",
            "idempotency_key": "invalid-lease-command",
        },
        {
            "run_id": str(uuid4()),
            "fence": 1,
            "lease_token": "private-lease",
            "idempotency_key": "invalid-lease-command",
            "prompt": "override",
        },
    ]:
        response = h.runner_client.post(
            "/v2/execution-worker/step", headers=h.runner_headers, json=body
        )
        assert response.status_code == 422
        assert "private-lease" not in response.text and "override" not in response.text
    assert h.calls == []


@pytest.mark.parametrize("changed", [{"fence": 999}, {"lease_token": "wrong-lease-private"}])
def test_wrong_lease_cannot_dispatch_or_renew(execution_api, changed):
    h = execution_api
    start_execution(h)
    lease = claim_execution(h)
    for path in ("/step", "/heartbeat"):
        response = h.runner_client.post(
            "/v2/execution-worker" + path,
            headers=h.runner_headers,
            json=lease_command(lease, **changed),
        )
        assert response.status_code in {403, 409}
        assert "wrong-lease-private" not in response.text
    assert not h.calls
    assert execute_step(h, lease)["status"] == "completed"


def test_known_failed_action_can_retry_without_replaying_original_model_call(execution_api):
    h = execution_api
    h.actions.insert(0, {"kind": "draft", "summary": "Invalid candidate without output"})
    run, _ = start_execution(h)
    lease = claim_execution(h)
    failed = execute_step(h, lease)
    assert failed["status"] == "failed"
    assert len(h.calls) == 1
    detail = h.client.get(h.path + f"/runs/{run['id']}").json()
    assert detail["can_retry"]
    assert detail["totals"]["held_microusd"] == 0
    command = {
        "expected_version": failed["version"],
        "idempotency_key": "retry-known-failed-action",
    }
    response = h.client.post(h.path + f"/runs/{run['id']}/retry", headers=h.headers, json=command)
    assert response.status_code == 202, response.text
    assert response.json()["id"] == run["id"] and response.json()["attempt"] > failed["attempt"]
    assert len(h.calls) == 1
    second = claim_execution(h)
    assert second["fence"] > lease["fence"]
    finished = execute_step(h, second)
    assert finished["status"] == "completed" and len(h.calls) == 2
    detail = h.client.get(h.path + f"/runs/{run['id']}").json()
    assert len(detail["steps"]) == 2
    assert {step["status"] for step in detail["steps"]} == {"failed", "completed"}


def test_known_paid_usage_settles_after_execution_grant_changes_mid_response(execution_api):
    h = execution_api
    transport = h.container.project_models.transport
    configure_model_http(h.container, paid=True)
    models_path = h.project_path + "/models"
    grant_budget(h.client, models_path, h.headers, workspace=True)
    grant_budget(h.client, models_path, h.headers)
    model = h.container.store.project_models(DEV_WORKSPACE_ID, UUID(h.project["id"]))[0]
    probe_model(h.client, models_path, h.headers, model.model_dump(mode="json"))
    h.container.project_models.transport = transport
    enable_execution(h, max_cost_microusd=100_000)
    run, _ = start_execution(h)
    lease = claim_execution(h)
    h.hook = lambda _: enable_execution(h, enabled=False)
    result = execute_step(h, lease)
    assert result["status"] == "stale"
    assert not result["result_text"]
    detail = h.client.get(h.path + f"/runs/{run['id']}").json()
    assert detail["totals"]["charged_microusd"] == 163
    assert detail["totals"]["held_microusd"] == 0
    assert len(h.calls) == 1


def test_normal_wait_resumption_does_not_consume_retry_allowance(execution_api):
    h = execution_api
    enable_execution(h, max_attempts=1)
    draft = h.actions[0]
    h.actions = [
        {
            "kind": "question",
            "summary": f"Clarify requirement {number}.",
            "question": f"What is requirement {number}?",
        }
        for number in range(4)
    ] + [draft]
    run, _ = start_execution(h)
    for number in range(4):
        lease = claim_execution(h)
        waiting = execute_step(h, lease)
        assert waiting["status"] == "waiting" and waiting["attempt"] == 1
        detail = h.client.get(h.path + f"/runs/{run['id']}").json()
        response = h.client.post(
            h.path + f"/runs/{run['id']}/signals",
            headers=h.headers,
            json={
                "correlation_id": detail["correlation_id"],
                "text": f"Owner answer {number}.",
                "idempotency_key": f"answer-normal-wait-{number}",
            },
        )
        assert response.status_code == 200, response.text
    final = execute_step(h, claim_execution(h))
    assert final["status"] == "completed" and final["attempt"] == 1
    assert len(h.calls) == 5


def test_recovered_checkpoint_command_is_idempotent_before_next_operation(
    execution_api, monkeypatch
):
    h = execution_api
    h.actions = [
        {
            "kind": "reference",
            "summary": "Check the local operation boundary.",
            "operation": "echo",
            "text": "Reference example",
        }
    ]
    service = h.container.native_execution
    advance = service._advance_checkpoint
    monkeypatch.setattr(service, "_advance_checkpoint", lambda run, _step: run)
    run, _ = start_execution(h)
    lease = claim_execution(h)
    first = execute_step(h, lease)
    assert first["applied_step"] == 0 and first["step_number"] == 1
    monkeypatch.setattr(service, "_advance_checkpoint", advance)
    command = lease_command(lease, idempotency_key="recover-checkpoint-once")
    path = "/v2/execution-worker/step"
    recovered = h.runner_client.post(path, headers=h.runner_headers, json=command)
    assert recovered.status_code == 200
    assert recovered.json()["applied_step"] == 1 and recovered.json()["step_number"] == 2
    replay = h.runner_client.post(path, headers=h.runner_headers, json=command)
    assert replay.status_code == 200 and replay.json() == recovered.json()
    detail = h.client.get(h.path + f"/runs/{run['id']}").json()
    assert [step["status"] for step in detail["steps"]] == ["completed", "prepared"]
    assert len(h.calls) == 1


def test_late_old_response_cannot_terminate_a_new_reconciled_lease(execution_api, monkeypatch):
    h = execution_api
    h.actions.append(
        {
            "kind": "draft",
            "summary": "New attempt candidate.",
            "output": "New candidate after explicit recovery.",
        }
    )
    arrived, release = Event(), Event()

    def hold_response(_request):
        arrived.set()
        assert release.wait(20)

    h.hook = hold_response
    run, _ = start_execution(h)
    old_lease = claim_execution(h)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_step, h, old_lease)
        try:
            assert arrived.wait(5)
            future_time = datetime.now(UTC) + timedelta(seconds=301)
            monkeypatch.setattr(native_execution, "utc_now", lambda: future_time)
            monkeypatch.setattr(model_usage, "utc_now", lambda: future_time)
            detail = h.client.get(h.path + f"/runs/{run['id']}").json()
            assert detail["run"]["status"] == "unknown"
            usage_id = detail["steps"][0]["usage_id"]
            usage = h.container.store.model_usage(
                DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(usage_id)
            )
            reconciled = h.client.post(
                h.project_path + f"/models/usage/{usage_id}/reconcile",
                headers=h.headers,
                json={
                    "expected_version": usage.version,
                    "charged_microusd": 0,
                    "reason": "Synthetic local request has no API charge.",
                    "evidence": "No paid provider is configured in this test.",
                    "idempotency_key": "reconcile-old-response",
                },
            )
            assert reconciled.status_code == 200, reconciled.text
            retried = h.client.post(
                h.path + f"/runs/{run['id']}/retry",
                headers=h.headers,
                json={
                    "expected_version": detail["run"]["version"],
                    "idempotency_key": "retry-old-response",
                },
            )
            assert retried.status_code == 202, retried.text
            successor = claim_execution(h)
            assert successor["fence"] > old_lease["fence"]
        finally:
            release.set()
        old_result = future.result(timeout=5)
    assert old_result["status"] == "running" and old_result["fence"] == successor["fence"]
    h.hook = None
    final = execute_step(h, successor)
    assert final["status"] == "completed"
    assert final["result_text"] == "New candidate after explicit recovery."
    assert len(h.calls) == 2


def test_idle_claims_do_not_accumulate_receipts_or_write_presence_every_poll(execution_api):
    h = execution_api
    runner_id = UUID(h.runner["runner"]["id"])
    namespace = f"execution_claim:{runner_id}"
    assert claim_execution(h, key="empty-then-admitted") is None
    first = h.container.store.execution_runner(DEV_WORKSPACE_ID, UUID(h.project["id"]), runner_id)
    for number in range(10):
        key = f"empty-poll-{number}"
        assert claim_execution(h, key=key) is None
        assert h.container.store.command_receipt(namespace, key) is None
    latest = h.container.store.execution_runner(DEV_WORKSPACE_ID, UUID(h.project["id"]), runner_id)
    assert latest.version == first.version and latest.last_seen_at == first.last_seen_at
    assert h.container.store.command_receipt(namespace, "empty-then-admitted") is None
    run, _ = start_execution(h)
    lease = claim_execution(h, key="empty-then-admitted")
    assert lease["run_id"] == run["id"]
    assert h.container.store.command_receipt(namespace, "empty-then-admitted") == {
        "run_id": run["id"]
    }
    assert claim_execution(h, key="empty-then-admitted") is None


def test_dependency_cancellation_during_model_call_prevents_candidate_publication(execution_api):
    h = execution_api
    start_execution(h)
    execute_step(h, claim_execution(h))
    dependent = create_execution_task(h, title="Build the calendar from the candidate brief")
    response = h.client.put(
        h.path + f"/tasks/{dependent['id']}/workflow",
        headers=h.headers,
        json={
            "expected_version": 0,
            "dependency_ids": [h.task["id"]],
            "idempotency_key": "dependent-calendar-config",
        },
    )
    assert response.status_code == 200
    h.actions = [
        {"kind": "draft", "summary": "Candidate calendar.", "output": "Obsolete dependent draft."}
    ]
    run, _ = start_execution(h, dependent)

    def cancel_dependency(_request):
        current = h.client.get(h.project_path + f"/tasks/{h.task['id']}").json()
        response = h.client.put(
            h.project_path + f"/tasks/{h.task['id']}",
            headers=h.headers,
            json={
                "expected_version": current["version"],
                "title": current["title"],
                "description": current["description"],
                "assignment": current["assignment"],
                "status": "cancelled",
                "idempotency_key": "cancel-dependency-during-call",
            },
        )
        assert response.status_code == 200, response.text

    h.hook = cancel_dependency
    failed = execute_step(h, claim_execution(h))
    assert failed["status"] == "failed" and not failed["result_text"]
    current = h.client.get(h.project_path + f"/tasks/{dependent['id']}").json()
    assert current["status"] == "in_progress"
    detail = h.client.get(h.path + f"/runs/{run['id']}").json()
    assert detail["steps"][0]["status"] == "completed"
    assert detail["totals"]["held_microusd"] == 0


def test_each_model_call_receives_current_remaining_execution_allowance(execution_api):
    h = execution_api
    enable_execution(h, max_steps=4, max_model_calls=2)
    h.actions.insert(
        0,
        {
            "kind": "reference",
            "summary": "Exercise the local operation.",
            "operation": "echo",
            "text": "Reference data",
        },
    )
    start_execution(h)
    lease = claim_execution(h)
    assert execute_step(h, lease)["status"] == "running"
    assert execute_step(h, lease)["status"] == "running"
    assert execute_step(h, lease)["status"] == "completed"

    def allowance(request):
        prompt = json.loads(request.content)["messages"][1]["content"]
        context = prompt.split("Current task and authority context (data):\n", 1)[1].split(
            "\nCompleted workflow history (data):\n", 1
        )[0]
        return json.loads(context)["execution"]

    first, second = [allowance(request) for request in h.calls]
    assert first["step_number"] == 1 and first["steps_remaining"] == 4
    assert first["model_calls_remaining"] == 2
    assert second["step_number"] == 3 and second["steps_remaining"] == 2
    assert second["model_calls_remaining"] == 1
    assert first["cost_remaining_microusd"] == second["cost_remaining_microusd"] == 0
    assert first["children_remaining"] == second["children_remaining"] == 8
    assert first["deadline_at"] == second["deadline_at"]
