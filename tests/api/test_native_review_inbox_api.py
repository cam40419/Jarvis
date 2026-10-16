"""The review queue uses live human membership and workspace context."""

from uuid import UUID, uuid4

from simon.domain.identity import DEV_WORKSPACE_ID
from simon.domain.native_projects import NativeProjectMember
from tests.api.test_native_artifacts_api import artifacts_api as artifacts_api
from tests.api.test_native_artifacts_api import upload
from tests.api.test_native_execution_api import execution_api as execution_api
from tests.api.test_native_project_api import sign_in


def test_review_queue_owner_member_and_revoked_membership(artifacts_api):
    h = artifacts_api
    upload(h)
    path = h.project_path + "/review-inbox"
    value = h.client.get(path).json()
    assert value["can_manage"] and value["summary"]["counts"]["needs_review"] == 1
    assert h.client.get(path, params={"limit": 0}).status_code == 422
    assert h.client.get(path, headers={"X-Workspace-ID": str(uuid4())}).status_code == 403
    member, _ = sign_in(h.client, h.container)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(h.project["id"]),
            actor_id=member,
        )
    )
    response = h.client.get(path)
    assert response.status_code == 200 and not response.json()["can_manage"]
    h.container.store.delete_native_project_member(DEV_WORKSPACE_ID, UUID(h.project["id"]), member)
    assert h.client.get(path).status_code == 404
    assert not h.calls


def test_review_queue_rejects_worker_bearer_and_anonymous_requests(artifacts_api):
    h = artifacts_api
    path = h.project_path + "/review-inbox"
    assert h.client.get(path, headers=h.runner_headers).status_code == 401
    h.client.cookies.clear()
    assert h.client.get(path).status_code == 401
