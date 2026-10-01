"""Board endpoints enforce the browser session and expose safe local snapshots."""

from uuid import uuid4

from tests.unit.test_clickup import TOKEN, adapter, connection


def project(client, headers):
    response = client.post(
        "/v1/projects",
        headers=headers,
        json={
            "name": "Clothing launch",
            "description": "Build a small collection",
            "idempotency_key": "company-board-project",
        },
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()["id"]


def test_board_authentication_and_unconfigured_snapshot(client, auth_headers):
    anonymous = client.__class__(client.app, base_url="http://localhost:8000")
    with anonymous:
        assert anonymous.get("/v1/project-boards/connections").status_code == 401
    identifier = project(client, auth_headers)
    assert client.get("/v1/project-boards/connections").json() == []
    state = client.get(f"/v1/projects/{identifier}/board").json()
    assert state["state"]["binding"] is None and state["operations"] == []
    assert client.get(f"/v1/projects/{uuid4()}/board").status_code == 404


def test_board_connect_preview_import_and_optimistic_binding(client, container, auth_headers):
    provider, requests = adapter()
    service = container.project_boards
    service.adapter = provider
    service.configured_connections = (connection(),)
    identifier = project(client, auth_headers)
    url = f"/v1/projects/{identifier}/board"
    choices = client.get("/v1/project-boards/connections")
    assert choices.json()[0]["available"]
    assert TOKEN not in choices.text and "credential_env" not in choices.text
    assert not requests
    body = {
        "expected_version": 0,
        "binding": {
            "connection_id": "company",
            "list_id": "456",
            "sync_progress": False,
        },
    }
    assert client.patch(url, json=body).status_code == 403
    assert not requests
    bound = client.patch(url, json=body, headers=auth_headers)
    assert bound.status_code == 200, bound.text
    assert bound.json()["state"]["board"]["name"] == "Company project"
    assert client.patch(url, json=body, headers=auth_headers).status_code == 409
    preview = client.get(url + "/preview").json()
    assert preview["tasks"][0]["name"] == "Human task name"
    imported = client.post(
        url + "/import",
        headers=auth_headers,
        json={
            "task_ids": ["task1"],
            "expected_version": bound.json()["state"]["version"],
            "idempotency_key": "company-selected-import",
        },
    )
    assert imported.status_code == 200, imported.text
    mapping = imported.json()["state"]["mappings"][0]
    assert mapping["remote_id"] == "task1"
    tasks = client.get(f"/v1/projects/{identifier}/command").json()["state"]
    todo = tasks["todos"][0]
    assert todo["title"] == "Human task name"
    rejected = client.patch(
        f"/v1/projects/{identifier}/todos/{todo['id']}",
        headers=auth_headers,
        json={"expected_version": tasks["version"], "todo": {**todo, "title": "Local overwrite"}},
    )
    assert rejected.status_code in {400, 422}, rejected.text
    assert "ClickUp" in rejected.text
    assert all(request.method == "GET" for request in requests)
    before = len(requests)
    service.configured_connections = ()
    revoked = client.get(url).json()
    assert any("no longer available" in reason for reason in revoked["blocked_reasons"])
    command = client.get(f"/v1/projects/{identifier}/command").json()
    assert any("no longer available" in reason for reason in command["blocked_reasons"])
    assert len(requests) == before
