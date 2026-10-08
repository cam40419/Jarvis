"""HTTP separation of human sessions and scoped project worker credentials."""

from uuid import UUID, uuid4

import pytest

from simon.domain.identity import DEV_WORKSPACE_ID
from tests.api.test_native_project_api import create_project, sign_in


def role_body(*, key="research", manager=False):
    return {
        "name": "Research specialist",
        "role_key": key,
        "instructions": "Review the project evidence and prepare draft findings.",
        "success_criteria": "Every material claim cites its source.",
        "rationale": "The project needs an accountable research role.",
        "can_manage_team": manager,
        "idempotency_key": f"create-{key}",
    }


@pytest.mark.parametrize(
    "token",
    ["not-an-agent-token", f"sagent.{'-' * 36}.{'a' * 43}", f"sagent.{uuid4()}.{'a' * 43}"],
)
def test_invalid_machine_identity_returns_safe_authentication_error(client, token):
    response = client.get("/v2/projects", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert token not in response.text
    assert "Traceback" not in response.text


@pytest.fixture
def worker(client, auth_headers):
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}"
    response = client.post(path + "/agents", headers=auth_headers, json=role_body(manager=True))
    assert response.status_code == 201, response.text
    agent = response.json()
    command = {
        "expected_version": 1,
        "idempotency_key": "issue-worker-token",
        "scopes": ["board:read", "board:write", "team:manage"],
        "ttl_seconds": 900,
    }
    response = client.post(
        path + f"/agents/{agent['id']}/credentials", headers=auth_headers, json=command
    )
    assert response.status_code == 201, response.text
    issued = response.json()
    return {
        "project": project,
        "path": path,
        "agent": agent,
        "issued": issued,
        "command": command,
        "human_headers": auth_headers,
        "headers": {"Authorization": f"Bearer {issued['token']}"},
    }


def test_human_team_management_requires_csrf_and_owner_authority(client, container, auth_headers):
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}"
    assert client.post(path + "/agents", json=role_body()).status_code == 403
    assert (
        client.post(
            path + "/agents",
            headers={**auth_headers, "Origin": "https://untrusted.invalid"},
            json=role_body(),
        ).status_code
        == 403
    )
    _, member_headers = sign_in(client, container)
    assert (
        client.post(path + "/agents", headers=member_headers, json=role_body()).status_code == 404
    )
    assert client.get(path + "/team").status_code == 404


