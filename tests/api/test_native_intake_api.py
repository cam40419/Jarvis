"""Session-authorized intake with real planner transport and immutable source downloads."""

import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from simon.domain.identity import DEV_WORKSPACE_ID
from simon.domain.native_models import ModelTemplate
from simon.domain.native_projects import NativeProjectMember
from simon.services.intake_sources import IntakeSourceBytes
from tests.api.test_native_project_api import create_project, sign_in
from tests.api.test_native_team_api import worker as worker
from tests.unit.test_intake_planner import endpoint, wire_result
from tests.unit.test_native_intake import role_document, seed_qualified_model, task_document


def install_planner(container):
    """Inject genuine model HTTP parsing while keeping every request synthetic."""
    state = SimpleNamespace(
        calls=[],
        proposal={
            "summary": "Reconcile the project evidence into a provisional charter.",
            "next_milestone": "Reviewed source-backed charter",
            "roles": [role_document()],
            "tasks": [task_document()],
        },
        review={"approved": True, "issues": []},
        hook=None,
    )

    def respond(request):
        state.calls.append(request)
        if state.hook:
            state.hook(request)
        reviewing = "Independently review a proposed project" in request.content.decode()
        document = state.review if reviewing else state.proposal
        return httpx.Response(
            200,
            json=wire_result(
                "openai_compatible", json.dumps(document), input_tokens=100, output_tokens=40
            ),
        )

    container.project_models.templates_factory = lambda workspace: (
        ModelTemplate(
            id="planning",
            name="Synthetic planning",
            workspace_ids=(workspace,),
            credential_required=False,
            endpoint=endpoint(input_cost_per_million_usd=1, output_cost_per_million_usd=2),
        ),
    )
    container.project_models.transport = httpx.MockTransport(respond)
    return state


