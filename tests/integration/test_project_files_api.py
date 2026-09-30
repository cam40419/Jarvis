import base64
from time import time

from cryptography.fernet import Fernet
from pydantic import SecretStr

from simon.adapters.google import DRIVE_WRITE_SCOPE, GoogleTokens
from simon.domain.connected_tools import GoogleConnection
from tests.contract.test_project_files import FakeDrive


def test_project_api_upload_reads_and_csrf(client, container, auth_headers):
    service = container.connected
    service.settings = service.settings.model_copy(
        update={
            "google_client_id": "synthetic",
            "google_client_secret": SecretStr("synthetic"),
            "google_token_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    _, actor = container.identity.resolve(client.cookies.get("simon_session"))
    tokens = GoogleTokens(
        access_token="synthetic", refresh_token="synthetic", expires_at=time() + 3600
    )
    service.store.save_google_connection(
        GoogleConnection(
            household_id=actor.household_id,
            actor_id=actor.actor_id,
            email="synthetic@example.com",
            scopes=(DRIVE_WRITE_SCOPE,),
            encrypted_tokens=service.encrypt(tokens.model_dump_json()),
        )
    )
    service.projects.api = FakeDrive()
    body = {"name": "API project", "description": "Test project", "idempotency_key": "api-project"}
    assert client.post("/v1/projects", json=body).status_code == 403
    result = client.post("/v1/projects", json=body, headers=auth_headers)
    assert result.status_code == 200, result.text
    project = result.json()
    assert project["drive"]["status"] == "ready"
    path = f"/v1/projects/{project['id']}/upload"
    body = {
        "name": "uploaded.txt",
        "content_base64": base64.b64encode(b"hello").decode(),
        "idempotency_key": "upload-project",
    }
    assert client.post(path, json=body).status_code == 403
    upload = client.post(path, json=body, headers=auth_headers).json()
    assert upload["status"] == "succeeded"
    assert client.post(path, json=body, headers=auth_headers).json() == upload
    files = client.get(f"/v1/projects/{project['id']}/files").json()
    assert len(files["files"]) == 1
    read = client.get(f"/v1/projects/{project['id']}/files/{upload['file_id']}").json()
    assert read["text"] == "hello"
    assert client.get("/v1/work/overview").json()["projects"][0]["drive"]["folder_id"]
    assert client.get(f"/v1/projects/{project['id']}/history").status_code == 200

    from tests.contract.test_drive_management import BrowsableDrive

    service.projects.api = BrowsableDrive(service.projects.api)
    browse = client.post("/v1/projects/drive/browse", json={}, headers=auth_headers)
    assert browse.status_code == 200 and browse.json()["folder"]["id"] == "my-drive"
    link = {
        "project_id": project["id"],
        "folder_id": "root",
        "expected_version": project["drive"]["version"],
    }
    linked = client.post("/v1/projects/link-drive", json=link, headers=auth_headers).json()
    assert linked["folder_id"] == "my-drive"
    body = {
        "project_id": project["id"],
        "file_id": project["drive"]["folder_id"],
        "revision": "1",
        "idempotency_key": "trash-old-folder",
    }
    assert client.post("/v1/projects/trash-drive-item", json=body).status_code == 403
    trashed = client.post("/v1/projects/trash-drive-item", json=body, headers=auth_headers)
    assert trashed.status_code == 200 and trashed.json()["status"] == "succeeded"
    assert (
        client.post("/v1/projects/trash-drive-item", json=body, headers=auth_headers).json()
        == trashed.json()
    )
    body = {"project_id": project["id"], "expected_version": linked["version"]}
    assert client.post("/v1/projects/unlink-drive", json=body).status_code == 403
    result = client.post("/v1/projects/unlink-drive", json=body, headers=auth_headers)
    assert result.json()["status"] == "unlinked"
    assert (
        client.post(f"/v1/projects/{project['id']}/sync", json={}, headers=auth_headers).json()[
            "status"
        ]
        == "unlinked"
    )
