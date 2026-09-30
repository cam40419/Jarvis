from simon.services.local_files import revision
from tests.contract.test_local_files import zip_bytes


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
