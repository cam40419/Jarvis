"""Real session, tenant and shared-board boundaries for the native project API."""

import json
from uuid import UUID, uuid4

import pytest

from simon.api.auth import session_cookie
from simon.domain.accounts import ManagedAccount
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID, Membership
from simon.domain.native_projects import NativeProjectMember
from simon.services.identity import csrf_token


@pytest.mark.parametrize("field", ["name", "objective", "idempotency_key"])
@pytest.mark.parametrize("invalid", ["bad\x00text", "bad\ud800text"])
def test_invalid_database_text_is_a_safe_validation_error(client, auth_headers, field, invalid):
    body = {"name": "Project", "objective": "A goal", "idempotency_key": "valid-command"}
    body[field] = invalid
    response = client.post(
        "/v2/projects",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=json.dumps(body).encode("ascii"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "request fields are invalid"
    assert client.get("/v2/projects").json() == []


def sign_in(client, container, *, actor_id=None, workspace_id=DEV_WORKSPACE_ID, role="member"):
    actor_id = actor_id or uuid4()
    container.store.put_membership(
        Membership(actor_id=actor_id, workspace_id=workspace_id, role=role)
    )
    token, _ = container.identity._issue(actor_id, workspace_id, "development")
    client.cookies.clear()
    client.cookies.set(session_cookie(container.identity), token)
    return actor_id, {"Origin": "http://localhost:8000", "X-CSRF-Token": csrf_token(token)}


def create_project(client, headers, *, key="native-project-create", name="Clothing pilot"):
    response = client.post(
        "/v2/projects",
        headers=headers,
        json={
            "name": name,
            "objective": "Review source material and coordinate an approved first collection.",
            "idempotency_key": key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_task(client, headers, project_id, *, key="native-task-create"):
    response = client.post(
        f"/v2/projects/{project_id}/tasks",
        headers=headers,
        json={"title": "Review brand direction", "idempotency_key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_native_projects_require_sessions_and_csrf(client, auth_headers):
    body = {"name": "Project", "objective": "A useful objective", "idempotency_key": "auth-create"}
    assert client.post("/v2/projects", json=body).status_code == 403
    assert (
        client.post(
            "/v2/projects",
            headers={**auth_headers, "Origin": "https://untrusted.example"},
            json=body,
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/v2/projects").status_code == 401
    assert client.post("/v2/projects", headers=auth_headers, json=body).status_code == 401


def test_native_creation_uses_only_native_authority(client, container, auth_headers):
    project = create_project(client, auth_headers)
    assert project["board_authority"] == "native"
    assert project["workspace_id"] == str(DEV_WORKSPACE_ID)
    assert project["created_by"] == str(DEV_ACTOR_ID)
    assert project["version"] == 1
    assert "drive" not in project
    assert client.get("/v2/projects").json() == [project]
    assert client.get(f"/v2/projects/{project['id']}").json() == project
    assert client.get(f"/v1/projects/{project['id']}").status_code == 404
    assert client.get(f"/v1/projects/{project['id']}/command").status_code == 404
    assert container.store.explicit_memory(DEV_WORKSPACE_ID, UUID(project["id"])) is None
    members = client.get(f"/v2/projects/{project['id']}/members").json()
    assert [(member["actor_id"], member["role"]) for member in members] == [
        (str(DEV_ACTOR_ID), "owner")
    ]


def test_project_commands_replay_and_reject_stale_or_conflicting_writes(client, auth_headers):
    project = create_project(client, auth_headers)
    assert create_project(client, auth_headers) == project
    conflicting = client.post(
        "/v2/projects",
        headers=auth_headers,
        json={"name": "Other", "objective": "Changed", "idempotency_key": "native-project-create"},
    )
    assert conflicting.status_code == 409
    path = f"/v2/projects/{project['id']}"
    body = {
        "name": "Reviewed project",
        "objective": project["objective"],
        "status": "active",
        "expected_version": project["version"],
        "idempotency_key": "native-project-update",
    }
    saved = client.put(path, headers=auth_headers, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert client.put(path, headers=auth_headers, json=body).json() == saved.json()
    stale = client.put(
        path, headers=auth_headers, json={**body, "idempotency_key": "native-project-stale"}
    )
    assert stale.status_code == 409
    assert client.get(path).json() == saved.json()


def test_native_project_members_share_and_claim_the_same_tasks(client, container, auth_headers):
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}"
    member_id = uuid4()
    container.store.put_membership(
        Membership(actor_id=member_id, workspace_id=DEV_WORKSPACE_ID, role="member")
    )
    added = client.put(
        path + "/members",
        headers=auth_headers,
        json={
            "actor_id": str(member_id),
            "role": "member",
            "expected_version": project["version"],
            "idempotency_key": "native-member-add",
        },
    )
    assert added.status_code == 200, added.text
    task = create_task(client, auth_headers, project["id"])
    _, member_headers = sign_in(client, container, actor_id=member_id)
    assert [item["id"] for item in client.get("/v2/projects").json()] == [project["id"]]
    assert client.get(path + "/tasks").json() == [task]
    task_path = path + f"/tasks/{task['id']}"
    claim = {"expected_version": task["version"], "idempotency_key": "member-claim-task"}
    claimed = client.post(task_path + "/claim", headers=member_headers, json=claim)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["assignment"] == {
        "kind": "human",
        "actor_id": str(member_id),
        "agent_id": None,
    }
    assert (
        client.post(task_path + "/claim", headers=member_headers, json=claim).json()
        == claimed.json()
    )
    saved = client.put(
        task_path,
        headers=member_headers,
        json={
            "title": task["title"],
            "description": "Prepared direction alternatives for human review.",
            "status": "in_review",
            "assignment": claimed.json()["assignment"],
            "expected_version": claimed.json()["version"],
            "idempotency_key": "member-task-review",
        },
    )
    assert saved.status_code == 200, saved.text
    _, owner_headers = sign_in(client, container, actor_id=DEV_ACTOR_ID, role="owner")
    assert client.get(task_path).json() == saved.json()
    stale = client.post(
        task_path + "/claim",
        headers=owner_headers,
        json={**claim, "idempotency_key": "competing-owner-claim"},
    )
    assert stale.status_code == 409


@pytest.mark.parametrize("different_workspace", [False, True])
def test_native_projects_and_tasks_do_not_leak_to_outsiders(
    client, container, auth_headers, different_workspace
):
    project = create_project(client, auth_headers)
    task = create_task(client, auth_headers, project["id"])
    _, outsider_headers = sign_in(
        client,
        container,
        workspace_id=uuid4() if different_workspace else DEV_WORKSPACE_ID,
        role="owner" if different_workspace else "member",
    )
    path = f"/v2/projects/{project['id']}"
    assert client.get("/v2/projects").json() == []
    for suffix in ("", "/members", "/tasks", f"/tasks/{task['id']}"):
        response = client.get(path + suffix)
        assert response.status_code == 404, response.text
    create = client.post(
        path + "/tasks",
        headers=outsider_headers,
        json={"title": "Unauthorized task", "idempotency_key": "outsider-task-create"},
    )
    assert create.status_code == 404


def test_project_membership_removal_immediately_revokes_access(client, container, auth_headers):
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}"
    member_id = uuid4()
    container.store.put_membership(
        Membership(actor_id=member_id, workspace_id=DEV_WORKSPACE_ID, role="member")
    )
    added = client.put(
        path + "/members",
        headers=auth_headers,
        json={
            "actor_id": str(member_id),
            "expected_version": project["version"],
            "idempotency_key": "temporary-member-add",
        },
    )
    assert added.status_code == 200, added.text
    sign_in(client, container, actor_id=member_id)
    assert client.get(path).status_code == 200
    _, owner_headers = sign_in(client, container, actor_id=DEV_ACTOR_ID, role="owner")
    removed = client.post(
        path + f"/members/{member_id}/remove",
        headers=owner_headers,
        json={
            "expected_version": client.get(path).json()["version"],
            "idempotency_key": "temporary-member-remove",
        },
    )
    assert removed.status_code == 200, removed.text
    _, revoked_headers = sign_in(client, container, actor_id=member_id)
    assert client.get(path).status_code == 404
    assert client.get("/v2/projects").json() == []
    assert (
        client.post(
            path + "/tasks",
            headers=revoked_headers,
            json={"title": "No longer allowed", "idempotency_key": "revoked-task-create"},
        ).status_code
        == 404
    )


def test_native_api_rejects_forged_scope_and_malformed_agent_assignments(client, auth_headers):
    project_body = {
        "name": "Project",
        "objective": "Review inputs",
        "idempotency_key": "scope-test",
    }
    for forged in ({"workspace_id": str(uuid4())}, {"board_authority": "clickup"}):
        response = client.post("/v2/projects", headers=auth_headers, json=project_body | forged)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    project = create_project(client, auth_headers)
    task = client.post(
        f"/v2/projects/{project['id']}/tasks",
        headers=auth_headers,
        json={
            "title": "Invalid agent identifier",
            "assignment": {"kind": "agent", "agent_id": "designer"},
            "idempotency_key": "malformed-agent",
        },
    )
    assert task.status_code == 422
    assert client.get("/v2/projects?limit=101").status_code == 422
    assert client.get(f"/v2/projects/{project['id']}/tasks?offset=-1").status_code == 422


def test_task_identifiers_are_scoped_to_their_project(client, auth_headers):
    first = create_project(client, auth_headers)
    second = create_project(client, auth_headers, key="second-native-project")
    task = create_task(client, auth_headers, first["id"])
    wrong_path = f"/v2/projects/{second['id']}/tasks/{task['id']}"
    assert client.get(wrong_path).status_code == 404
    assert client.get(f"/v2/projects/{second['id']}/tasks").json() == []


def test_guest_members_can_read_native_boards_without_write_access(client, container, auth_headers):
    project = create_project(client, auth_headers)
    task = create_task(client, auth_headers, project["id"])
    path = f"/v2/projects/{project['id']}"
    guest_id = uuid4()
    container.store.put_membership(
        Membership(actor_id=guest_id, workspace_id=DEV_WORKSPACE_ID, role="guest")
    )
    added = client.put(
        path + "/members",
        headers=auth_headers,
        json={
            "actor_id": str(guest_id),
            "expected_version": project["version"],
            "idempotency_key": "guest-project-access",
        },
    )
    assert added.status_code == 200, added.text
    _, guest_headers = sign_in(client, container, actor_id=guest_id, role="guest")
    assert client.get(path).status_code == 200
    assert [item["id"] for item in client.get("/v2/projects").json()] == [project["id"]]
    assert client.get(path + "/tasks").json() == [task]
    assert client.get(path + f"/tasks/{task['id']}").json() == task
    assert client.get(path + "/members").status_code == 200
    assert client.get("/v1/projects").status_code == 404
    assert client.get("/auth/session").json()["scopes"] == ["system:read"]
    created = client.post(
        path + "/tasks",
        headers=guest_headers,
        json={"title": "Guest cannot create", "idempotency_key": "guest-task-create"},
    )
    assert created.status_code == 403
    claimed = client.post(
        path + f"/tasks/{task['id']}/claim",
        headers=guest_headers,
        json={"expected_version": task["version"], "idempotency_key": "guest-task-claim"},
    )
    assert claimed.status_code == 403
    assert client.get(path + f"/tasks/{task['id']}").json() == task


def test_current_role_downgrade_blocks_writes_and_replays_with_cached_owner_scopes(
    client, container, auth_headers, monkeypatch
):
    project = create_project(client, auth_headers)
    task = create_task(client, auth_headers, project["id"])
    token = client.cookies.get(session_cookie(container.identity))
    cached_authentication = container.identity.resolve(token)
    assert "jobs:write" in cached_authentication[1].scopes
    container.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, workspace_id=DEV_WORKSPACE_ID, role="guest")
    )
    # Model a membership change after HTTP authentication but before mutation dispatch.
    monkeypatch.setattr(container.identity, "resolve", lambda _token: cached_authentication)
    path = f"/v2/projects/{project['id']}"
    for key in ("native-task-create", "downgraded-owner-new-task"):
        response = client.post(
            path + "/tasks",
            headers=auth_headers,
            json={"title": task["title"], "idempotency_key": key},
        )
        assert response.status_code == 403, response.text
    assert client.get(path + "/tasks").json() == [task]


@pytest.mark.parametrize("role", ["owner", "member", "guest"])
def test_project_access_reports_current_permissions_and_minimal_shared_people(
    client, container, auth_headers, role
):
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}"
    actor_id = DEV_ACTOR_ID
    if role != "owner":
        actor_id, _ = sign_in(client, container, role=role)
        container.store.put_native_project_member(
            NativeProjectMember(
                workspace_id=DEV_WORKSPACE_ID, project_id=UUID(project["id"]), actor_id=actor_id
            )
        )
    candidate_id, foreign_id, disabled_id = uuid4(), uuid4(), uuid4()
    for identifier, workspace_id, name in (
        (candidate_id, DEV_WORKSPACE_ID, "Potential collaborator"),
        (foreign_id, uuid4(), "Other tenant private name"),
        (disabled_id, DEV_WORKSPACE_ID, "Disabled account"),
    ):
        container.store.put_membership(
            Membership(
                actor_id=identifier, workspace_id=workspace_id, role="member", display_name=name
            )
        )
    container.store.save_managed_account(
        ManagedAccount(
            actor_id=disabled_id,
            workspace_id=DEV_WORKSPACE_ID,
            invited_by=DEV_ACTOR_ID,
            display_name="Disabled account",
            disabled=True,
        )
    )
    response = client.get(path + "/access")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["actor_id"] == str(actor_id)
    assert value["project_version"] == project["version"]
    assert value["permissions"] == {
        "can_edit": role != "guest",
        "can_manage_members": role == "owner",
        "can_archive": role == "owner",
        "can_claim": role != "guest",
    }
    people = {person["actor_id"]: person for person in value["members"]}
    assert people[str(actor_id)]["can_assign"] == (role != "guest")
    assert people[str(actor_id)]["workspace_role"] == role
    assert people[str(actor_id)]["active"] is True
    assert all(person["display_name"] for person in people.values())
    assert value["member_candidates"] == (
        [
            {
                "actor_id": str(candidate_id),
                "display_name": "Potential collaborator",
                "workspace_role": "member",
            }
        ]
        if role == "owner"
        else []
    )
    assert str(foreign_id) not in response.text
    assert "Other tenant private name" not in response.text
    assert str(disabled_id) not in response.text
    assert "email" not in response.text and "password" not in response.text


def test_access_directory_paginates_filtered_rows_and_archive_disables_board_actions(
    client, container, auth_headers
):
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}"
    ids = [UUID(int=value) for value in (1, 2, 3)]
    for identifier in ids:
        container.store.put_membership(
            Membership(actor_id=identifier, workspace_id=DEV_WORKSPACE_ID, role="guest")
        )
    container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID, project_id=UUID(project["id"]), actor_id=ids[0]
        )
    )
    first = client.get(path + "/access?candidates_limit=1").json()
    assert first["member_candidates"] == []
    assert first["candidates_next_offset"] == 1
    second = client.get(path + "/access?candidates_limit=1&candidates_offset=1").json()
    assert [candidate["actor_id"] for candidate in second["member_candidates"]] == [str(ids[1])]
    assert second["member_candidates"][0]["workspace_role"] == "guest"
    assert second["candidates_next_offset"] == 2
    final = client.get(path + "/access?candidates_limit=1&candidates_offset=4").json()
    assert final["member_candidates"] == [] and final["candidates_next_offset"] is None
    archived = client.put(
        path,
        headers=auth_headers,
        json={
            "name": project["name"],
            "objective": project["objective"],
            "status": "archived",
            "expected_version": project["version"],
            "idempotency_key": "archive-access-view",
        },
    )
    assert archived.status_code == 200, archived.text
    access = client.get(path + "/access").json()
    assert access["project_version"] == archived.json()["version"]
    assert access["permissions"] == {
        "can_edit": True,
        "can_manage_members": False,
        "can_archive": True,
        "can_claim": False,
    }
    assert access["member_candidates"] == []


def test_access_view_rechecks_revocation_and_does_not_leak_to_other_tenants(
    client, container, auth_headers
):
    project = create_project(client, auth_headers)
    project_id = UUID(project["id"])
    path = f"/v2/projects/{project_id}/access"
    member_id, _ = sign_in(client, container)
    container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID, project_id=project_id, actor_id=member_id
        )
    )
    assert client.get(path).json()["permissions"]["can_claim"] is True
    container.store.delete_native_project_member(DEV_WORKSPACE_ID, project_id, member_id)
    assert client.get(path).status_code == 404
    sign_in(client, container, workspace_id=uuid4(), role="owner")
    assert client.get(path).status_code == 404


