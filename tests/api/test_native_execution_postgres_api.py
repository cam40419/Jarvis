"""Real PostgreSQL workflow restart, lease races and committed checkpoint recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from simon.domain.identity import DEV_WORKSPACE_ID
from simon.domain.models import utc_now
from tests.api.test_native_models_api import configure_model_http, enroll_model, probe_model
from tests.api.test_native_project_api import create_project
from tests.api.test_native_project_postgres_api import login_owner
from tests.api.test_native_project_postgres_api import postgres_native_api as postgres_native_api
from tests.api.test_native_team_api import role_body

pytestmark = pytest.mark.postgres


def model_actions(container, *actions):
    state = SimpleNamespace(calls=[], actions=list(actions))

    def answer(request):
        state.calls.append(request)
        assert state.actions, "Unexpected repeated model dispatch"
        action = state.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return httpx.Response(
            200,
            json={
                "model": json.loads(request.content)["model"],
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(action)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    container.project_models.transport = httpx.MockTransport(answer)
    return state


def setup_execution_api(client, container):
    headers = login_owner(client)
    configure_model_http(container, preserve_cipher=True)
    project = create_project(client, headers)
    project_path = f"/v2/projects/{project['id']}"
    model, _ = enroll_model(client, project_path + "/models", headers)
    checked, _ = probe_model(client, project_path + "/models", headers, model)
    assert checked["ready"]
    response = client.post(project_path + "/agents", headers=headers, json=role_body())
    assert response.status_code == 201, response.text
    agent = response.json()
    response = client.post(
        project_path + "/tasks",
        headers=headers,
        json={
            "title": "Prepare a native execution candidate",
            "description": "Make a clearly labeled draft for review.",
            "assignment": {"kind": "agent", "agent_id": agent["id"]},
            "idempotency_key": "execution-task-create",
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    path = project_path + "/execution"
    response = client.put(
        path + "/policy",
        headers=headers,
        json={
            "expected_version": 0,
            "enabled": True,
            "idempotency_key": "enable-execution",
        },
    )
    assert response.status_code == 200, response.text
    enrollment = {"name": "Disposable native runner", "idempotency_key": "execution-runner-enroll"}
    response = client.post(path + "/runners", headers=headers, json=enrollment)
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    command = {"expected_task_version": task["version"], "idempotency_key": "execution-start"}
    response = client.post(path + f"/tasks/{task['id']}/runs", headers=headers, json=command)
    assert response.status_code == 202, response.text
    return SimpleNamespace(
        headers=headers,
        project=project,
        path=path,
        project_path=project_path,
        task=task,
        token=token,
        enrollment=enrollment,
        command=command,
        run=response.json(),
    )


def worker_post(client, token, action, body):
    request = client.build_request(
        "POST",
        "/v2/execution-worker/" + action,
        headers={"Authorization": "Bearer " + token},
        json=body,
    )
    request.headers.pop("cookie", None)
    return client.send(request)


def claim(client, h, key=None):
    response = worker_post(client, h.token, "claim", {"idempotency_key": key or str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def step(client, h, lease, key=None):
    response = worker_post(
        client,
        h.token,
        "step",
        {
            "run_id": lease["run_id"],
            "fence": lease["fence"],
            "lease_token": lease["lease_token"],
            "idempotency_key": key or str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_native_wait_signal_candidate_and_receipts_survive_app_restart(postgres_native_api):
    with postgres_native_api() as (client, container):
        h = setup_execution_api(client, container)
        state = model_actions(
            container,
            {
                "kind": "question",
                "summary": "Need direction",
                "question": "Which audience should this address?",
            },
        )
        lease = claim(client, h)
        waiting = step(client, h, lease)
        assert waiting["status"] == "waiting" and len(state.calls) == 1
        before = client.get(h.path + "/runs/" + h.run["id"]).json()
        assert before["waits"][0]["status"] == "pending"
        cookies = dict(client.cookies)
    with postgres_native_api(cookies=cookies) as (client, container):
        configure_model_http(container, preserve_cipher=True)
        state = model_actions(
            container,
            {
                "kind": "draft",
                "summary": "Ready for review",
                "output": "Candidate for the confirmed audience.",
            },
        )
        replay = client.post(
            h.path + f"/tasks/{h.task['id']}/runs", headers=h.headers, json=h.command
        )
        assert replay.status_code == 202 and replay.json()["id"] == h.run["id"]
        assert claim(client, h) is None
        resumed = client.post(
            h.path + f"/runs/{h.run['id']}/signals",
            headers=h.headers,
            json={
                "correlation_id": before["waits"][0]["correlation_id"],
                "text": "Independent artists",
                "idempotency_key": "owner-audience-answer",
            },
        )
        assert resumed.status_code == 200, resumed.text
        lease = claim(client, h)
        finished = step(client, h, lease)
        assert finished["status"] == "completed" and len(state.calls) == 1
        assert "Independent artists" in state.calls[0].content.decode()
        task = client.get(h.project_path + "/tasks/" + h.task["id"]).json()
        assert task["status"] == "in_review"
        detail = client.get(h.path + "/runs/" + h.run["id"]).json()
        assert len(detail["steps"]) == 2 and detail["waits"][0]["status"] == "resolved"
        assert "lease_token_hash" not in json.dumps(detail)
        assert h.token not in json.dumps(detail)
        assert all("request" not in record for record in detail["steps"])
        enrollment = client.post(h.path + "/runners", headers=h.headers, json=h.enrollment)
        assert enrollment.status_code == 201 and enrollment.json()["token"] is None


def test_two_postgres_apps_claim_once_and_expired_lease_is_fenced(postgres_native_api):
    with postgres_native_api() as (client, container), postgres_native_api() as (other, second):
        h = setup_execution_api(client, container)
        configure_model_http(second, preserve_cipher=True)
        barrier = Barrier(2)

        def competing(worker_client):
            barrier.wait(timeout=10)
            return claim(worker_client, h)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(competing, (client, other)))
        leases = [item for item in results if item is not None]
        assert len(leases) == 1
        first = leases[0]
        current = container.store.execution_run(
            DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(h.run["id"])
        )
        container.store.update_execution_run(
            current.model_copy(
                update={
                    "version": current.version + 1,
                    "lease_until": utc_now() - timedelta(seconds=1),
                }
            ),
            current.version,
        )
        replacement = claim(other, h)
        assert replacement["fence"] == first["fence"] + 1
        stale = worker_post(
            client,
            h.token,
            "heartbeat",
            {
                "run_id": first["run_id"],
                "fence": first["fence"],
                "lease_token": first["lease_token"],
                "idempotency_key": "old-worker-heartbeat",
            },
        )
        assert stale.status_code == 409, stale.text
        state = model_actions(
            second,
            {
                "kind": "draft",
                "summary": "Completed after takeover",
                "output": "Only the current lease can publish this candidate.",
            },
        )
        assert step(other, h, replacement)["status"] == "completed"
        assert len(state.calls) == 1


def test_restart_applies_committed_checkpoint_without_another_model_call(
    postgres_native_api, monkeypatch
):
    with postgres_native_api() as (client, container):
        h = setup_execution_api(client, container)
        state = model_actions(
            container,
            {
                "kind": "draft",
                "summary": "Durable candidate",
                "output": "Checkpoint survives the controller crash.",
            },
        )
        lease = claim(client, h)

        def crash(*args):
            raise RuntimeError("Synthetic crash after committed provider checkpoint")

        monkeypatch.setattr(container.native_execution, "_apply_checkpoint", crash)
        with pytest.raises(RuntimeError, match="committed provider checkpoint"):
            step(client, h, lease)
        run = container.store.execution_run(
            DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(h.run["id"])
        )
        assert run.step_number == 1 and run.applied_step == 0 and len(state.calls) == 1
        checkpoints = container.store.execution_steps(DEV_WORKSPACE_ID, run.project_id, run.id)
        assert checkpoints[0].status == "completed"
        container.store.update_execution_run(
            run.model_copy(
                update={
                    "version": run.version + 1,
                    "lease_until": utc_now() - timedelta(seconds=1),
                }
            ),
            run.version,
        )
        cookies = dict(client.cookies)
    with postgres_native_api(cookies=cookies) as (client, container):
        configure_model_http(container, preserve_cipher=True)
        state = model_actions(container)
        replacement = claim(client, h)
        finished = step(client, h, replacement)
        assert finished["status"] == "completed"
        assert finished["result_text"] == "Checkpoint survives the controller crash."
        assert state.calls == []
        detail = client.get(h.path + "/runs/" + h.run["id"]).json()
        assert len(detail["steps"]) == 1


def test_unknown_dispatched_model_survives_restart_without_claim_or_resend(postgres_native_api):
    with postgres_native_api() as (client, container):
        h = setup_execution_api(client, container)
        state = model_actions(container, httpx.ReadTimeout("Synthetic provider response loss"))
        lease = claim(client, h)
        uncertain = step(client, h, lease)
        assert uncertain["status"] == "unknown" and len(state.calls) == 1
        before = client.get(h.path + "/runs/" + h.run["id"]).json()
        cookies = dict(client.cookies)
    with postgres_native_api(cookies=cookies) as (client, container):
        configure_model_http(container, preserve_cipher=True)
        state = model_actions(container)
        assert claim(client, h) is None
        after = client.get(h.path + "/runs/" + h.run["id"]).json()
        assert after["run"]["status"] == "unknown"
        assert after["steps"] == before["steps"]
        assert state.calls == []