def test_machine_can_create_and_claim_work_as_real_agent_without_human_session(
    client, container, worker
):
    client.cookies.clear()
    path, headers = worker["path"], worker["headers"]
    listed = client.get("/v2/projects", headers=headers)
    assert listed.status_code == 200 and [p["id"] for p in listed.json()] == [
        worker["project"]["id"]
    ]
    response = client.post(
        path + "/tasks",
        headers=headers,
        json={"title": "Prepare evidence", "idempotency_key": "agent-created-task"},
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["created_by"] is None and task["created_by_agent_id"] == worker["agent"]["id"]
    assert container.store.memberships(UUID(worker["agent"]["id"])) == ()
    claimed = client.post(
        path + f"/tasks/{task['id']}/claim",
        headers=headers,
        json={"expected_version": 1, "idempotency_key": "agent-claims-task"},
    )
    assert claimed.status_code == 200, claimed.text
    assignment = claimed.json()["assignment"]
    assert assignment["kind"] == "agent" and assignment["agent_id"] == worker["agent"]["id"]
    assert assignment.get("actor_id") is None
    assert claimed.json()["status"] == "in_progress"
    event = next(
        e
        for e in container.store.audit_events(DEV_WORKSPACE_ID)
        if e.event_type == "native.task.claimed"
    )
    assert event.payload["actor_kind"] == "agent"
    assert event.actor_id == UUID(worker["agent"]["id"])


def test_bearer_never_falls_back_to_a_human_session(client, worker):
    for headers in [
        worker["headers"],
        {"Authorization": "Bearer invalid"},
        {"Authorization": "Basic invalid"},
    ]:
        response = client.get(worker["path"], headers=headers)
        assert response.status_code == 401, response.text
    human_only = [
        "/auth/session",
        "/auth/email",
        "/auth/password",
        "/v1/accounts",
        "/v1/threads",
        "/v1/memories",
        "/v1/connections/google",
    ]
    for path in human_only:
        assert client.get(path, headers=worker["headers"]).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    mixed_headers = {**worker["human_headers"], **worker["headers"]}
    assert client.post("/auth/logout", headers=mixed_headers).status_code == 401
    assert (
        client.post(
            "/v1/threads", headers=mixed_headers, json={"title": "Confused authority"}
        ).status_code
        == 401
    )
    assert client.get("/auth/session").status_code == 200
    client.cookies.clear()
    assert (
        client.get(worker["path"], headers={"Authorization": "Bearer invalid"}).status_code == 401
    )
    for path in human_only:
        assert client.get(path, headers=worker["headers"]).status_code == 401


def test_worker_cannot_leak_other_projects_or_tenants_or_switch_workspace(
    client, container, worker
):
    other = create_project(client, worker["human_headers"], key="other-native-project")
    _, foreign_headers = sign_in(client, container, workspace_id=uuid4(), role="owner")
    foreign = create_project(client, foreign_headers, key="foreign-native-project")
    client.cookies.clear()
    for project in [other, foreign]:
        for suffix in ["", "/team", "/tasks", "/members", "/access"]:
            response = client.get(
                f"/v2/projects/{project['id']}" + suffix, headers=worker["headers"]
            )
            assert response.status_code == 404, response.text
    assert (
        client.get(
            worker["path"], headers={**worker["headers"], "X-Workspace-ID": str(uuid4())}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v2/projects",
            headers=worker["headers"],
            json={
                "name": "Unauthorized",
                "objective": "Expand authority",
                "idempotency_key": "worker-new-project",
            },
        ).status_code
        == 403
    )


def test_scoped_manager_creates_subordinates_but_cannot_grant_authority(client, worker):
    client.cookies.clear()
    path, headers = worker["path"], worker["headers"]
    response = client.post(path + "/agents", headers=headers, json=role_body(key="designer"))
    assert response.status_code == 201, response.text
    child = response.json()
    assert child["created_by"] is None and child["created_by_agent_id"] == worker["agent"]["id"]
    assert (
        client.post(
            path + "/agents", headers=headers, json=role_body(key="coordinator", manager=True)
        ).status_code
        == 403
    )
    assert (
        client.put(
            path + "/team/policy",
            headers=headers,
            json={
                "max_active_agents": 100,
                "agents_can_manage_team": True,
                "expected_version": 0,
                "idempotency_key": "worker-raise-budget",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            path + f"/agents/{child['id']}/credentials",
            headers=headers,
            json={"expected_version": 1, "idempotency_key": "worker-mints-access"},
        ).status_code
        == 403
    )
    assert (
        client.get(path + f"/agents/{child['id']}/credentials", headers=headers).status_code == 403
    )
    assert (
        client.put(
            path + "/members",
            headers=headers,
            json={
                "actor_id": str(uuid4()),
                "role": "owner",
                "expected_version": 1,
                "idempotency_key": "worker-changes-human",
            },
        ).status_code
        == 403
    )
    view = client.get(path + "/team", headers=headers).json()
    assert view["can_manage"] and not view["can_set_policy"] and not view["can_issue_credentials"]


def test_credential_secret_is_returned_once_and_never_on_list_or_retry(client, worker):
    path = worker["path"] + f"/agents/{worker['agent']['id']}/credentials"
    response = client.post(path, headers=worker["human_headers"], json=worker["command"])
    assert response.status_code == 201, response.text
    replay = response.json()
    assert replay["token"] is None and replay["replayed"]
    assert replay["credential"] == worker["issued"]["credential"]
    assert response.headers["cache-control"] == "no-store"
    listing = client.get(path)
    assert listing.status_code == 200
    assert listing.json() == [worker["issued"]["credential"]]
    assert worker["issued"]["token"] not in listing.text
    assert "token_hash" not in listing.text
    assert "token_hash" not in response.text


def test_read_only_token_can_inspect_board_but_cannot_write(client, worker):
    path = worker["path"]
    issued = client.post(
        path + f"/agents/{worker['agent']['id']}/credentials",
        headers=worker["human_headers"],
        json={
            "scopes": ["board:read"],
            "expected_version": 1,
            "idempotency_key": "issue-read-only",
        },
    )
    assert issued.status_code == 201, issued.text
    headers = {"Authorization": "Bearer " + issued.json()["token"]}
    client.cookies.clear()
    assert client.get(path + "/tasks", headers=headers).status_code == 200
    assert client.get(path + "/team", headers=headers).status_code == 200
    assert (
        client.post(
            path + "/tasks",
            headers=headers,
            json={"title": "Denied", "idempotency_key": "readonly-task"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            path + "/agents", headers=headers, json=role_body(key="denied-role")
        ).status_code
        == 403
    )
    assert not client.get(path + "/access", headers=headers).json()["permissions"]["can_edit"]


def test_revoked_machine_cannot_replay_successful_http_mutation(client, worker):
    path, headers = worker["path"], worker["headers"]
    human_cookies = dict(client.cookies)
    client.cookies.clear()
    command = {"title": "Reviewable draft", "idempotency_key": "replay-before-revoke"}
    created = client.post(path + "/tasks", headers=headers, json=command)
    assert created.status_code == 201
    client.cookies.update(human_cookies)
    credential = worker["issued"]["credential"]
    revoked = client.post(
        path + f"/agents/{worker['agent']['id']}/credentials/{credential['id']}/revoke",
        headers=worker["human_headers"],
        json={"expected_version": 1, "idempotency_key": "owner-revokes-worker"},
    )
    assert revoked.status_code == 200 and not revoked.json()["valid"]
    client.cookies.clear()
    assert client.post(path + "/tasks", headers=headers, json=command).status_code == 401
    assert client.get(path + "/tasks", headers=headers).status_code == 401
