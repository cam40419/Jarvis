import struct
import zlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from simon.api.app import create_app
from simon.domain.identity import Membership
from simon.services.local_files import revision
from tests.contract.test_local_files import zip_bytes


def png_bytes():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(
            ">I", zlib.crc32(kind + value),
        )

    return b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0),
    ) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x80\xff\xff")) + chunk(b"IEND", b"")


def test_local_api_upload_extract_read_download_and_csrf(client, container, auth_headers, tmp_path):
    service = container.connected.local_files
    container.connected.settings = container.connected.settings.model_copy(
        update={
            "local_files_enabled": True,
            "local_files_dir": tmp_path / "files",
        }
    )
    params = {"root": "workspace", "path": "upload.zip", "idempotency_key": "upload-once"}
    content = zip_bytes([("folder/readme.md", "Test contents")])
    assert client.post("/v1/local-files/upload", params=params, content=content).status_code == 403
    response = client.post(
        "/v1/local-files/upload", params=params, content=content, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    assert (
        client.post(
            "/v1/local-files/upload", params=params, content=content, headers=auth_headers
        ).json()
        == response.json()
    )
    assert (
        client.get("/v1/local-files/list", params={"root": "workspace"}).json()["files"][0]["name"]
        == "upload.zip"
    )
    body = {
        "name": "local_zip_extract",
        "arguments": {
            "root": "workspace",
            "path": "upload.zip",
            "destination_root": "workspace",
            "destination_path": "extracted",
        },
        "idempotency_key": "extract-once",
    }
    assert client.post("/v1/local-files/action", json=body).status_code == 403
    extracted = client.post("/v1/local-files/action", json=body, headers=auth_headers)
    assert extracted.status_code == 200, extracted.text
    params = {"root": "workspace", "path": "extracted/folder/readme.md"}
    result = client.get("/v1/local-files/read", params=params)
    assert result.json()["text"] == "Test contents"
    assert result.json()["revision"] == revision(b"Test contents")
    downloaded = client.get("/v1/local-files/download", params=params)
    assert downloaded.content == b"Test contents"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert (
        client.get(
            "/v1/local-files/read", params={"root": "workspace", "path": "../outside"}
        ).status_code
        == 422
    )
    invalid = {"name": "local_file_write", "arguments": {}, "idempotency_key": "invalid-write"}
    assert (
        client.post("/v1/local-files/action", json=invalid, headers=auth_headers).status_code == 422
    )
    _, actor = container.identity.resolve(client.cookies.get("simon_session"))
    assert (
        service.workspace(actor).joinpath("extracted/folder/readme.md").read_text()
        == "Test contents"
    )

    for endpoint in ("list", "read", "download"):
        assert (
            client.get(
                "/v1/local-files/" + endpoint,
                params={"root": "x" * 101, "path": "readme.md"},
            ).status_code
            == 422
        )
    assert (
        client.get("/v1/local-files/list", params={"root": "workspace", "offset": -1}).status_code
        == 422
    )


@pytest.fixture
def preview_files(client, container, auth_headers, tmp_path):
    container.connected.settings = container.connected.settings.model_copy(update={
        "local_files_enabled": True, "local_files_dir": tmp_path / "files",
    })
    _, actor = container.identity.resolve(client.cookies.get("simon_session"))
    base = container.connected.local_files.workspace(actor)
    base.mkdir(parents=True, exist_ok=True)
    return base, actor


@pytest.mark.parametrize("name,content,mime", [
    ("photo.png", png_bytes(), "image/png"),
    ("photo.jpeg", b"\xff\xd8\xff\xe0image", "image/jpeg"),
    ("animation.gif", b"GIF89aimage", "image/gif"),
    ("picture.webp", b"RIFF\x10\x00\x00\x00WEBPimage", "image/webp"),
    ("report.pdf", b"%PDF-1.7\n% synthetic document\n%%EOF", "application/pdf"),
    ("misnamed.html", png_bytes(), "image/png"),
])
def test_preview_uses_magic_content_and_sandbox_headers(client, preview_files, name, content, mime):
    base, _ = preview_files
    (base / name).write_bytes(content)
    response = client.get("/v1/local-files/preview", params={"root": "workspace", "path": name})
    assert response.status_code == 200 and response.content == content
    assert response.headers["content-type"] == mime
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["content-disposition"].startswith("inline;")
    assert "sandbox;" in response.headers["content-security-policy"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "allow-scripts" not in response.headers["content-security-policy"]


@pytest.mark.parametrize("content", [
    b"<!doctype html><script>alert(1)</script>",
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b"plain text", b"PK\x03\x04zip archive", b"",
])
def test_preview_rejects_active_and_misnamed_content(client, preview_files, content):
    base, _ = preview_files
    (base / "unsafe.png").write_bytes(content)
    response = client.get(
        "/v1/local-files/preview", params={"root": "workspace", "path": "unsafe.png"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


def test_preview_requires_auth_and_cannot_read_another_accounts_files(
    client, container, preview_files,
):
    base, actor = preview_files
    (base / "private.png").write_bytes(png_bytes())
    params = {"root": "workspace", "path": "private.png"}
    with TestClient(create_app(container), base_url="http://localhost:8000") as anonymous:
        assert anonymous.get("/v1/local-files/preview", params=params).status_code == 401
    other = actor.model_copy(update={"actor_id": uuid4()})
    container.store.put_membership(Membership(
        actor_id=other.actor_id, household_id=other.household_id, role="owner",
        display_name="Other account", household_name="Home",
    ))
    token, _ = container.identity._issue(other.actor_id, other.household_id, "development")
    with TestClient(create_app(container), base_url="http://localhost:8000") as second:
        second.cookies.set("simon_session", token)
        assert second.get("/v1/local-files/preview", params=params).status_code == 422
        params["path"] = f"../{actor.actor_id}/private.png"
        assert second.get("/v1/local-files/preview", params=params).status_code == 422


def test_preview_revalidates_account_after_read(client, container, preview_files, monkeypatch):
    base, actor = preview_files
    (base / "private.png").write_bytes(png_bytes())
    original = container.connected.local_files.blob

    def revoked(path, *args):
        raw = original(path, *args)
        container.store.put_membership(Membership(
            actor_id=actor.actor_id, household_id=actor.household_id, role="guest",
            display_name="Revoked role", household_name="Home",
        ))
        return raw

    monkeypatch.setattr(container.connected.local_files, "blob", revoked)
    result = client.get(
        "/v1/local-files/preview", params={"root": "workspace", "path": "private.png"},
    )
    assert result.status_code == 403
