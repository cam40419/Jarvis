from datetime import UTC, datetime, timedelta

from tests.integration.test_agent_profiles_api import profile_api as profile_api
from tests.integration.test_project_command_api import command_api as command_api
from tests.integration.test_project_member_api import member_api as member_api


def test_project_queue_and_wait_api_are_durable_without_model_calls(
    client, container, auth_headers, command_api
):
    project_id, actor, _ = command_api
    base = f"/v1/projects/{project_id}"
    payload = {"instruction": "Draft a project update", "idempotency_key": "queued-followup"}
    assert client.post(base + "/requests", json=payload).status_code == 403
    saved = client.post(base + "/requests", headers=auth_headers, json=payload)
    assert saved.status_code == 202, saved.text
    assert (
        client.post(base + "/requests", headers=auth_headers, json=payload).json()["id"]
        == saved.json()["id"]
    )
    snapshot = client.get(base + "/command", headers=auth_headers).json()
    assert snapshot["continuity"]["requests"][0]["instruction"] == payload["instruction"]
    assert snapshot["state"]["active_cycle"] is None
    wait = client.post(
        base + "/waits",
        headers=auth_headers,
        json={
            "question": "Which date?",
            "instruction": "Prepare the launch schedule",
            "idempotency_key": "saved-question",
        },
    )
    assert wait.status_code == 201, wait.text
    wait = wait.json()
    reply = {
        "expected_version": wait["version"],
        "message": "October 30",
        "idempotency_key": "owner-reply",
    }
    response = client.post(base + f"/waits/{wait['id']}/reply", headers=auth_headers, json=reply)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "replied"
    assert (
        client.post(base + f"/waits/{wait['id']}/reply", headers=auth_headers, json=reply).json()
        == response.json()
    )
    assert container.project_work.get(actor, project_id).active_cycle is None
    another = client.post(
        base + "/waits",
        headers=auth_headers,
        json={
            "question": "Which supplier?",
            "instruction": "Prepare the supplier request",
            "idempotency_key": "cancel-question",
        },
    ).json()
    cancelled = client.post(
        base + f"/waits/{another['id']}/cancel",
        headers=auth_headers,
        json={"expected_version": another["version"]},
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (
        client.post(
            base + f"/waits/{another['id']}/reply",
            headers=auth_headers,
            json={
                "expected_version": another["version"],
                "message": "Too late",
                "idempotency_key": "cancelled-reply",
            },
        ).status_code
        == 409
    )


def test_calendar_api_requires_bounded_explicit_schedule_and_version(
    client, auth_headers, command_api
):
    project_id, _, state = command_api
    base = f"/v1/projects/{project_id}"
    payload = {
        "name": "One report",
        "instruction": "Prepare a saved project report",
        "kind": "once",
        "run_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "timezone": "America/New_York",
        "agent_id": state["team"]["lead_agent_id"],
        "model_budget_usd": 1,
        "max_runs": 1,
        "idempotency_key": "calendar-once",
    }
    missing = dict(payload)
    missing.pop("model_budget_usd")
    assert client.post(base + "/schedules", headers=auth_headers, json=missing).status_code == 422
    assert client.post(base + "/schedules", json=payload).status_code == 403
    response = client.post(base + "/schedules", headers=auth_headers, json=payload)
    assert response.status_code == 201, response.text
    schedule = response.json()
    disabled = client.patch(
        base + f"/schedules/{schedule['id']}",
        headers=auth_headers,
        json={"expected_version": schedule["version"], "enabled": False},
    )
    assert disabled.status_code == 200 and not disabled.json()["enabled"]
    assert (
        client.patch(
            base + f"/schedules/{schedule['id']}",
            headers=auth_headers,
            json={"expected_version": schedule["version"], "enabled": True},
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + "/continue",
            headers=auth_headers,
            json={"expected_version": state["version"], "idempotency_key": "cannot-continue"},
        ).status_code
        == 409
    )
