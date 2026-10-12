"""Authenticated artifact transfer and review decisions against exact revisions."""

import base64
import hashlib
from uuid import UUID, uuid4

import pytest

from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.native_projects import NativeProjectMember
from tests.api.test_native_execution_api import (
    claim_execution,
    execute_step,
    start_execution,
)
from tests.api.test_native_execution_api import execution_api as execution_api
from tests.api.test_native_models_api import probe_model
from tests.api.test_native_project_api import create_project, sign_in
from tests.api.test_native_team_api import role_body


@pytest.fixture
def artifacts_api(execution_api):
    h = execution_api
    h.artifact_path = h.project_path + "/artifacts"
    response = h.client.post(
        h.project_path + "/agents", headers=h.headers, json=role_body(key="quality")
    )
    assert response.status_code == 201, response.text
    h.reviewer = response.json()
    response = h.client.put(
        h.artifact_path + "/policy",
        headers=h.headers,
        json={
            "expected_version": 0,
            "reviewer_agent_id": h.reviewer["id"],
            "idempotency_key": "artifact-review-policy",
        },
    )
    assert response.status_code == 200, response.text
    h.policy = response.json()
    return h


def upload_command(h, content=b"Candidate supported by source evidence.", **changes):
    task = h.client.get(h.project_path + "/tasks/" + h.task["id"]).json()
    return {
        "task_id": h.task["id"],
        "expected_version": 0,
        "expected_task_version": task["version"],
        "filename": "candidate.txt",
        "media_type": "text/plain",
        "content_base64": base64.b64encode(content).decode(),
        "idempotency_key": f"artifact-upload-{uuid4()}",
        **changes,
    }


def upload(h, content=b"Candidate supported by source evidence.", **changes):
    command = upload_command(h, content, **changes)
    response = h.client.post(h.artifact_path + "/upload", headers=h.headers, json=command)
    assert response.status_code == 201, response.text
    return response.json(), command


def head(detail):
    return next(
        revision
        for revision in detail["revisions"]
        if revision["id"] == detail["artifact"]["current_revision_id"]
    )


def target_command(detail, **changes):
    current = head(detail)
    return {
        "expected_version": detail["artifact"]["version"],
        "expected_task_version": detail["task"]["version"],
        "expected_policy_version": detail["policy"]["version"],
        "revision_id": current["id"],
        "sha256": current["sha256"],
        "idempotency_key": f"artifact-target-{uuid4()}",
        **changes,
    }


