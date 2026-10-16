"""Resource authority, safe downgrades and live tenant boundaries through real sessions."""

import base64
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID, Membership
from simon.domain.models import utc_now
from simon.domain.native_projects import NativeProjectMember
from simon.services.intake_sources import IntakeSourceBytes
from tests.api.test_native_project_api import create_project, sign_in


def entitlement(version=0, limit=1000, **changes):
    return {
        "expected_version": version,
        "idempotency_key": str(uuid4()),
        "plan_key": "synthetic",
        "limit_bytes": limit,
        "reason": "Controlled storage acceptance fixture",
        **changes,
    }


def upload_body(*, content=b"abc", version=1, **changes):
    return {
        "expected_version": version,
        "idempotency_key": str(uuid4()),
        "source_key": "brief.txt",
        "filename": "brief.txt",
        "media_type": "text/plain",
        "content_base64": base64.b64encode(content).decode(),
        **changes,
    }


@pytest.fixture
def storage_api(client, container, auth_headers, tmp_path):
    container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "sources")
    project = create_project(client, auth_headers)
    saved = client.put(
        f"/v2/projects/{project['id']}/intake",
        headers=auth_headers,
        json={"expected_version": 0, "idempotency_key": str(uuid4())},
    )
    assert saved.status_code == 200, saved.text
    return f"/v2/projects/{project['id']}", UUID(project["id"])


def test_storage_default_exact_accounting_downgrade_and_successful_replay(
    client, container, auth_headers, storage_api
):
    path, _ = storage_api
    initial = client.get(path + "/storage").json()
    assert initial["effective_limit_bytes"] == 256 * 1024 * 1024
    assert initial["used_bytes"] == 0 and initial["can_manage"]
    command = entitlement(limit=6)
    applied = client.post(path + "/storage/entitlement", headers=auth_headers, json=command)
    assert applied.status_code == 200, applied.text
    body = upload_body()
    source = client.post(path + "/intake/sources", headers=auth_headers, json=body)
    assert source.status_code == 201, source.text
    used = client.get(path + "/storage").json()
    assert used["retained_bytes"] == used["used_bytes"] == used["project_used_bytes"] == 6
    assert used["reserved_bytes"] == used["available_bytes"] == 0
    result = client.post(
        path + "/storage/entitlement", headers=auth_headers, json=entitlement(1, 0)
    )
    assert result.status_code == 200 and result.json()["overage_bytes"] == 6
    assert client.get(path + f"/intake/sources/{source.json()['id']}/content").content == b"abc"
    replay = client.post(path + "/intake/sources", headers=auth_headers, json=body)
    assert replay.status_code == 201 and replay.json() == source.json()
    denied = client.post(
        path + "/intake/sources", headers=auth_headers, json=upload_body(version=2)
    )
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "storage_quota_exceeded"
    assert client.get(path + "/storage").json()["used_bytes"] == 6
    revoked = client.post(
        path + f"/intake/sources/{source.json()['id']}/revoke",
        headers=auth_headers,
        json={"expected_version": 2, "idempotency_key": str(uuid4())},
    )
    assert revoked.status_code == 200, revoked.text
    assert client.get(path + "/storage").json()["used_bytes"] == 6
    receipt = client.get(path + f"/storage/operations/{command['idempotency_key']}")
    assert receipt.status_code == 200
    assert receipt.json()["applied_entitlement"]["version"] == 1
    assert receipt.json()["entitlement"]["version"] == 2
    assert len(container.store.storage_entitlement_history(DEV_WORKSPACE_ID)) == 2


def test_storage_authority_is_above_workspace_owner_and_fenced_before_replay(
    client, container, auth_headers, storage_api
):
    path, _ = storage_api
    command = entitlement()
    assert (
        client.post(path + "/storage/entitlement", headers=auth_headers, json=command).status_code
        == 200
    )
    owner, headers = sign_in(client, container, role="owner")
    assert not client.get(path + "/storage").json()["can_manage"]
    assert (
        client.post(path + "/storage/entitlement", headers=headers, json=entitlement(1)).status_code
        == 403
    )
    assert client.get(path + f"/storage/operations/{command['idempotency_key']}").status_code == 403
    # A formerly privileged administrator cannot reuse an old receipt after demotion.
    _, headers = sign_in(client, container, actor_id=DEV_ACTOR_ID, role="member")
    container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=storage_api[1],
            actor_id=DEV_ACTOR_ID,
            role="owner",
        )
    )
    assert (
        client.post(path + "/storage/entitlement", headers=headers, json=command).status_code == 403
    )
    assert client.get(path + f"/storage/operations/{command['idempotency_key']}").status_code == 403
    assert owner != DEV_ACTOR_ID


