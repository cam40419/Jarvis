from uuid import UUID, uuid4

from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.domain.models import ActorContext, Channel
from simon.services.identity import ROLE_SCOPES


def task_body(title: str, priority: int = 3) -> dict[str, object]:
    return {
        "title": title,
        "instructions": f"Research and summarize {title}",
        "task_type": "research",
        "priority": priority,
        "idempotency_key": str(uuid4()),
    }


def test_assistant_tasks_can_be_edited_reordered_steered_and_cancelled(
    client, auth_headers
):
    assert client.post("/v1/assistant-tasks", json=task_body("private")).status_code == 403
    first_response = client.post(
        "/v1/assistant-tasks", headers=auth_headers, json=task_body("First", 3)
    )
    second_response = client.post(
        "/v1/assistant-tasks", headers=auth_headers, json=task_body("Second", 2)
    )
    assert first_response.status_code == 202
    first, second = first_response.json(), second_response.json()
    assert first["status"] == "queued" and first["progress"] == 0

    edited = client.patch(
        f"/v1/assistant-tasks/{first['id']}",
        headers=auth_headers,
        json={
            "expected_version": first["version"],
            "title": "First revised",
            "task_type": "work",
            "priority": 4,
        },
    )
    assert edited.status_code == 200
    first = edited.json()
    assert (first["title"], first["task_type"], first["priority"]) == (
        "First revised",
        "work",
        4,
    )

    steered = client.post(
        f"/v1/assistant-tasks/{first['id']}/steer",
        headers=auth_headers,
        json={"expected_version": first["version"], "message": "Focus on operating cost."},
    ).json()
    assert steered["steering"] == ["Focus on operating cost."]

    paused = client.post(
        f"/v1/assistant-tasks/{steered['id']}/control",
        headers=auth_headers,
        json={"expected_version": steered["version"], "action": "pause"},
    ).json()
    assert paused["status"] == "waiting"
    resumed = client.post(
        f"/v1/assistant-tasks/{paused['id']}/control",
        headers=auth_headers,
        json={"expected_version": paused["version"], "action": "resume"},
    ).json()
    assert resumed["status"] == "queued"
    cancelled = client.post(
        f"/v1/assistant-tasks/{resumed['id']}/control",
        headers=auth_headers,
        json={"expected_version": resumed["version"], "action": "cancel"},
    ).json()
    assert cancelled["status"] == "cancelled"
    assert {task["id"] for task in client.get("/v1/assistant-tasks").json()} == {
        first["id"],
        second["id"],
    }


def test_work_overview_combines_projects_tasks_automations_and_prints(
    client, auth_headers, container
):
    project = client.post(
        "/v1/memories",
        headers=auth_headers,
        json={
            "subject": "Solar project",
            "content": "Compare rooftop proposals.",
            "scope": "personal",
            "category": "project",
            "idempotency_key": str(uuid4()),
        },
    )
    assert project.status_code == 201
    task = task_body("Compare panels") | {"project_id": project.json()["id"]}
    task_response = client.post("/v1/assistant-tasks", headers=auth_headers, json=task)
    assert task_response.status_code == 202
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )
    task_id = UUID(task_response.json()["id"])
    job = container.store.get_job(task_id)
    memory = container.store.explicit_memory(DEV_HOUSEHOLD_ID, UUID(project.json()["id"]))
    assert job and memory
    artifacts = container.tasks.save_artifacts(actor, job, memory, "# Panel result")
    artifact_response = client.get(f"/v1/assistant-tasks/{task_id}/artifacts")
    assert artifact_response.status_code == 200
    assert artifact_response.json()[0]["name"] == "result.md"
    download = client.get(
        f"/v1/assistant-tasks/artifacts/{artifacts[0].id}/download"
    )
    assert download.status_code == 200 and download.content == b"# Panel result"
    assert "attachment" in download.headers["content-disposition"]

    overview = client.get("/v1/work/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["projects"][0]["subject"] == "Solar project"
    assert body["tasks"][0]["project_name"] == "Solar project"
    assert body["project_artifacts"][0]["name"] == "result.md"
    assert set(body) == {
        "projects",
        "project_artifacts",
        "tasks",
        "workflows",
        "workflow_runs",
        "workflow_schedules",
        "workflow_health",
        "print_batches",
    }
