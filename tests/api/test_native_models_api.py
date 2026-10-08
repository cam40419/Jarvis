"""Human model enrollment, spend authority and real synthetic planning over HTTP."""

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from simon.domain.identity import DEV_WORKSPACE_ID, Membership
from simon.domain.model_routing import ModelEndpoint
from simon.domain.native_models import ModelTemplate
from simon.domain.native_projects import NativeProjectMember
from simon.services.project_models import ScopedModelSecrets
from tests.api.test_native_project_api import create_project, sign_in
from tests.api.test_native_team_api import worker as worker


def configure_model_http(container, *, keyed=False, paid=False, preserve_cipher=False):
    state = SimpleNamespace(calls=[], hook=None)

    def templates(workspace):
        return tuple(
            ModelTemplate(
                id=name,
                name=name.title(),
                workspace_ids=(workspace,),
                credential_required=keyed,
                endpoint=ModelEndpoint(
                    id=name,
                    model=f"synthetic-{name}",
                    provider="openai_compatible",
                    base_url="https://models.example/v1" if keyed else "http://127.0.0.1:8123/v1",
                    local=not keyed,
                    api_key_env="MODEL_PROJECT_KEY" if keyed else None,
                    input_cost_per_million_usd=1 if paid else 0,
                    output_cost_per_million_usd=2 if paid else 0,
                    context_window_tokens=128000,
                ),
            )
            for name in ("planner", "reviewer")
        )

    def response(request):
        state.calls.append(request)
        if state.hook:
            state.hook(request)
        body = request.content.decode()
        if "simon_model_probe" in body:
            result = {"simon_model_probe": True, "version": 1}
        elif "Independently review a proposed project" in body:
            result = {"approved": True, "issues": []}
        else:
            result = {
                "summary": "Confirm the source-backed project objective.",
                "next_milestone": "An agreed first deliverable.",
                "tasks": [
                    {
                        "key": "confirm-objective",
                        "title": "Confirm the project objective",
                        "description": "Compare the available options with the project owner.",
                        "acceptance": "The owner selects a measurable objective.",
                        "assignment": "human",
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "model": json.loads(request.content)["model"] + "-dated",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    container.project_models.templates_factory = templates
    container.project_models.transport = httpx.MockTransport(response)
    if not preserve_cipher:
        cipher = Fernet(Fernet.generate_key())
        container.project_models.secrets = ScopedModelSecrets(lambda: cipher)
    return state


def enroll_model(client, path, headers, *, template="planner", credential=None, key=None):
    command = {
        "template_id": template,
        "label": template.title(),
        "idempotency_key": key or f"enroll-{uuid4()}",
    }
    if credential is not None:
        command["credential"] = credential
    response = client.post(path + "/connections", headers=headers, json=command)
    assert response.status_code == 201, response.text
    return response.json(), command


def update_policy(client, path, headers, *, workspace=False, **changes):
    current = client.get(path).json()["workspace_policy" if workspace else "project_policy"]
    command = {
        key: value
        for key, value in current.items()
        if key not in {"workspace_id", "project_id", "version", "updated_at"}
    }
    command.update(
        expected_version=current["version"], idempotency_key=f"policy-{uuid4()}", **changes
    )
    response = client.put(
        path + ("/workspace-policy" if workspace else "/policy"), headers=headers, json=command
    )
    assert response.status_code == 200, response.text
    return response.json()


def grant_budget(client, path, headers, *, workspace=False, amount=1_000_000, cloud=False):
    return update_policy(
        client,
        path,
        headers,
        workspace=workspace,
        lifetime_limit_microusd=amount,
        daily_limit_microusd=amount,
        monthly_limit_microusd=amount,
        per_operation_limit_microusd=amount,
        **({} if workspace else {"allow_paid": True, "allow_cloud": cloud}),
    )


def probe_model(client, path, headers, model, *, key=None):
    command = {"expected_version": model["version"], "idempotency_key": key or f"probe-{uuid4()}"}
    response = client.post(
        path + f"/connections/{model['id']}/probe", headers=headers, json=command
    )
    assert response.status_code == 200, response.text
    return response.json(), command


@pytest.fixture
def models_api(client, container, auth_headers):
    state = configure_model_http(container)
    project = create_project(client, auth_headers)
    return SimpleNamespace(
        client=client,
        container=container,
        headers=auth_headers,
        state=state,
        project=project,
        path=f"/v2/projects/{project['id']}/models",
    )


def test_model_mutations_require_session_csrf_origin_and_expected_workspace(models_api):
    h = models_api
    command = {"template_id": "planner", "label": "Planner", "idempotency_key": "safe-enroll-model"}
    assert h.client.post(h.path + "/connections", json=command).status_code == 403
    for changes in (
        {"Origin": "https://untrusted.example"},
        {"X-CSRF-Token": "invalid"},
        {"X-Workspace-ID": str(uuid4())},
    ):
        assert (
            h.client.post(
                h.path + "/connections", headers=h.headers | changes, json=command
            ).status_code
            == 403
        )
    assert h.client.get(h.path, headers={"X-Workspace-ID": str(uuid4())}).status_code == 403
    h.client.cookies.clear()
    assert h.client.get(h.path).status_code == 401
    assert (
        h.client.post(h.path + "/connections", headers=h.headers, json=command).status_code == 401
    )
    assert h.state.calls == []


@pytest.mark.parametrize("keep_cookie", [True, False])
def test_models_reject_worker_bearer_even_alongside_valid_human_cookie(client, worker, keep_cookie):
    path = worker["path"] + "/models"
    if not keep_cookie:
        client.cookies.clear()
    for authorization in (worker["headers"], {"Authorization": "Bearer invalid"}):
        assert client.get(path, headers=authorization).status_code == 401
        response = client.post(
            path + "/connections",
            headers=worker["human_headers"] | authorization,
            json={
                "template_id": "planner",
                "label": "Planner",
                "idempotency_key": "worker-enroll-model",
            },
        )
        assert response.status_code == 401
        assert worker["issued"]["token"] not in response.text


def test_enrollment_secrets_are_never_returned_or_echoed_and_retry_keys_bind_credential(models_api):
    h = models_api
    state = configure_model_http(h.container, keyed=True, paid=True)
    secret = "sk-synthetic-enrollment-secret"
    model, command = enroll_model(h.client, h.path, h.headers, credential=secret)
    assert state.calls == [] and not model["ready"]
    assert model["has_credential"]
    assert h.client.post(h.path + "/connections", headers=h.headers, json=command).json() == model
    assert (
        h.client.post(
            h.path + "/connections",
            headers=h.headers,
            json=command | {"credential": "different-key"},
        ).status_code
        == 409
    )
    receipt = h.client.get(h.path + "/operations/" + command["idempotency_key"])
    assert receipt.status_code == 200 and receipt.json() == model
    for response in (receipt, h.client.get(h.path), h.client.get(h.path + "/usage")):
        assert secret not in response.text
        assert "encrypted_secret" not in response.text
        assert "api_key_env" not in response.text
    for changes in (
        {"base_url": "http://127.0.0.1/private"},
        {"label": ""},
        {"credential": secret * 200},
    ):
        response = h.client.post(h.path + "/connections", headers=h.headers, json=command | changes)
        assert response.status_code == 422 and secret not in response.text
    assert secret not in json.dumps(
        [event.model_dump(mode="json") for event in h.container.store.audit_events()]
    )
    credential = h.container.store.project_model_credential(
        DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(model["id"]), 1
    )
    assert secret not in credential.model_dump_json()
    assert h.client.get(h.path + "/operations/not-a-real-command").status_code == 404


def test_shared_readers_project_owners_and_workspace_owners_have_distinct_model_authority(
    models_api,
):
    h = models_api
    model, command = enroll_model(h.client, h.path, h.headers)
    actor_id, headers = sign_in(h.client, h.container)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(h.project["id"]),
            actor_id=actor_id,
            role="member",
        )
    )
    view = h.client.get(h.path).json()
    assert not view["can_manage"] and not view["can_manage_workspace"] and not view["can_reconcile"]
    assert view["workspace_totals"] is None and view["workspace_policy"] is None
    assert h.client.get(h.path + "/usage").status_code == 200
    assert h.client.get(h.path + "/operations/" + command["idempotency_key"]).status_code == 403
    assert h.client.post(h.path + "/connections", headers=headers, json=command).status_code == 403
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(h.project["id"]),
            actor_id=actor_id,
            role="owner",
        )
    )
    view = h.client.get(h.path).json()
    assert view["can_manage"] and not view["can_manage_workspace"]
    update_policy(h.client, h.path, headers, planning_model_id=UUID(model["id"]).__str__())
    policy_command = {"expected_version": 0, "idempotency_key": "member-workspace-policy"}
    assert (
        h.client.put(h.path + "/workspace-policy", headers=headers, json=policy_command).status_code
        == 403
    )
    # Idempotent credential reads/writes must still recheck current owner authority.
    _, own_command = enroll_model(h.client, h.path, headers, template="reviewer")
    h.container.store.put_membership(
        Membership(actor_id=actor_id, workspace_id=DEV_WORKSPACE_ID, role="guest")
    )
    assert h.client.get(h.path).status_code == 200
    assert (
        h.client.post(h.path + "/connections", headers=headers, json=own_command).status_code == 403
    )
    assert h.client.get(h.path + "/operations/" + own_command["idempotency_key"]).status_code == 403
    h.container.store.delete_membership(actor_id, DEV_WORKSPACE_ID)
    assert h.client.get(h.path).status_code == 403


