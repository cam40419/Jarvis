"""Automatically persisted incomplete work is readable through scoped project APIs."""

from uuid import UUID, uuid4

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.models import JobStatus
from simon.services.agent_platform import AgentPlatformService
from tests.contract.test_project_history import saved_run


def test_saved_draft_preview_download_and_journal_pages_remain_authenticated(
    client, container, auth_headers, tmp_path
):
    # Use the same authenticated synthetic owner that creates the project.
    project = UUID(
        client.post(
            "/v1/projects",
            headers=auth_headers,
            json={
                "name": "Persistent research",
                "description": "Drafts stay saved",
                "idempotency_key": "persistent-research-project",
            },
        ).json()["id"]
    )
    team = TeamTemplate(id="studio", name="Studio", agent_ids=("writer",))
    configured = AgentPlatformService(
        container.store,
        PlatformManifest(agents=(AgentProfile(id="writer", instructions="Write."),), teams=(team,)),
        state_dir=tmp_path / "agents",
        environ={},
    )
    container.agent_platform.__dict__.update(configured.__dict__)
    container.agent_platform.project_visibility_resolver = container.connected.projects.project
    container.agent_platform.project_team_resolver = lambda *_: team
    run = saved_run(container.store, project, state_dir=container.agent_platform.state_dir)
    actor = container.agent_runs._resolve_actor(run.actor_id, run.workspace_id)
    executor = uuid4()
    run = container.agent_runs.update(
        run.id,
        lambda value: value.model_copy(
            update={
                "status": JobStatus.RUNNING,
                "executor_id": executor,
                "tasks": (
                    value.tasks[0].model_copy(update={"status": "running", "artifacts": ()}),
                ),
            }
        ),
    )
    task = container.agent_platform.get(actor, run.plan_id).tasks[0]
    entry = container.project_outputs.journal.append(
        actor,
        run.id,
        task,
        executor_id=executor,
        project_id=project,
        kind="candidate",
        step=1,
        payload={"text": "# Saved draft\nEvidence pending."},
    )
    listing = client.get(f"/v1/projects/{project}/outputs").json()
    item = listing["items"][0]
    assert item["id"] == str(entry.artifact.id) and item["status"] == "draft"
    assert item["kind"] == "response"
    assert client.get(f"/v1/projects/{project}/outputs?kind=deliverable").json()["items"] == []
    assert client.get(f"/v1/projects/{project}/outputs?kind=response").json()["items"] == [item]
    assert client.get(item["download_url"]).text == "# Saved draft\nEvidence pending."
    assert client.get(item["preview_url"]).json()["text"] == "# Saved draft\nEvidence pending."
    base = f"/v1/projects/{project}/outputs/journal/{run.id}"
    assert client.get(base).json()["items"][0]["id"] == str(entry.id)
    page = client.get(base + f"/{entry.id}?offset=2&limit=10").json()
    assert page["text"] == "Saved draf" and page["next_offset"] == 12
    assert client.get(base + f"/{entry.id}?limit=8001").status_code == 422
    assert client.get(base.replace(str(project), str(uuid4()))).status_code == 404
    with client.__class__(client.app, base_url="http://localhost:8000") as anonymous:
        for url in (base, base + f"/{entry.id}", item["download_url"], item["preview_url"]):
            assert anonymous.get(url).status_code == 401
