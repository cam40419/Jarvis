"""Manual commands and replacement requests retain execution-review boundaries."""

from uuid import UUID

import pytest

from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import ActorContext, Channel, JobStatus
from simon.domain.project_work import ProjectCycleUpdate
from simon.services.identity import ROLE_SCOPES
from tests.contract.test_project_history import saved_run
from tests.integration.test_agent_profiles_api import profile_api as profile_api
from tests.integration.test_project_member_api import member_api as member_api


@pytest.fixture
def command_api(client, container, auth_headers, member_api):
    project_id, team = member_api
    container.agent_runs.enabled = True
    response = client.patch(
        f"/v1/projects/{project_id}/team",
        headers=auth_headers,
        json={"expected_version": 0, "team": team},
    )
    assert response.status_code == 200, response.text
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )
    return UUID(project_id), actor, response.json()


def test_user_command_on_scheduled_project_stays_manual(
    client, container, auth_headers, command_api
):
    project_id, _actor, state = command_api
    base = f"/v1/projects/{project_id}"
    configured = client.patch(
        base + "/team",
        headers=auth_headers,
        json={
            "expected_version": state["version"],
            "autonomy": {
                "mode": "scheduled",
                "objective": "Weekly standing work",
                "cadence_minutes": 60,
                "max_cycles": 2,
                "model_budget_usd": 1,
            },
        },
    )
    assert configured.status_code == 200, configured.text
    state = configured.json()
    response = client.post(
        base + "/command",
        headers=auth_headers,
        json={"instruction": "A specific manual request", "idempotency_key": "manual-scheduled"},
    )
    assert response.status_code == 202, response.text
    value = response.json()
    assert value["autonomy"]["mode"] == "scheduled"
    assert value["scheduled_cycles_used"] == state["scheduled_cycles_used"] == 0
    assert value["active_cycle"]["automatic"] is False
    assert value["active_cycle"]["execution_approved"] is False
    assert value["active_cycle"]["instruction"] == "A specific manual request"


def test_replacement_command_requires_csrf_current_version_and_explicit_choice(
    client,
    container,
    auth_headers,
    command_api,
):
    project_id, actor, _state = command_api
    work = container.project_work
    before = work.request_cycle(actor, project_id, "Original", "api-original")
    before = work.update_cycle_atomic(
        actor,
        project_id,
        before.active_cycle.id,
        lambda current: ProjectCycleUpdate(
            cycle=current.active_cycle.model_copy(update={"phase": "blocked", "error": "No plan"})
        ),
    )
    base = f"/v1/projects/{project_id}/command"
    body = {
        "instruction": "New request",
        "idempotency_key": "replace-api-failed",
        "replace_failed": True,
        "expected_version": before.version,
    }
    assert client.post(base, json=body).status_code == 403
    unversioned = {key: value for key, value in body.items() if key != "expected_version"}
    assert client.post(base, headers=auth_headers, json=unversioned).status_code == 422
    for change in ({"replace_failed": False}, {"expected_version": before.version - 1}):
        assert client.post(base, headers=auth_headers, json={**body, **change}).status_code == 409
    assert work.get(actor, project_id) == before
    response = client.post(base, headers=auth_headers, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["active_cycle"]["instruction"] == "New request"
    assert response.json()["last_cycle"] == before.last_cycle.model_dump(mode="json")
    assert client.post(base, headers=auth_headers, json=body).json() == response.json()


@pytest.mark.parametrize("run_status", [JobStatus.NEEDS_HUMAN, JobStatus.FAILED, JobStatus.RUNNING])
def test_replacement_api_rechecks_linked_run_even_when_cycle_says_blocked(
    client,
    container,
    auth_headers,
    command_api,
    run_status,
):
    project_id, actor, _state = command_api
    work = container.project_work
    current = work.request_cycle(actor, project_id, "Original", "api-run-original")
    saved = saved_run(container.store, project_id, actor=actor, cycle_id=current.active_cycle.id)
    saved = container.agent_runs.update(
        saved.id,
        lambda run: run.model_copy(
            update={
                "status": run_status,
                "tasks": tuple(task.model_copy(update={"status": "unknown"}) for task in run.tasks),
            }
        ),
    )
    before = work.update_cycle_atomic(
        actor,
        project_id,
        current.active_cycle.id,
        lambda snapshot: ProjectCycleUpdate(
            cycle=snapshot.active_cycle.model_copy(
                update={
                    "phase": "blocked",
                    "error": "Legacy blocked outcome",
                    "planning_run_id": saved.id,
                    "planning_plan_id": saved.plan_id,
                }
            )
        ),
    )
    response = client.post(
        f"/v1/projects/{project_id}/command",
        headers=auth_headers,
        json={
            "instruction": "New request",
            "idempotency_key": "reject-uncertain-run",
            "replace_failed": True,
            "expected_version": before.version,
        },
    )
    assert response.status_code == 409, response.text
    assert work.get(actor, project_id) == before
    assert container.agent_runs.get(actor, saved.id) == saved