def review(h, detail, *, approved=True):
    current = head(detail)
    h.actions = [
        {
            "revision_id": current["id"],
            "sha256": current["sha256"],
            "verdict": "approved" if approved else "changes_requested",
            "summary": "Checked the task requirements against the exact uploaded candidate.",
            "findings": [] if approved else ["The claimed source evidence is absent."],
        }
    ]
    response = h.client.post(
        h.artifact_path + "/" + detail["artifact"]["id"] + "/reviews",
        headers=h.headers,
        json=target_command(detail),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_artifact_routes_require_human_session_csrf_origin_and_workspace(artifacts_api):
    h = artifacts_api
    command = upload_command(h)
    assert h.client.post(h.artifact_path + "/upload", json=command).status_code == 403
    for changes in (
        {"Origin": "https://untrusted.example"},
        {"X-CSRF-Token": "invalid"},
        {"X-Workspace-ID": str(uuid4())},
    ):
        response = h.client.post(
            h.artifact_path + "/upload", headers=h.headers | changes, json=command
        )
        assert response.status_code == 403
    assert (
        h.client.get(h.artifact_path, headers={"X-Workspace-ID": str(uuid4())}).status_code == 403
    )
    h.client.cookies.clear()
    assert h.client.get(h.artifact_path).status_code == 401
    assert (
        h.client.post(h.artifact_path + "/upload", headers=h.headers, json=command).status_code
        == 401
    )
    assert h.calls == []


@pytest.mark.parametrize("keep_cookie", [True, False])
def test_artifact_routes_reject_execution_bearer_even_alongside_human_cookie(
    artifacts_api, keep_cookie
):
    h = artifacts_api
    command = upload_command(h)
    if not keep_cookie:
        h.client.cookies.clear()
    assert h.client.get(h.artifact_path, headers=h.runner_headers).status_code == 401
    assert (
        h.client.post(
            h.artifact_path + "/upload", headers=h.runner_headers, json=command
        ).status_code
        == 401
    )


def test_candidate_publication_is_visible_before_any_review_or_owner_acceptance(artifacts_api):
    h = artifacts_api
    run, _ = start_execution(h)
    result = execute_step(h, claim_execution(h))
    assert result["status"] == "completed"
    response = h.client.get(h.artifact_path)
    assert response.status_code == 200
    artifacts = response.json()["artifacts"]["items"]
    assert len(artifacts) == 1
    detail = h.client.get(h.artifact_path + "/" + artifacts[0]["id"]).json()
    revision_value = head(detail)
    assert revision_value["run_id"] == run["id"]
    assert detail["task"]["status"] == "in_review"
    assert detail["reviews"] == [] and detail["decisions"] == []
    assert "content_base64" not in response.text
    assert "context" not in detail


def test_html_upload_is_plain_preview_and_authenticated_attachment(artifacts_api):
    h = artifacts_api
    content = (
        b'<script>window.artifactExecuted=true</script>\n<a href="javascript:alert(1)">Link</a>'
    )
    detail, _ = upload(h, content, filename='quoted" candidate.html', media_type="text/html")
    revision_value = head(detail)
    base = h.artifact_path + "/" + detail["artifact"]["id"] + "/revisions/" + revision_value["id"]
    preview = h.client.get(base + "/preview")
    assert preview.status_code == 200 and preview.headers["content-type"].startswith(
        "application/json"
    )
    assert preview.json()["text"] == content.decode()
    download = h.client.get(base + "/content")
    assert download.status_code == 200 and download.content == content
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "\r" not in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in download.headers["content-security-policy"]
    assert revision_value["sha256"] == hashlib.sha256(download.content).hexdigest()
    h.client.cookies.clear()
    assert h.client.get(base + "/preview").status_code == 401
    assert h.client.get(base + "/content").status_code == 401
    assert not h.calls


def test_generic_binary_upload_remains_exact_and_review_requires_supported_content(artifacts_api):
    h = artifacts_api
    content = bytes(range(256))
    detail, _ = upload(h, content, filename="reference.bin", media_type="application/octet-stream")
    revision_value = head(detail)
    base = h.artifact_path + "/" + detail["artifact"]["id"]
    downloaded = h.client.get(base + "/revisions/" + revision_value["id"] + "/content")
    assert downloaded.status_code == 200 and downloaded.content == content
    assert revision_value["size_bytes"] == 256
    denied = h.client.post(base + "/reviews", headers=h.headers, json=target_command(detail))
    assert denied.status_code in (409, 422)
    assert not h.calls


@pytest.mark.parametrize(
    "filename",
    ["../escaped.txt", "C:\\private.txt", "nested/report.txt", "bad\r\nheader.txt", ".."],
)
def test_unsafe_display_filenames_are_rejected_without_saving(artifacts_api, filename):
    h = artifacts_api
    response = h.client.post(
        h.artifact_path + "/upload", headers=h.headers, json=upload_command(h, filename=filename)
    )
    assert response.status_code == 422
    assert h.client.get(h.artifact_path).json()["artifacts"]["items"] == [] and not h.calls


def test_owner_review_and_approval_are_separate_exact_version_operations(artifacts_api):
    h = artifacts_api
    detail, _ = upload(h)
    path = h.artifact_path + "/" + detail["artifact"]["id"]
    command = target_command(detail, decision="approved")
    assert h.client.post(path + "/decisions", headers=h.headers, json=command).status_code == 409
    checked = review(h, detail)
    assert checked["verdict"] == "approved"
    still_pending = h.client.get(path).json()
    assert still_pending["task"]["status"] == "in_review" and not still_pending["decisions"]
    command = target_command(still_pending, decision="approved")
    approved = h.client.post(path + "/decisions", headers=h.headers, json=command)
    assert approved.status_code == 200, approved.text
    decision = approved.json()["decisions"][0]
    assert decision["review_id"] == checked["id"] and decision["sha256"] == command["sha256"]
    assert approved.json()["task"]["status"] == "done"
    replay = h.client.post(path + "/decisions", headers=h.headers, json=command)
    assert replay.status_code == 200 and replay.json()["decisions"][0]["id"] == decision["id"]
    assert len(h.calls) == 1


def test_requested_changes_create_linked_repair_with_feedback_and_no_inference(artifacts_api):
    h = artifacts_api
    detail, _ = upload(h)
    command = target_command(
        detail, decision="changes_requested", feedback="Cite the source for each numerical claim."
    )
    path = h.artifact_path + "/" + detail["artifact"]["id"]
    response = h.client.post(path + "/decisions", headers=h.headers, json=command)
    assert response.status_code == 200, response.text
    decision = response.json()["decisions"][0]
    repair = h.client.get(h.project_path + "/tasks/" + decision["repair_task_id"]).json()
    assert repair["assignment"]["agent_id"] == h.agent["id"]
    assert (
        command["feedback"] in repair["description"] and command["sha256"] in repair["description"]
    )
    assert repair["status"] == "todo" and not h.calls
    again = h.client.post(path + "/decisions", headers=h.headers, json=command)
    assert (
        again.status_code == 200 and again.json()["decisions"][0]["repair_task_id"] == repair["id"]
    )


def test_member_reads_are_scoped_and_review_spend_or_owner_decisions_are_denied(artifacts_api):
    h = artifacts_api
    detail, upload_body = upload(h)
    path = h.artifact_path + "/" + detail["artifact"]["id"]
    member_id, headers = sign_in(h.client, h.container)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID, project_id=UUID(h.project["id"]), actor_id=member_id
        )
    )
    assert h.client.get(path).status_code == 200
    revision_value = head(detail)
    assert h.client.get(path + "/revisions/" + revision_value["id"] + "/content").status_code == 200
    assert (
        h.client.post(path + "/reviews", headers=headers, json=target_command(detail)).status_code
        == 403
    )
    assert (
        h.client.post(
            path + "/decisions", headers=headers, json=target_command(detail, decision="approved")
        ).status_code
        == 403
    )
    assert (
        h.client.post(h.artifact_path + "/upload", headers=headers, json=upload_body).status_code
        == 403
    )
    h.container.store.delete_native_project_member(
        DEV_WORKSPACE_ID, UUID(h.project["id"]), member_id
    )
    assert h.client.get(path).status_code == 404
    assert h.client.get(path + "/revisions/" + revision_value["id"] + "/content").status_code == 404
    assert not h.calls


