"""Browser-visible output links retain authentication and revision-safe copy semantics."""

from uuid import UUID, uuid4

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.services.agent_platform import AgentPlatformService
from tests.contract.test_project_history import saved_run


def test_output_routes_require_login_and_project_visibility(client, auth_headers):
    anonymous = client.__class__(client.app, base_url="http://localhost:8000")
    with anonymous:
        assert anonymous.get(f"/v1/projects/{uuid4()}/outputs").status_code == 401
    project = client.post(
        "/v1/projects",
        headers=auth_headers,
        json={
            "name": "Generated outputs",
            "description": "Reuse earlier files",
            "idempotency_key": "outputs-api-project",
        },
    ).json()["id"]
    response = client.get(f"/v1/projects/{project}/outputs")
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["next_cursor"] is None
    assert not response.json()["can_promote"]
    assert client.get(f"/v1/projects/{uuid4()}/outputs").status_code == 404
    assert client.get(f"/v1/projects/{project}/outputs?limit=51").status_code == 422
    assert client.get(f"/v1/projects/{project}/outputs?kind=deliverable").status_code == 200
    assert client.get(f"/v1/projects/{project}/outputs?kind=response").status_code == 200
    assert client.get(f"/v1/projects/{project}/outputs?kind=unknown").status_code == 422


def test_output_copy_download_csrf_original_and_conflicts(
    client,
    container,
    auth_headers,
    tmp_path,
):
    team = TeamTemplate(id="studio", name="Studio", agent_ids=("writer",))
    configured = AgentPlatformService(
        container.store,
        PlatformManifest(
            agents=(AgentProfile(id="writer", instructions="Write."),),
            teams=(team,),
        ),
        state_dir=tmp_path / "agents",
        environ={},
    )
    container.agent_platform.__dict__.update(configured.__dict__)
    container.agent_platform.project_visibility_resolver = container.connected.projects.project
    container.agent_platform.project_team_resolver = lambda *_: team
    container.connected.settings = container.connected.settings.model_copy(
        update={
            "local_files_enabled": True,
            "local_files_dir": tmp_path / "local",
        }
    )
    project = UUID(
        client.post(
            "/v1/projects",
            headers=auth_headers,
            json={
                "name": "Reusable outputs",
                "description": "Retain copies",
                "idempotency_key": "outputs-copy-api",
            },
        ).json()["id"]
    )
    run = saved_run(container.store, project, state_dir=container.agent_platform.state_dir)
    artifact = run.tasks[0].artifacts[0]
    url = f"/v1/projects/{project}/outputs"
    listing = client.get(url).json()
    assert listing["can_promote"] and listing["items"][0]["project_copy"] is None
    endpoint = url + f"/{run.id}/{artifact.id}/promote"
    request = {"idempotency_key": "outputs-browser-save"}
    assert client.post(endpoint, json=request).status_code == 403
    copied = client.post(endpoint, headers=auth_headers, json=request)
    assert copied.status_code == 200, copied.text
    item = copied.json()
    copy = item["project_copy"]
    assert client.get(item["download_url"]).content == b"Result"
    assert client.get(copy["download_url"]).content == b"Result"
    assert client.get(url).json()["items"][0]["project_copy"] == copy
    assert client.post(endpoint, headers=auth_headers, json=request).json() == item
    edited = client.post(
        "/v1/local-files/action",
        headers=auth_headers,
        json={
            "name": "local_file_edit",
            "idempotency_key": "edit-output-copy",
            "arguments": {
                "root": copy["root"],
                "path": copy["path"],
                "revision": copy["revision"],
                "old_text": "Result",
                "new_text": "Edited",
            },
        },
    )
    assert edited.status_code == 200, edited.text
    assert client.get(copy["download_url"]).content == b"Edited"
    assert client.get(item["download_url"]).content == b"Result"
    assert (
        client.post(
            endpoint,
            headers=auth_headers,
            json={
                "idempotency_key": "save-after-user-edit",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/projects/{uuid4()}/outputs/{run.id}/{artifact.id}/promote",
            headers=auth_headers,
            json=request,
        ).status_code
        == 404
    )
