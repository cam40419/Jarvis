"""The project file-location UI persists settings through authenticated APIs."""

from tests.integration.test_project_command_api import command_api as command_api
from tests.integration.test_project_command_api import member_api as member_api
from tests.integration.test_project_command_api import profile_api as profile_api


def test_file_location_settings_require_csrf_and_current_version(client, auth_headers, command_api):
    project_id, _, _ = command_api
    path = f"/v1/projects/{project_id}/workspace/file-locations"
    initial = client.get(path)
    assert initial.status_code == 200
    assert initial.json()["version"] == 0
    body = {"expected_version": 0, "locations": []}
    assert client.put(path, json=body).status_code == 403
    saved = client.put(path, headers=auth_headers, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["locations"] == []
    assert client.get(path).json()["version"] == saved.json()["version"] == 1
    assert client.put(path, headers=auth_headers, json=body).status_code == 409


def test_file_locations_reject_foreign_connections_and_outside_paths(
    client, auth_headers, command_api
):
    project_id, _, _ = command_api
    path = f"/v1/projects/{project_id}/workspace/file-locations"
    location = {
        "id": "foreign",
        "name": "Other account",
        "provider": "box",
        "connection_id": "link_foreign",
    }
    response = client.put(
        path, headers=auth_headers, json={"expected_version": 0, "locations": [location]}
    )
    assert response.status_code == 422, response.text
    assert client.get(path).json()["version"] == 0
    response = client.post(
        path + "/read",
        headers=auth_headers,
        json={"location_id": "foreign", "operation": "list", "arguments": {}},
    )
    assert response.status_code == 403


def test_project_storage_config_accepts_only_the_defined_fields(client, auth_headers, command_api):
    project_id, _, _ = command_api
    path = f"/v1/projects/{project_id}/workspace/file-locations"
    for extra in (
        {"credential": "secret"},
        {"workspace_id": "22222222-2222-4222-8222-222222222222"},
    ):
        body = {
            "expected_version": 0,
            "locations": [{"id": "local", "name": "Local", "provider": "local", **extra}],
        }
        assert client.put(path, headers=auth_headers, json=body).status_code == 422
