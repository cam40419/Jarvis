"""Real sessions, schema commands and exact package receipts over HTTP."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx

from tests.api.test_native_dependencies_api import artifact_head, declare, output
from tests.api.test_native_project_api import create_project, sign_in
from tests.unit.test_native_acceptance import SCHEMA


def setup_api(client, container, headers):
    container.project_models.transport = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(
            AssertionError("Package validation cannot call a model.")
        )
    )
    project = create_project(client, headers, key=str(uuid4()))
    path = "/v2/projects/" + project["id"]
    h = SimpleNamespace(
        client=client,
        container=container,
        headers=headers,
        project=project,
        project_path=path,
        path=path + "/dependencies",
    )
    policy = client.put(
        path + "/artifacts/policy",
        headers=headers,
        json={
            "expected_version": 0,
            "require_agent_review": False,
            "idempotency_key": str(uuid4()),
        },
    )
    assert policy.status_code == 200, policy.text
    files = []
    for name, media, text in (
        ("spec.json", "application/json", '{"unit_price":20,"color":"navy"}'),
        ("rationale.md", "text/markdown", "# Rationale\nSupported package and limits.\n"),
        ("schema.json", "application/json", json.dumps({"schema_version": 1, "schema": SCHEMA})),
    ):
        record = output(h, name, text, media)
        declare(h, record)
        head = artifact_head(record)
        approved = client.post(
            path + "/artifacts/" + record["artifact"]["id"] + "/decisions",
            headers=headers,
            json={
                "expected_version": record["artifact"]["version"],
                "expected_task_version": record["task"]["version"],
                "expected_policy_version": policy.json()["version"],
                "revision_id": head["id"],
                "sha256": head["sha256"],
                "decision": "approved",
                "idempotency_key": str(uuid4()),
            },
        )
        assert approved.status_code == 200, approved.text
        files.append(approved.json())
    response = client.post(
        path + "/delivery-packets",
        headers=headers,
        json={
            "title": "Supported package",
            "components": [
                {
                    "artifact_id": file["artifact"]["id"],
                    "revision_id": artifact_head(file)["id"],
                    "sha256": artifact_head(file)["sha256"],
                    "role": "source",
                }
                for file in files
            ],
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    h.packet = response.json()["packet"]
    h.acceptance_path = path + "/delivery-packets/" + h.packet["id"] + "/acceptance"
    h.assignment = {
        "expected_version": 0,
        "profile_name": "editable-text-json",
        "profile_version": 1,
        "validator_version": 1,
        "roles": [
            {"role": role, "revision_id": artifact_head(file)["id"]}
            for role, file in zip(
                ("json_specification", "markdown_rationale", "schema_and_check_report"),
                files,
                strict=True,
            )
        ],
        "probe": {
            "selector": {"kind": "json_pointer", "path": "/unit_price"},
            "before": 20,
            "after": 25,
        },
        "idempotency_key": str(uuid4()),
    }
    return h


def complete(h):
    assignment = h.client.put(h.acceptance_path + "/profile", headers=h.headers, json=h.assignment)
    assert assignment.status_code == 201, assignment.text
    assigned = assignment.json()["assignment"]
    validation = {
        "assignment_id": assigned["id"],
        "expected_profile_version": assigned["version"],
        "manifest_sha256": h.packet["manifest_sha256"],
        "validator_version": 1,
        "idempotency_key": str(uuid4()),
    }
    result = h.client.post(h.acceptance_path + "/validate", headers=h.headers, json=validation)
    assert result.status_code == 201, result.text
    assert result.json()["latest_validation"]["passed"]
    approval = {
        **validation,
        "validation_id": result.json()["latest_validation"]["id"],
        "authority_sha256": result.json()["latest_validation"]["authority_sha256"],
        "idempotency_key": str(uuid4()),
    }
    result = h.client.post(h.acceptance_path + "/decisions", headers=h.headers, json=approval)
    assert result.status_code == 201, result.text
    assert result.json()["applicability"]["accepted"]
    return result.json(), validation, approval


def test_profile_validate_approve_receipts_and_extra_passed_claims(client, container, auth_headers):
    h = setup_api(client, container, auth_headers)
    value, validation, approval = complete(h)
    replay = client.post(h.acceptance_path + "/decisions", headers=h.headers, json=approval)
    assert (
        replay.status_code == 201
        and replay.json()["latest_acceptance"]["id"] == value["latest_acceptance"]["id"]
    )
    for key in (
        h.assignment["idempotency_key"],
        validation["idempotency_key"],
        approval["idempotency_key"],
    ):
        receipt = client.get(h.acceptance_path + "/operations/" + key)
        assert receipt.status_code == 200 and receipt.json()["id"]
    response = client.post(
        h.acceptance_path + "/validate", headers=h.headers, json={**validation, "passed": True}
    )
    assert response.status_code == 422
    response = client.put(
        h.acceptance_path + "/profile",
        headers=h.headers,
        json={
            **h.assignment,
            "profile_version": 99,
            "expected_version": 1,
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 422


def test_other_project_nonowner_machine_and_workspace_headers_cannot_approve(
    client, container, auth_headers
):
    h = setup_api(client, container, auth_headers)
    _, _, command = complete(h)
    assert (
        client.get(h.acceptance_path, headers={"X-Workspace-Id": str(uuid4())}).status_code == 403
    )
    other = create_project(client, auth_headers, key=str(uuid4()))
    assert client.get(h.acceptance_path.replace(h.project["id"], other["id"])).status_code == 404
    _, member_headers = sign_in(client, container)
    response = client.post(h.acceptance_path + "/decisions", headers=member_headers, json=command)
    assert response.status_code == 404
    assert client.get(h.acceptance_path).status_code == 404
