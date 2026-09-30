from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo


def test_recurring_schedule_fires_advances_and_can_be_managed(
    client, auth_headers, container
):
    clock = [datetime(2026, 3, 7, 14, 0, tzinfo=UTC)]
    container.workflows.clock = lambda: clock[0]
    workflow = client.post(
        "/v1/workflows",
        headers=auth_headers,
        json={
            "spec": {
                "name": "Daily light check",
                "steps": [{"id": "check", "action": "system.echo"}],
            },
            "expected_version": 0,
            "idempotency_key": str(uuid4()),
        },
    ).json()
    first = clock[0] + timedelta(minutes=5)
    response = client.post(
        f"/v1/workflows/{workflow['id']}/schedules",
        headers=auth_headers,
        json={
            "definition_version": workflow["version"],
            "frequency": "daily",
            "start_at": first.isoformat(),
            "time_zone": "America/New_York",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201
    schedule = response.json()
    assert schedule["enabled"] is True and schedule["next_run_at"] == first.isoformat().replace(
        "+00:00", "Z"
    )

    clock[0] = first
    container.workflows.tick("schedule-test")
    container.workflows.tick("schedule-test")
    runs = client.get("/v1/workflow-runs").json()
    assert len(runs) == 1 and runs[0]["status"] == "succeeded"
    schedule = client.get("/v1/workflow-schedules").json()[0]
    next_local = datetime.fromisoformat(schedule["next_run_at"]).astimezone(
        ZoneInfo("America/New_York")
    )
    assert next_local.hour == 9 and next_local.date().isoformat() == "2026-03-08"

    paused = client.post(
        f"/v1/workflow-schedules/{schedule['id']}/control",
        headers=auth_headers,
        json={"action": "pause", "expected_version": schedule["version"]},
    ).json()
    assert paused["enabled"] is False and paused["next_run_at"] is None
    resumed = client.post(
        f"/v1/workflow-schedules/{schedule['id']}/control",
        headers=auth_headers,
        json={"action": "resume", "expected_version": paused["version"]},
    ).json()
    assert resumed["enabled"] is True and resumed["next_run_at"] is not None
    deleted = client.delete(
        f"/v1/workflow-schedules/{schedule['id']}?expected_version={resumed['version']}",
        headers=auth_headers,
    )
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert client.get("/v1/workflow-schedules").json() == []


def test_print_event_trigger_is_created_and_can_be_paused(client, auth_headers):
    response = client.post(
        "/v1/workflows",
        headers=auth_headers,
        json={
            "spec": {
                "name": "Print air cleaning",
                "steps": [
                    {"id": "done", "kind": "condition",
                     "condition": "printer.not_printing"},
                ],
            },
            "expected_version": 0,
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201
    definition = response.json()
    for kind in ("printer.print_started", "printer.print_finished"):
        created = client.post(
            f"/v1/workflows/{definition['id']}/triggers",
            headers=auth_headers,
            json={
                "definition_version": definition["version"],
                "trigger": {"kind": kind},
                "idempotency_key": str(uuid4()),
            },
        )
        assert created.status_code == 201
    attached = client.get("/v1/workflow-triggers", headers=auth_headers).json()
    assert len(attached) == 2
    trigger = next(item for item in attached if item["kind"] == "printer.print_started")
    assert trigger["definition_id"] == definition["id"]
    assert trigger["kind"] == "printer.print_started" and trigger["enabled"] is True

    paused = client.post(
        f"/v1/workflow-triggers/{trigger['id']}/control",
        headers=auth_headers,
        json={"action": "pause", "expected_version": trigger["version"]},
    )
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False and paused.json()["next_check_at"] is None
