from uuid import uuid4

from tests.integration.test_project_command_api import command_api as command_api
from tests.integration.test_project_command_api import member_api as member_api
from tests.integration.test_project_command_api import profile_api as profile_api


def test_drafts_require_current_version_and_csrf(client, auth_headers, command_api):
    project_id, _, _ = command_api
    path = f"/v1/projects/{project_id}/workspace/drafts/composer"
    assert client.get(path).json()["version"] == 0
    body = {
        "text": "Research supplier terms",
        "expected_version": 0,
        "idempotency_key": "api-draft-first",
    }
    assert client.put(path, json=body).status_code == 403
    saved = client.put(path, headers=auth_headers, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    assert client.put(path, headers=auth_headers, json=body).json() == saved.json()
    stale = client.put(
        path, headers=auth_headers, json={**body, "idempotency_key": "other-tab-draft"}
    )
    assert stale.status_code == 409
    assert client.get(path).json() == saved.json()


def test_workspace_records_and_readonly_briefing(client, auth_headers, command_api):
    project_id, _, _ = command_api
    base = f"/v1/projects/{project_id}/workspace"
    record_id = uuid4()
    body = {
        "title": "Supplier short list",
        "kind": "note",
        "summary": "Compare source-backed quotes.",
        "expected_version": 0,
        "idempotency_key": "api-record-first",
    }
    saved = client.put(base + f"/records/{record_id}", headers=auth_headers, json=body)
    assert saved.status_code == 200, saved.text
    assert client.get(base + "/records?query=quotes").json()["items"][0]["id"] == str(record_id)
    before = client.get(f"/v1/projects/{project_id}/command").json()["state"]
    briefing = client.get(base + "/briefing")
    assert briefing.status_code == 200
    assert briefing.json()["records"]["items"][0]["title"] == body["title"]
    assert client.get(f"/v1/projects/{project_id}/command").json()["state"] == before
    assert client.get(base + f"/records/{record_id}/revisions").json()["items"] == [saved.json()]