def test_native_workspace_header_rejects_requests_after_another_tab_switches_workspace(
    client, container, auth_headers
):
    original = create_project(client, auth_headers)
    other_workspace = uuid4()
    container.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, workspace_id=other_workspace, role="owner")
    )
    switched = client.post(
        "/auth/workspace", headers=auth_headers, json={"workspace_id": str(other_workspace)}
    )
    assert switched.status_code == 200, switched.text
    headers = {
        "Origin": "http://localhost:8000",
        "X-CSRF-Token": switched.json()["csrf_token"],
        "X-Workspace-ID": str(DEV_WORKSPACE_ID),
    }
    body = {
        "name": "Wrong workspace",
        "objective": "Must not create",
        "idempotency_key": "tab-switch-create",
    }
    for response in (
        client.get("/v2/projects", headers=headers),
        client.get(f"/v2/projects/{original['id']}/access", headers=headers),
        client.post("/v2/projects", headers=headers, json=body),
    ):
        assert response.status_code == 403
        assert response.json()["error"]["message"] == "Workspace changed. Reload before continuing."
    assert container.store.native_projects(other_workspace, DEV_ACTOR_ID, 0, 100) == ()
    headers["X-Workspace-ID"] = str(other_workspace)
    assert client.get("/v2/projects", headers=headers).json() == []
    assert client.post("/v2/projects", headers=headers, json=body).status_code == 201