def test_foreign_project_and_workspace_cannot_read_artifact_or_revision(artifacts_api):
    h = artifacts_api
    detail, _ = upload(h)
    revision_value = head(detail)
    other = create_project(h.client, h.headers, key="artifact-other-project")
    path = f"/v2/projects/{other['id']}/artifacts/{detail['artifact']['id']}"
    assert h.client.get(path).status_code == 404
    assert h.client.get(path + "/revisions/" + revision_value["id"] + "/content").status_code == 404
    sign_in(h.client, h.container, workspace_id=uuid4(), role="owner")
    original_path = h.artifact_path + "/" + detail["artifact"]["id"]
    assert h.client.get(original_path).status_code == 404
    assert (
        h.client.get(original_path + "/revisions/" + revision_value["id"] + "/content").status_code
        == 404
    )


def test_owner_authority_is_checked_before_successful_upload_receipt_replay(artifacts_api):
    h = artifacts_api
    detail, command = upload(h)
    replay = h.client.post(h.artifact_path + "/upload", headers=h.headers, json=command)
    assert replay.status_code == 201 and replay.json()["artifact"]["id"] == detail["artifact"]["id"]
    h.container.store.delete_membership(DEV_ACTOR_ID, DEV_WORKSPACE_ID)
    assert (
        h.client.post(h.artifact_path + "/upload", headers=h.headers, json=command).status_code
        == 403
    )
    assert not h.calls


@pytest.mark.parametrize("limit", ["complete_context", "model_capacity"])
def test_review_admission_capacity_errors_are_safe_422_without_any_durable_attempt(
    artifacts_api, limit
):
    h = artifacts_api
    if limit == "complete_context":
        task_response = h.client.put(
            h.project_path + "/tasks/" + h.task["id"],
            headers=h.headers,
            json={
                **{
                    field: h.task[field]
                    for field in ("title", "description", "status", "assignment")
                },
                "description": "T" * 8000,
                "expected_version": h.task["version"],
                "idempotency_key": "large-review-task-brief",
            },
        )
        assert task_response.status_code == 200, task_response.text
        role_response = h.client.put(
            h.project_path + "/agents/" + h.reviewer["id"],
            headers=h.headers,
            json={
                **{
                    field: h.reviewer[field]
                    for field in (
                        "name",
                        "instructions",
                        "success_criteria",
                        "rationale",
                        "status",
                        "can_manage_team",
                    )
                },
                "instructions": "R" * 8000,
                "expected_version": h.reviewer["version"],
                "idempotency_key": "large-review-role-instructions",
            },
        )
        assert role_response.status_code == 200, role_response.text
        content = (b"private-review-evidence-" * 2200)[:48000]
        message = "review context limit"
    else:
        catalog = h.container.project_models.templates_factory

        def narrow_catalog(workspace):
            return tuple(
                row.model_copy(
                    update={
                        "endpoint": row.endpoint.model_copy(
                            update={"context_window_tokens": 4096, "max_output_tokens": 1024}
                        )
                    }
                )
                for row in catalog(workspace)
            )

        h.container.project_models.templates_factory = narrow_catalog
        model = h.container.store.project_models(DEV_WORKSPACE_ID, UUID(h.project["id"]))[0]
        h.actions = [{"simon_model_probe": True, "version": 1}]
        probe_model(h.client, h.project_path + "/models", h.headers, model.model_dump(mode="json"))
        content = b"private-review-evidence-" * 200
        message = "enough context capacity"
    detail, _ = upload(h, content)
    command = target_command(detail)
    tasks = h.container.store.native_tasks(DEV_WORKSPACE_ID, UUID(h.project["id"]), 0, 100)
    usages = h.container.store.model_usage_entries(DEV_WORKSPACE_ID, UUID(h.project["id"]))
    h.calls.clear()
    response = h.client.post(
        h.artifact_path + "/" + detail["artifact"]["id"] + "/reviews",
        headers=h.headers,
        json=command,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert message in response.json()["error"]["message"]
    assert "private-review-evidence" not in response.text
    assert h.container.store.native_tasks(DEV_WORKSPACE_ID, UUID(h.project["id"]), 0, 100) == tasks
    assert h.container.store.model_usage_entries(DEV_WORKSPACE_ID, UUID(h.project["id"])) == usages
    assert h.client.get(h.artifact_path + "/" + detail["artifact"]["id"]).json()["reviews"] == []
    assert (
        h.client.get(h.artifact_path + "/operations/" + command["idempotency_key"]).status_code
        == 404
    )
    assert not h.calls