def save_settings(client, path, headers, **changes):
    current = client.get(path).json()["intake"]
    container = client.app.state.container
    workspace, project_id = UUID(current["workspace_id"]), UUID(current["project_id"])
    project = container.store.native_project(workspace, project_id)
    seed_qualified_model(container.project_models, workspace, project_id, project.created_by)
    values = {
        key: value
        for key, value in current.items()
        if key not in {"project_id", "workspace_id", "version", "updated_at"}
    }
    response = client.put(
        path,
        headers=headers,
        json={
            **values,
            "expected_version": current["version"],
            "idempotency_key": f"settings-{uuid4()}",
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def save_limit(client, intake_path, headers, amount):
    path = intake_path.removesuffix("/intake") + "/models"
    policy = client.get(path).json()["project_policy"]
    response = client.put(
        path + "/policy",
        headers=headers,
        json={
            **{
                key: value
                for key, value in policy.items()
                if key not in {"workspace_id", "project_id", "version", "updated_at"}
            },
            "expected_version": policy["version"],
            "idempotency_key": f"limit-{uuid4()}",
            "lifetime_limit_microusd": amount,
            "daily_limit_microusd": amount,
            "monthly_limit_microusd": amount,
            "per_operation_limit_microusd": amount,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload_source(client, path, headers, *, content=b"Source evidence.", key="brief.txt"):
    version = client.get(path).json()["intake"]["version"]
    body = {
        "source_key": key,
        "filename": key.rsplit("/", 1)[-1],
        "media_type": "text/plain",
        "content_base64": base64.b64encode(content).decode(),
        "expected_version": version,
        "idempotency_key": f"source-{uuid4()}",
    }
    response = client.post(path + "/sources", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def analyze_body(client, path, **changes):
    return {
        "expected_version": client.get(path).json()["intake"]["version"],
        "idempotency_key": f"planning-{uuid4()}",
        **changes,
    }


@pytest.fixture
def intake_api(client, container, auth_headers, tmp_path):
    state = install_planner(container)
    container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}/intake"
    saved = save_settings(client, path, auth_headers)
    return SimpleNamespace(
        client=client,
        container=container,
        headers=auth_headers,
        project=project,
        path=path,
        saved=saved,
        state=state,
    )


def test_intake_mutations_require_real_session_csrf_origin_and_expected_workspace(intake_api):
    h = intake_api
    body = analyze_body(h.client, h.path)
    assert h.client.post(h.path + "/analyze", json=body).status_code == 403
    for headers in (
        {**h.headers, "Origin": "https://untrusted.example"},
        {**h.headers, "X-CSRF-Token": "invalid"},
        {**h.headers, "X-Workspace-ID": str(uuid4())},
    ):
        assert h.client.post(h.path + "/analyze", headers=headers, json=body).status_code == 403
    assert h.client.get(h.path, headers={"X-Workspace-ID": str(uuid4())}).status_code == 403
    h.client.cookies.clear()
    assert h.client.get(h.path).status_code == 401
    assert h.client.post(h.path + "/analyze", headers=h.headers, json=body).status_code == 401
    assert h.state.calls == []


@pytest.mark.parametrize("keep_human_cookie", [True, False])
def test_scoped_bearer_is_rejected_without_human_fallback(client, worker, keep_human_cookie):
    path = worker["path"] + "/intake"
    if not keep_human_cookie:
        client.cookies.clear()
    for authorization in (
        worker["headers"],
        {"Authorization": "Bearer invalid"},
        {"Authorization": "Basic invalid"},
    ):
        response = client.get(path, headers=authorization)
        assert response.status_code == 401
        assert worker["issued"]["token"] not in response.text
        response = client.post(
            path + "/analyze",
            headers={**worker["human_headers"], **authorization},
            json={"expected_version": 1, "idempotency_key": "forbidden-analysis"},
        )
        assert response.status_code == 401


def test_shared_member_reads_evidence_but_owner_alone_can_change_intake(intake_api):
    h = intake_api
    source, _ = upload_source(h.client, h.path, h.headers)
    member, headers = sign_in(h.client, h.container)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID, project_id=UUID(h.project["id"]), actor_id=member
        )
    )
    viewed = h.client.get(h.path)
    assert viewed.status_code == 200 and not viewed.json()["can_manage"]
    assert (
        h.client.get(h.path + f"/sources/{source['id']}/text").json()["text"] == "Source evidence."
    )
    assert h.client.get(h.path + f"/sources/{source['id']}/content").status_code == 200
    body = analyze_body(h.client, h.path)
    assert h.client.post(h.path + "/analyze", headers=headers, json=body).status_code == 403
    assert (
        h.client.post(
            h.path + f"/sources/{source['id']}/revoke", headers=headers, json=body
        ).status_code
        == 403
    )
    assert h.state.calls == []


@pytest.mark.parametrize("foreign_workspace", [False, True])
def test_private_project_sources_and_runs_are_hidden_from_other_principals(
    intake_api, foreign_workspace
):
    h = intake_api
    source, _ = upload_source(h.client, h.path, h.headers)
    run = h.client.post(
        h.path + "/analyze", headers=h.headers, json=analyze_body(h.client, h.path)
    ).json()
    _, headers = sign_in(
        h.client,
        h.container,
        workspace_id=uuid4() if foreign_workspace else DEV_WORKSPACE_ID,
        role="owner" if foreign_workspace else "member",
    )
    for suffix in (
        "",
        f"/sources/{source['id']}/text",
        f"/sources/{source['id']}/content",
        f"/runs/{run['id']}",
    ):
        assert h.client.get(h.path + suffix).status_code == 404
    assert (
        h.client.post(
            h.path + "/analyze",
            headers=headers,
            json={"expected_version": 2, "idempotency_key": "outsider-analysis"},
        ).status_code
        == 404
    )


def test_sources_have_redacted_preview_exact_attachment_and_revocation(intake_api):
    h = intake_api
    raw = b"API_KEY=private-evidence-key\nPublic project facts."
    source, command = upload_source(
        h.client, h.path, h.headers, content=raw, key="briefs/source.txt"
    )
    assert "text" not in source
    assert source["sha256"] == hashlib.sha256(raw).hexdigest()
    source_path = h.path + f"/sources/{source['id']}"
    preview = h.client.get(source_path + "/text")
    assert preview.status_code == 200
    assert "private-evidence-key" not in preview.text
    assert "REDACTED CREDENTIAL" in preview.json()["text"]
    original = h.client.get(source_path + "/content")
    assert original.status_code == 200 and original.content == raw
    assert original.headers["content-type"] == "application/octet-stream"
    assert original.headers["x-content-type-options"] == "nosniff"
    assert original.headers["content-disposition"].startswith("attachment;")
    assert "text" not in h.client.get(h.path).json()["sources"][0]
    replay = h.client.post(h.path + "/sources", headers=h.headers, json=command)
    assert replay.status_code == 201 and replay.json() == source
    assert (
        h.client.post(
            h.path + "/sources",
            headers=h.headers,
            json={**command, "idempotency_key": "stale-source-upload"},
        ).status_code
        == 409
    )
    version = h.client.get(h.path).json()["intake"]["version"]
    revoked = h.client.post(
        source_path + "/revoke",
        headers=h.headers,
        json={"expected_version": version, "idempotency_key": "revoke-source-once"},
    )
    assert revoked.status_code == 200
    assert h.client.get(source_path + "/content").status_code == 404
    assert h.client.get(source_path + "/text").status_code == 404


def test_download_integrity_failure_does_not_return_tampered_content(intake_api):
    h = intake_api
    source, _ = upload_source(h.client, h.path, h.headers)
    blob = (
        h.container.native_intake.source_bytes.root
        / str(DEV_WORKSPACE_ID)
        / h.project["id"]
        / source["sha256"]
    )
    blob.write_bytes(b"tampered-private-content")
    response = h.client.get(h.path + f"/sources/{source['id']}/content")
    assert response.status_code == 422
    assert "tampered-private-content" not in response.text


def test_true_planner_and_review_apply_once_with_auditable_usage(intake_api):
    h = intake_api
    source, _ = upload_source(h.client, h.path, h.headers)
    body = analyze_body(h.client, h.path, source_ids=[source["id"]])
    response = h.client.post(h.path + "/analyze", headers=h.headers, json=body)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "applied"
    assert run["review"] == {"approved": True, "issues": []}
    assert run["charged_microusd"] == 360 and run["reserved_microusd"] == 0
    assert len(run["applied_agent_ids"]) == 1 and len(run["applied_task_ids"]) == 2
    assert h.client.get(h.path + f"/runs/{run['id']}").json() == run
    assert h.client.post(h.path + "/analyze", headers=h.headers, json=body).json() == run
    assert len(h.state.calls) == 2
    assert (
        h.client.post(
            h.path + "/analyze", headers=h.headers, json={**body, "source_ids": []}
        ).status_code
        == 409
    )
    cancelled = h.client.post(
        h.path + f"/runs/{run['id']}/cancel",
        headers=h.headers,
        json={"expected_version": run["version"], "idempotency_key": "cancel-applied-run"},
    )
    assert cancelled.status_code == 409
    board = h.client.get(f"/v2/projects/{h.project['id']}/tasks").json()
    assert {task["id"] for task in board} == set(run["applied_task_ids"])


def test_manual_apply_is_versioned_and_stale_context_never_creates_roles(intake_api):
    h = intake_api
    save_settings(h.client, h.path, h.headers, auto_staff=False)
    run = h.client.post(
        h.path + "/analyze", headers=h.headers, json=analyze_body(h.client, h.path)
    ).json()
    assert run["status"] == "ready"
    save_settings(
        h.client, h.path, h.headers, constraints="Owner changed the direction", auto_staff=False
    )
    apply = h.client.post(
        h.path + f"/runs/{run['id']}/apply",
        headers=h.headers,
        json={"expected_version": run["version"], "idempotency_key": "stale-plan-apply"},
    )
    assert apply.status_code == 409
    assert h.client.get(f"/v2/projects/{h.project['id']}/team").json()["agents"] == []
    assert h.client.get(h.path).json()["spent_microusd"] == 360


def test_missing_configuration_and_insufficient_allowance_never_dispatch(
    client, container, auth_headers, intake_api
):
    h = intake_api
    save_limit(h.client, h.path, h.headers, 0)
    response = h.client.post(
        h.path + "/analyze", headers=h.headers, json=analyze_body(h.client, h.path)
    )
    assert response.status_code == 409
    assert h.state.calls == []
    project = create_project(client, auth_headers, key="unconfigured-project")
    path = f"/v2/projects/{project['id']}/intake"
    assert client.get(path.removesuffix("/intake") + "/models").json()["models"] == []
    saved = client.put(
        path,
        headers=auth_headers,
        json={"expected_version": 0, "idempotency_key": "save-unconfigured"},
    )
    assert saved.status_code == 200
    response = client.post(path + "/analyze", headers=auth_headers, json=analyze_body(client, path))
    assert response.status_code == 422
    assert client.get(path).json()["runs"] == []


def test_unknown_model_dispatch_is_visible_and_replay_does_not_spend_again(intake_api):
    h = intake_api

    def timeout(request):
        raise httpx.ReadTimeout("private provider diagnostics", request=request)

    h.state.hook = timeout
    body = analyze_body(h.client, h.path)
    response = h.client.post(h.path + "/analyze", headers=h.headers, json=body)
    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "unknown" and run["reserved_microusd"] > 0
    assert "private provider diagnostics" not in response.text
    assert h.client.post(h.path + "/analyze", headers=h.headers, json=body).json() == run
    assert len(h.state.calls) == 1


@pytest.mark.parametrize("operation", ["apply", "cancel"])
def test_command_replay_after_source_revocation_never_restores_cached_quotations(
    intake_api, operation
):
    h = intake_api
    save_settings(h.client, h.path, h.headers, auto_staff=False)
    source, _ = upload_source(
        h.client, h.path, h.headers, content=b"Revoke this private source quotation."
    )
    h.state.proposal["findings"] = [
        {
            "kind": "fact",
            "statement": "An intake source was supplied.",
            "evidence": [
                {"source_id": source["id"], "quote": "Revoke this private source quotation."}
            ],
        }
    ]
    run = h.client.post(
        h.path + "/analyze", headers=h.headers, json=analyze_body(h.client, h.path)
    ).json()
    assert run["status"] == "ready" and run["proposal"]["findings"]
    command = {"expected_version": run["version"], "idempotency_key": f"stable-{operation}-key"}
    command_path = h.path + f"/runs/{run['id']}/{operation}"
    performed = h.client.post(command_path, headers=h.headers, json=command)
    assert performed.status_code == 200, performed.text
    version = h.client.get(h.path).json()["intake"]["version"]
    revoked = h.client.post(
        h.path + f"/sources/{source['id']}/revoke",
        headers=h.headers,
        json={"expected_version": version, "idempotency_key": "revoke-evidence-after-command"},
    )
    assert revoked.status_code == 200, revoked.text
    replayed = h.client.post(command_path, headers=h.headers, json=command)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["status"] == performed.json()["status"]
    assert replayed.json()["proposal"] is None
    assert "private source quotation" not in replayed.text
    assert h.client.get(h.path + f"/runs/{run['id']}").json() == replayed.json()
    assert len(h.state.calls) == 2


def test_source_identity_cannot_be_read_through_another_project_path(intake_api):
    h = intake_api
    source, _ = upload_source(h.client, h.path, h.headers)
    other = create_project(h.client, h.headers, key="second-intake-project")
    other_path = f"/v2/projects/{other['id']}/intake/sources/{source['id']}"
    assert h.client.get(other_path + "/content").status_code == 404
    assert h.client.get(other_path + "/text").status_code == 404


def test_source_upload_accepts_bounded_files_above_generic_json_limit(intake_api):
    h = intake_api
    source, _ = upload_source(h.client, h.path, h.headers, content=b"evidence " * 30000)
    assert source["size_bytes"] == 270000 and source["truncated"]
    preview = h.client.get(h.path + f"/sources/{source['id']}/text").json()
    assert len(preview["text"]) == 20000
    assert preview["truncated"]