def test_model_connection_policy_and_usage_ids_cannot_cross_project_or_workspace(models_api):
    h = models_api
    model, command = enroll_model(h.client, h.path, h.headers)
    second = create_project(h.client, h.headers, key="second-model-project")
    second_path = f"/v2/projects/{second['id']}/models"
    assert (
        h.client.put(
            second_path + f"/connections/{model['id']}",
            headers=h.headers,
            json={
                "expected_version": 1,
                "idempotency_key": "cross-project-model",
                "label": "Forbidden",
                "enabled": True,
            },
        ).status_code
        == 404
    )
    assert (
        h.client.put(
            second_path + "/policy",
            headers=h.headers,
            json={
                "expected_version": 0,
                "idempotency_key": "cross-project-policy",
                "planning_model_id": model["id"],
            },
        ).status_code
        == 422
    )
    assert (
        h.client.get(second_path + "/operations/" + command["idempotency_key"]).status_code == 404
    )
    sign_in(h.client, h.container, workspace_id=uuid4(), role="owner")
    assert h.client.get(h.path).status_code == 404
    assert h.client.get(h.path + "/usage").status_code == 404
    assert h.client.get(h.path + "/operations/" + command["idempotency_key"]).status_code == 404


def test_qualified_separate_models_drive_real_intake_and_keep_per_call_usage(models_api):
    h = models_api
    planner, _ = enroll_model(h.client, h.path, h.headers)
    reviewer, _ = enroll_model(h.client, h.path, h.headers, template="reviewer")
    planner, _ = probe_model(h.client, h.path, h.headers, planner)
    reviewer, _ = probe_model(h.client, h.path, h.headers, reviewer)
    assert planner["ready"] and reviewer["ready"]
    update_policy(
        h.client, h.path, h.headers, planning_model_id=planner["id"], review_model_id=reviewer["id"]
    )
    intake_path = h.path.removesuffix("/models") + "/intake"
    saved = h.client.put(
        intake_path,
        headers=h.headers,
        json={
            "background": "Rough project information",
            "outcomes": "Agree the first useful milestone",
            "expected_version": 0,
            "idempotency_key": "model-test-intake",
        },
    )
    assert saved.status_code == 200, saved.text
    command = {"expected_version": 1, "idempotency_key": "real-model-intake"}
    result = h.client.post(intake_path + "/analyze", headers=h.headers, json=command)
    assert result.status_code == 200, result.text
    run = result.json()
    assert run["status"] == "applied" and len(run["usage_ids"]) == 2
    assert run["endpoint_id"] != run["review_endpoint_id"]
    assert [json.loads(request.content)["model"] for request in h.state.calls] == [
        "synthetic-planner",
        "synthetic-reviewer",
        "synthetic-planner",
        "synthetic-reviewer",
    ]
    assert h.client.post(intake_path + "/analyze", headers=h.headers, json=command).json() == run
    assert len(h.state.calls) == 4
    usages = h.client.get(h.path + "/usage?limit=2").json()
    assert usages["has_more"] and len(usages["items"]) == 2
    assert {value["phase"] for value in usages["items"]} == {"generation", "review"}
    assert all(
        value["status"] == "settled" and value["reported_model"].endswith("-dated")
        for value in usages["items"]
    )
    assert all("endpoint_snapshot" not in value for value in usages["items"])
    assert len(h.client.get(h.path.removesuffix("/models") + "/tasks").json()) == 2