def test_storage_csrf_origin_workspace_and_input_validation(client, auth_headers, storage_api):
    path, _ = storage_api
    body = entitlement()
    assert client.post(path + "/storage/entitlement", json=body).status_code == 403
    for headers in (
        {**auth_headers, "Origin": "https://elsewhere.example"},
        {**auth_headers, "X-Workspace-ID": str(uuid4())},
    ):
        assert (
            client.post(path + "/storage/entitlement", headers=headers, json=body).status_code
            == 403
        )
    for value in (-1, True, 1.5, "1000", 1024**4 + 1):
        assert (
            client.post(
                path + "/storage/entitlement",
                headers=auth_headers,
                json={**body, "limit_bytes": value},
            ).status_code
            == 422
        )
    for change in (
        {"override_limit_bytes": 2000},
        {"override_expires_at": (utc_now() + timedelta(days=1)).isoformat()},
        {"reason": " "},
    ):
        assert (
            client.post(
                path + "/storage/entitlement", headers=auth_headers, json={**body, **change}
            ).status_code
            == 422
        )
    for delta in (-1, 31):
        invalid = {
            **body,
            "override_limit_bytes": 2000,
            "override_expires_at": (utc_now() + timedelta(days=delta)).isoformat(),
        }
        assert (
            client.post(
                path + "/storage/entitlement", headers=auth_headers, json=invalid
            ).status_code
            == 422
        )
    assert client.get(path + "/storage").json()["entitlement"]["version"] == 0


