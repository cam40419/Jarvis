"""Human-authorized typed inputs, exact declarations and selective repair over HTTP."""

import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from simon.domain.identity import DEV_WORKSPACE_ID
from simon.domain.native_intake import IntakeSource
from simon.domain.native_projects import NativeProjectMember
from tests.api.test_native_project_api import create_project, sign_in


def selector(path=None):
    return {"kind": "whole_file" if path is None else "json_pointer", "path": path or ""}


def fact_head(record):
    return next(
        item for item in record["revisions"] if item["id"] == record["fact"]["current_revision_id"]
    )


def artifact_head(record):
    return next(
        item
        for item in record["revisions"]
        if item["id"] == record["artifact"]["current_revision_id"]
    )


def seed_source(h, value=20, revision=1, *, project_id=None):
    text = json.dumps({"unit_price": value, "currency": "USD"})
    source = IntakeSource(
        workspace_id=DEV_WORKSPACE_ID,
        project_id=project_id or UUID(h.project["id"]),
        source_key="prices.json",
        revision=revision,
        filename="prices.json",
        media_type="application/json",
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        size_bytes=len(text.encode()),
        text=text,
        extraction_status="text",
        created_by=UUID(h.project["created_by"]),
    )
    h.container.store.insert_native_intake_source(source)
    return source


def fact_command(h, value=20, *, previous=None, source=None, **changes):
    source = source or h.source
    return {
        "fact_id": previous["fact"]["id"] if previous else None,
        "key": "unit-price",
        "kind": "fact",
        "expected_version": previous["fact"]["version"] if previous else 0,
        "value_type": "number",
        "value": value,
        "unit": "USD",
        "sources": [
            {
                "source_revision_id": str(source.id),
                "sha256": source.sha256,
                "selector": selector("/unit_price"),
            }
        ],
        "idempotency_key": str(uuid4()),
        **changes,
    }