def test_unknown_probe_holds_workspace_ceiling_until_explicit_reconciliation(
    models_api, monkeypatch
):
    h = models_api
    state = configure_model_http(h.container, keyed=True, paid=True)
    first, _ = enroll_model(h.client, h.path, h.headers, credential="synthetic-first-key")
    second_project = create_project(h.client, h.headers, key="second-paid-project")
    second_path = f"/v2/projects/{second_project['id']}/models"
    second, _ = enroll_model(h.client, second_path, h.headers, credential="synthetic-second-key")
    reservation = first["qualification_reservation_microusd"]
    grant_budget(h.client, h.path, h.headers, workspace=True, amount=reservation + 100)
    for path in (h.path, second_path):
        grant_budget(h.client, path, h.headers, cloud=True)
    state.hook = lambda request: (_ for _ in ()).throw(
        httpx.ReadTimeout("synthetic timeout", request=request)
    )
    failed, _ = probe_model(h.client, h.path, h.headers, first)
    assert not failed["ready"]
    usage = h.client.get(h.path + "/usage").json()["items"][0]
    assert usage["status"] == "unknown" and usage["held_microusd"] == reservation
    command = {"expected_version": 1, "idempotency_key": "second-project-check"}
    blocked = h.client.post(
        second_path + f"/connections/{second['id']}/probe", headers=h.headers, json=command
    )
    assert blocked.status_code == 409 and len(state.calls) == 1
    reconcile = {
        "expected_version": usage["version"],
        "idempotency_key": "resolve-provider-charge",
        "charged_microusd": 25,
        "reason": "Provider final usage checked",
        "evidence": "Synthetic invoice item 1234",
    }
    endpoint = h.path + f"/usage/{usage['id']}/reconcile"
    assert h.client.post(endpoint, headers=h.headers, json=reconcile).status_code == 409
    record = h.container.store.model_usage(
        DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(usage["id"])
    )
    monkeypatch.setattr(
        "simon.services.model_usage.utc_now", lambda: record.deadline_at + timedelta(seconds=1)
    )
    resolved = h.client.post(endpoint, headers=h.headers, json=reconcile)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "reconciled" and resolved.json()["charged_microusd"] == 25
    assert resolved.json()["held_microusd"] == 0
    assert h.client.post(endpoint, headers=h.headers, json=reconcile).json() == resolved.json()
    monkeypatch.undo()
    state.hook = None
    assert probe_model(h.client, second_path, h.headers, second)[0]["ready"]
    view = h.client.get(h.path).json()
    assert view["workspace_totals"]["charged_lifetime"] == 145
    assert view["workspace_totals"]["held_microusd"] == 0


def test_model_policy_inputs_and_usage_pagination_are_bounded(models_api):
    h = models_api
    for values in (
        {"lifetime_limit_microusd": True},
        {"max_concurrent_calls": 33},
        {"daily_limit_microusd": -1},
    ):
        response = h.client.put(
            h.path + "/policy",
            headers=h.headers,
            json={
                "expected_version": 0,
                "idempotency_key": "invalid-policy",
                **values,
            },
        )
        assert response.status_code == 422
    for query in ("limit=0", "limit=101", "offset=-1", "offset=1000001"):
        assert h.client.get(h.path + "/usage?" + query).status_code == 422
    assert h.client.get(h.path + "/usage").json() == {"items": [], "has_more": False}


def test_workspace_policy_cannot_receive_project_only_authority(models_api):
    h = models_api
    for field, value in (
        ("allow_paid", True),
        ("allow_cloud", True),
        ("planning_model_id", str(uuid4())),
    ):
        response = h.client.put(
            h.path + "/workspace-policy",
            headers=h.headers,
            json={
                "expected_version": 0,
                "idempotency_key": f"invalid-workspace-{field}",
                field: value,
            },
        )
        assert response.status_code == 422