def test_expiring_override_revocation_history_and_atomic_audit(
    client, container, auth_headers, storage_api, monkeypatch
):
    path, _ = storage_api
    body = entitlement(
        limit=0,
        override_limit_bytes=2000,
        override_expires_at=(utc_now() + timedelta(days=1)).isoformat(),
    )
    response = client.post(path + "/storage/entitlement", headers=auth_headers, json=body)
    assert response.status_code == 200 and response.json()["effective_limit_bytes"] == 2000
    assert (
        client.post(path + "/storage/entitlement", headers=auth_headers, json=body).status_code
        == 200
    )
    assert len(container.store.storage_entitlement_history(DEV_WORKSPACE_ID)) == 1
    conflict = client.post(
        path + "/storage/entitlement",
        headers=auth_headers,
        json={**body, "reason": "Changed command"},
    )
    assert conflict.status_code == 409
    stale = client.post(path + "/storage/entitlement", headers=auth_headers, json=entitlement())
    assert stale.status_code == 409
    updated = client.post(
        path + "/storage/entitlement", headers=auth_headers, json=entitlement(1, 0)
    )
    assert updated.status_code == 200 and updated.json()["effective_limit_bytes"] == 0
    events = [
        e
        for e in container.store.audit_events(DEV_WORKSPACE_ID)
        if e.event_type == "native.storage.entitlement_updated"
    ]
    assert len(events) == 2 and events[-1].payload["before"]["override_limit_bytes"] == 2000
    assert events[-1].payload["after"]["override_limit_bytes"] is None

    def fail_audit(**kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(container.native_projects.audit, "record", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit"):
        client.post(path + "/storage/entitlement", headers=auth_headers, json=entitlement(2))
    assert container.store.storage_entitlement(DEV_WORKSPACE_ID).version == 2


def test_exhausted_workspace_preserves_second_tenant_progress_and_admin_targeting(
    client, container, auth_headers, storage_api
):
    path, first_project = storage_api
    assert (
        client.post(
            path + "/storage/entitlement", headers=auth_headers, json=entitlement(limit=0)
        ).status_code
        == 200
    )
    other_workspace, other_actor = uuid4(), uuid4()
    container.store.put_membership(
        Membership(actor_id=other_actor, workspace_id=other_workspace, role="owner")
    )
    admin_path = f"/v2/admin/workspaces/{other_workspace}/storage"
    admin_command = entitlement(limit=6)
    assert (
        client.post(
            admin_path + "/entitlement", headers=auth_headers, json=admin_command
        ).status_code
        == 200
    )
    assert client.get(admin_path).json()["effective_limit_bytes"] == 6
    receipt_path = admin_path + "/operations/" + admin_command["idempotency_key"]
    assert client.get(receipt_path).json()["applied_entitlement"]["version"] == 1
    assert client.get(admin_path + "/operations/missing-command").status_code == 404
    events = [
        e
        for e in container.store.audit_events(other_workspace)
        if e.event_type == "native.storage.entitlement_updated"
    ]
    assert len(events) == 1 and events[0].actor_id == DEV_ACTOR_ID
    assert events[0].payload["issuer_workspace_id"] == str(DEV_WORKSPACE_ID)
    _, headers = sign_in(
        client, container, actor_id=other_actor, workspace_id=other_workspace, role="owner"
    )
    other_project = create_project(client, headers)
    other_path = f"/v2/projects/{other_project['id']}"
    assert (
        client.put(
            other_path + "/intake",
            headers=headers,
            json={"expected_version": 0, "idempotency_key": str(uuid4())},
        ).status_code
        == 200
    )
    assert (
        client.post(other_path + "/intake/sources", headers=headers, json=upload_body()).status_code
        == 201
    )
    assert client.get(other_path + "/storage").json()["used_bytes"] == 6
    assert client.get(path + "/storage").status_code == 404
    assert client.get(f"/v2/admin/workspaces/{DEV_WORKSPACE_ID}/storage").status_code == 403
    assert client.get(receipt_path).status_code == 403
    assert (
        client.post(
            admin_path + "/entitlement", headers=headers, json=entitlement(1, 9999)
        ).status_code
        == 403
    )
    assert container.store.storage_usage(DEV_WORKSPACE_ID)["used_bytes"] == 0
    assert not container.store.native_intake_sources(DEV_WORKSPACE_ID, first_project)


def test_shared_member_sees_aggregates_without_administrator_notes_or_other_project_ids(
    client, container, auth_headers, storage_api
):
    path, project_id = storage_api
    client.post(
        path + "/storage/entitlement",
        headers=auth_headers,
        json=entitlement(reason="Private administrator note"),
    )
    member, _ = sign_in(client, container)
    container.store.put_native_project_member(
        NativeProjectMember(workspace_id=DEV_WORKSPACE_ID, project_id=project_id, actor_id=member)
    )
    response = client.get(path + "/storage")
    assert response.status_code == 200
    value = response.json()
    assert value["history"] == [] and not value["can_manage"]
    assert value["entitlement"]["reason"] == "" and value["entitlement"]["issued_by"] is None
    assert "Private administrator note" not in response.text
    container.store.delete_membership(member, DEV_WORKSPACE_ID)
    assert client.get(path + "/storage").status_code in {401, 403}


def test_administrator_manages_orphan_workspace_without_restoring_tenant_membership(
    client,
    container,
    auth_headers,
):
    workspace, former_owner = uuid4(), uuid4()
    container.store.put_membership(
        Membership(actor_id=former_owner, workspace_id=workspace, role="owner")
    )
    path = f"/v2/admin/workspaces/{workspace}/storage"
    first = entitlement(limit=100)
    assert client.post(path + "/entitlement", headers=auth_headers, json=first).status_code == 200
    container.store.delete_membership(former_owner, workspace)
    assert container.store.workspace_members(workspace, 0, 1) == ()
    response = client.get(path)
    assert response.status_code == 200 and response.json()["entitlement"]["version"] == 1
    changed = client.post(path + "/entitlement", headers=auth_headers, json=entitlement(1, 200))
    assert changed.status_code == 200 and changed.json()["entitlement"]["version"] == 2
    assert client.get(path + "/operations/" + first["idempotency_key"]).status_code == 200
    assert container.store.memberships(former_owner) == ()
    assert len(container.store.storage_entitlement_history(workspace)) == 2
    unknown = f"/v2/admin/workspaces/{uuid4()}/storage"
    assert client.get(unknown).status_code == 404
    assert (
        client.post(unknown + "/entitlement", headers=auth_headers, json=entitlement()).status_code
        == 404
    )
