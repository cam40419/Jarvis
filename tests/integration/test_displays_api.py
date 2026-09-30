from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient


def test_display_provision_upload_configure_and_device_refresh(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        "/v1/displays", headers=auth_headers, json={"name": "Kitchen frame"}
    )
    assert created.status_code == 201
    display = created.json()
    assert display["configuration"]["layout"] == "overlay"
    assert "token=" in display["provisioning_url"]

    image = client.post(
        f"/v1/displays/{display['id']}/images",
        headers=auth_headers,
        files={"image": ("view.png", b"\x89PNG\r\n\x1a\nsynthetic", "image/png")},
    )
    assert image.status_code == 201
    image_id = image.json()["id"]

    configured = client.patch(
        f"/v1/displays/{display['id']}",
        headers=auth_headers,
        json={"layout": "split", "slide_seconds": 12},
    )
    assert configured.status_code == 200
    assert configured.json()["configuration"]["layout"] == "split"
    assert configured.json()["revision"] == 3

    parsed = urlsplit(display["provisioning_url"])
    token = parse_qs(parsed.query)["token"][0]
    provision = client.get(
        f"/display/{display['id']}?token={token}", follow_redirects=False
    )
    assert provision.status_code == 303
    assert "simon_display_token=" in provision.headers["set-cookie"]
    assert "token=" not in provision.headers["location"]

    state = client.get(f"/v1/displays/{display['id']}/state")
    assert state.status_code == 200
    assert state.json()["display"]["configuration"]["slide_seconds"] == 12
    assert state.json()["display"]["images"][0]["id"] == image_id
    served = client.get(f"/v1/displays/{display['id']}/images/{image_id}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"


def test_display_endpoints_reject_bad_device_token_and_bad_image(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    display = client.post(
        "/v1/displays", headers=auth_headers, json={"name": "Office"}
    ).json()
    client.cookies.set("simon_display_token", "wrong")
    assert client.get(f"/v1/displays/{display['id']}/state").status_code == 403
    invalid = client.post(
        f"/v1/displays/{display['id']}/images",
        headers=auth_headers,
        files={"image": ("fake.png", b"not an image", "image/png")},
    )
    assert invalid.status_code == 422


def test_display_management_requires_signed_in_owner(client: TestClient) -> None:
    assert client.get("/v1/displays").status_code == 401
    assert client.post("/v1/displays", json={"name": "Nope"}).status_code == 401