def save_fact(h, value=20, **changes):
    body = fact_command(h, value, **changes)
    response = h.client.post(h.path + "/facts", headers=h.headers, json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def output(h, filename, text, media_type="text/plain"):
    task = h.client.post(
        h.project_path + "/tasks",
        headers=h.headers,
        json={"title": "Prepare " + filename, "idempotency_key": str(uuid4())},
    )
    assert task.status_code == 201, task.text
    response = h.client.post(
        h.project_path + "/artifacts/upload",
        headers=h.headers,
        json={
            "task_id": task.json()["id"],
            "expected_version": 0,
            "expected_task_version": task.json()["version"],
            "filename": filename,
            "media_type": media_type,
            "content_base64": base64.b64encode(text.encode()).decode(),
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def edge(record, *, kind="fact", input_path=None, output_path=None):
    head = fact_head(record) if kind == "fact" else artifact_head(record)
    return {
        "input_kind": kind,
        "input_id": record["fact" if kind == "fact" else "artifact"]["id"],
        "revision_id": head["id"],
        "sha256": head["sha256"],
        "input_selector": selector(input_path),
        "output_selector": selector(output_path),
    }


def declare(h, record, dependencies=(), coverage="complete"):
    head = artifact_head(record)
    current = h.client.get(h.path).json()
    row = next(
        item for item in current["outputs"] if item["artifact"]["id"] == record["artifact"]["id"]
    )
    body = {
        "expected_version": row["declaration"]["version"] if row["declaration"] else 0,
        "expected_artifact_version": record["artifact"]["version"],
        "revision_id": head["id"],
        "sha256": head["sha256"],
        "coverage": coverage,
        "dependencies": list(dependencies),
        "notes": "Owner attests the stated coverage.",
        "idempotency_key": str(uuid4()),
    }
    response = h.client.post(
        h.path + "/outputs/" + record["artifact"]["id"], headers=h.headers, json=body
    )
    assert response.status_code == 201, response.text
    return body


@pytest.fixture
def dependencies_api(client, container, auth_headers):
    project = create_project(client, auth_headers, key=str(uuid4()))
    h = SimpleNamespace(
        client=client,
        container=container,
        headers=auth_headers,
        project=project,
        project_path="/v2/projects/" + project["id"],
    )
    h.path = h.project_path + "/dependencies"
    response = client.put(
        h.project_path + "/intake",
        headers=auth_headers,
        json={"expected_version": 0, "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    h.source = seed_source(h)
    return h


def seed_graph(h):
    h.fact, _ = save_fact(h)
    h.spec = output(h, "spec.json", '{"unit_price":20,"color":"navy"}\n', "application/json")
    h.sheet = output(h, "line-sheet.md", "Unit price: USD 20. Color: navy.\n", "text/markdown")
    h.brand = output(h, "brand.txt", "Approved wordmark: STDOUT\n")
    h.notes = output(h, "notes.md", "Pricing discussion awaiting review\n", "text/markdown")
    declare(h, h.spec, [edge(h.fact, output_path="/unit_price")])
    declare(h, h.sheet, [edge(h.spec, kind="artifact", input_path="/unit_price")])
    declare(h, h.brand)
    declare(h, h.notes, coverage="partial")
    declare(
        h,
        h.spec,
        [
            edge(h.fact, output_path="/unit_price"),
            edge(h.sheet, kind="artifact", output_path="/unit_price"),
        ],
    )
    changed = seed_source(h, 25, 2)
    h.new_fact, _ = save_fact(h, 25, previous=h.fact, source=changed)
    body = {
        "fact_id": h.fact["fact"]["id"],
        "from_revision_id": fact_head(h.fact)["id"],
        "to_revision_id": fact_head(h.new_fact)["id"],
        "idempotency_key": str(uuid4()),
    }
    response = h.client.post(h.path + "/assessments", headers=h.headers, json=body)
    assert response.status_code == 201, response.text
    h.assessment = response.json()
    return h


def preview_command(h):
    head = artifact_head(h.spec)
    return {
        "assessment_id": h.assessment["assessment"]["id"],
        "edits": [
            {
                "artifact_id": h.spec["artifact"]["id"],
                "expected_version": h.spec["artifact"]["version"],
                "revision_id": head["id"],
                "sha256": head["sha256"],
                "replacements": [{"selector": selector("/unit_price"), "before": 20, "after": 25}],
            }
        ],
        "rationale": "Update the declared price field and preserve the approved color.",
        "idempotency_key": str(uuid4()),
    }


def test_dependency_api_requires_session_origin_csrf_and_current_workspace(dependencies_api):
    h = dependencies_api
    body = fact_command(h)
    assert h.client.post(h.path + "/facts", json=body).status_code == 403
    for changes in (
        {"Origin": "https://untrusted.example"},
        {"X-CSRF-Token": "wrong"},
        {"X-Workspace-ID": str(uuid4())},
        {"Authorization": "Bearer obsolete-runner"},
    ):
        assert h.client.post(
            h.path + "/facts", headers=h.headers | changes, json=body
        ).status_code in {401, 403}
    assert h.client.get(h.path, headers={"X-Workspace-ID": str(uuid4())}).status_code == 403
    h.client.cookies.clear()
    assert h.client.get(h.path).status_code == 401
    assert h.client.post(h.path + "/facts", headers=h.headers, json=body).status_code == 401


def test_typed_source_value_version_and_receipt_are_exact(dependencies_api):
    h = dependencies_api
    first, body = save_fact(h)
    replay = h.client.post(h.path + "/facts", headers=h.headers, json=body)
    assert replay.status_code == 201 and replay.json() == first
    receipt = h.client.get(h.path + "/operations/" + body["idempotency_key"])
    assert receipt.status_code == 200 and receipt.json() == {
        "kind": "fact",
        "id": first["fact"]["id"],
    }
    changed_body = fact_command(h, 25, previous=first)
    assert h.client.post(h.path + "/facts", headers=h.headers, json=changed_body).status_code == 422
    new_source = seed_source(h, 25, 2)
    second, _ = save_fact(h, 25, previous=first, source=new_source)
    stale = fact_command(h, 25, previous=first, source=new_source)
    assert h.client.post(h.path + "/facts", headers=h.headers, json=stale).status_code == 409
    assert second["fact"]["version"] == first["fact"]["version"] + 1
    assert len(second["revisions"]) == 2
    assert fact_head(second)["parent_revision_id"] == fact_head(first)["id"]
    assert fact_head(first) in second["revisions"]


def test_member_can_inspect_but_cannot_mutate_or_read_owner_receipts(dependencies_api):
    h = dependencies_api
    first, body = save_fact(h)
    actor, headers = sign_in(h.client, h.container)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(h.project["id"]),
            actor_id=actor,
            role="member",
        )
    )
    view = h.client.get(h.path)
    assert view.status_code == 200 and not view.json()["can_manage"]
    assert h.client.get(h.path + "/facts/" + first["fact"]["id"]).status_code == 200
    assert (
        h.client.post(
            h.path + "/facts", headers=headers, json=fact_command(h, previous=first)
        ).status_code
        == 403
    )
    assert h.client.get(h.path + "/operations/" + body["idempotency_key"]).status_code == 403


def test_direct_transitive_cycle_unknown_coverage_and_atomic_repair_are_visible(dependencies_api):
    h = seed_graph(dependencies_api)
    statuses = {
        item["artifact_id"]: item["status"] for item in h.assessment["assessment"]["outputs"]
    }
    assert statuses == {
        h.spec["artifact"]["id"]: "affected",
        h.sheet["artifact"]["id"]: "affected",
        h.brand["artifact"]["id"]: "unaffected",
        h.notes["artifact"]["id"]: "uncertain",
    }
    body = preview_command(h)
    response = h.client.post(h.path + "/repairs", headers=h.headers, json=body)
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview["current"] and preview["can_commit"] and not preview["committed_revision_ids"]
    saved = h.client.get(h.path).json()["repairs"]
    assert len(saved) == 1 and saved[0]["id"] == preview["proposal"]["id"]
    assert set(saved[0]) == {"id", "assessment_id", "created_at"}
    assert (
        h.client.get(h.project_path + "/artifacts/" + h.spec["artifact"]["id"]).json()["artifact"]
        == h.spec["artifact"]
    )
    commit = {"proposal_sha256": preview["proposal"]["sha256"], "idempotency_key": str(uuid4())}
    endpoint = h.path + "/repairs/" + preview["proposal"]["id"] + "/commit"
    committed = h.client.post(endpoint, headers=h.headers, json=commit)
    assert committed.status_code == 200, committed.text
    assert len(committed.json()["committed_revision_ids"]) == 1
    assert h.client.post(endpoint, headers=h.headers, json=commit).json() == committed.json()
    updated = h.client.get(h.project_path + "/artifacts/" + h.spec["artifact"]["id"]).json()
    assert updated["artifact"]["version"] == h.spec["artifact"]["version"] + 1
    assert updated["status"] != "accepted"
    current = artifact_head(updated)
    content = h.client.get(
        h.project_path
        + "/artifacts/"
        + h.spec["artifact"]["id"]
        + "/revisions/"
        + current["id"]
        + "/content"
    )
    assert json.loads(content.content) == {"unit_price": 25, "color": "navy"}
    for unchanged in (h.sheet, h.brand, h.notes):
        assert (
            h.client.get(h.project_path + "/artifacts/" + unchanged["artifact"]["id"]).json()[
                "artifact"
            ]
            == unchanged["artifact"]
        )
    assert h.sheet["artifact"]["id"] in committed.json()["proposal"]["plan"]["remaining_stale"]
    receipt = h.client.get(h.path + "/operations/" + commit["idempotency_key"])
    assert receipt.status_code == 200 and receipt.json()["id"] == preview["proposal"]["id"]


def test_revoked_source_withholds_retained_fact_assessment_and_repair_bodies(dependencies_api):
    h = seed_graph(dependencies_api)
    response = h.client.post(h.path + "/repairs", headers=h.headers, json=preview_command(h))
    assert response.status_code == 201, response.text
    preview = response.json()
    intake = h.client.get(h.project_path + "/intake").json()
    revoked = h.client.post(
        h.project_path + "/intake/sources/" + str(h.source.id) + "/revoke",
        headers=h.headers,
        json={"expected_version": intake["intake"]["version"], "idempotency_key": str(uuid4())},
    )
    assert revoked.status_code == 200, revoked.text
    fact = h.client.get(h.path + "/facts/" + h.fact["fact"]["id"]).json()
    assert fact["disclosure"] == "withheld" and fact["revisions"] == []
    assessment = h.client.get(h.path + "/assessments/" + h.assessment["assessment"]["id"]).json()
    assert assessment["disclosure"] == "withheld" and "outputs" not in assessment["assessment"]
    repair = h.client.get(h.path + "/repairs/" + preview["proposal"]["id"]).json()
    assert repair["disclosure"] == "withheld" and not repair["can_commit"]
    assert "plan" not in repair["proposal"]


def test_foreign_project_fact_binding_is_not_accepted(dependencies_api):
    h = dependencies_api
    other = create_project(h.client, h.headers, key=str(uuid4()), name="Separate source project")
    foreign = seed_source(h, project_id=UUID(other["id"]))
    response = h.client.post(
        h.path + "/facts", headers=h.headers, json=fact_command(h, source=foreign)
    )
    assert response.status_code in {403, 404}
    assert h.client.get(h.path).json()["facts"] == []
