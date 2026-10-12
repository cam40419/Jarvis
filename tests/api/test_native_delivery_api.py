"""Delivery packages are owner-created and freshly scoped at every metadata/file read."""

import io
import json
import zipfile
from uuid import UUID, uuid4

import pytest

from simon.domain.identity import DEV_WORKSPACE_ID
from simon.domain.models import utc_now
from simon.domain.native_projects import NativeProjectMember
from tests.api.test_native_artifacts_api import artifacts_api as artifacts_api
from tests.api.test_native_artifacts_api import head, upload
from tests.api.test_native_execution_api import execution_api as execution_api
from tests.api.test_native_project_api import sign_in
from tests.contract.test_native_intake_store import source


def command(h, detail):
    revision = head(detail)
    return {
        "title": "Editable source handoff",
        "components": [
            {
                "artifact_id": detail["artifact"]["id"],
                "revision_id": revision["id"],
                "sha256": revision["sha256"],
                "role": "source",
            }
        ],
        "limitations": ["Needs physical validation."],
        "idempotency_key": str(uuid4()),
    }


def packet(h):
    h.delivery_path = h.project_path + "/delivery-packets"
    artifact, _ = upload(h)
    body = command(h, artifact)
    response = h.client.post(h.delivery_path, headers=h.headers, json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


def test_packet_api_exact_manifest_and_portable_bytes(artifacts_api):
    h = artifacts_api
    value, body = packet(h)
    endpoint = h.delivery_path + "/" + value["packet"]["id"]
    response = h.client.get(endpoint + "/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-manifest-sha256"] == value["packet"]["manifest_sha256"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest == value["packet"]["manifest"]
        path = next(iter(json.loads(archive.read("export-paths.json")).values()))
        assert archive.read(path) == b"Candidate supported by source evidence."
    assert (
        h.client.post(h.delivery_path, headers=h.headers, json=body).json()["packet"]
        == value["packet"]
    )
    receipt = h.client.get(h.delivery_path + "/operations/" + body["idempotency_key"])
    assert receipt.json() == {"packet_id": value["packet"]["id"]}
    assert h.client.get(endpoint).json() == value
    assert not h.calls


def test_packet_auth_csrf_origin_workspace_and_bearer_fences(artifacts_api):
    h = artifacts_api
    value, body = packet(h)
    for changes in (
        {"Origin": "https://untrusted.example"},
        {"X-CSRF-Token": "invalid"},
        {"X-Workspace-ID": str(uuid4())},
    ):
        assert (
            h.client.post(h.delivery_path, headers=h.headers | changes, json=body).status_code
            == 403
        )
    assert h.client.post(h.delivery_path, json=body).status_code == 403
    assert (
        h.client.get(h.delivery_path, headers={"X-Workspace-ID": str(uuid4())}).status_code == 403
    )
    assert h.client.get(h.delivery_path, headers=h.runner_headers).status_code == 401
    h.client.cookies.clear()
    for suffix in ("", "/" + value["packet"]["id"], "/" + value["packet"]["id"] + "/download"):
        assert h.client.get(h.delivery_path + suffix).status_code == 401


def test_member_can_download_but_cannot_create_or_use_owner_receipt_and_revocation_is_live(
    artifacts_api,
):
    h = artifacts_api
    value, body = packet(h)
    identifier, headers = sign_in(h.client, h.container)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(h.project["id"]),
            actor_id=identifier,
        )
    )
    endpoint = h.delivery_path + "/" + value["packet"]["id"]
    assert h.client.get(h.delivery_path).json()["can_manage"] is False
    assert h.client.get(endpoint + "/download").status_code == 200
    assert h.client.post(h.delivery_path, headers=headers, json=body).status_code == 403
    assert (
        h.client.get(h.delivery_path + "/operations/" + body["idempotency_key"]).status_code == 403
    )
    h.container.store.delete_native_project_member(
        DEV_WORKSPACE_ID, UUID(h.project["id"]), identifier
    )
    for suffix in ("", "/download"):
        assert h.client.get(endpoint + suffix).status_code == 404


def test_revoked_source_packet_is_withheld_on_details_and_download(artifacts_api):
    from types import SimpleNamespace

    from simon.domain.identity import DEV_ACTOR_ID

    h = artifacts_api
    s = source(
        SimpleNamespace(
            workspace=DEV_WORKSPACE_ID,
            first=SimpleNamespace(id=UUID(h.project["id"])),
            owner=DEV_ACTOR_ID,
        )
    )
    h.container.store.insert_native_intake_source(s)
    value, _ = packet(h)
    h.container.store.revoke_native_intake_source(
        DEV_WORKSPACE_ID, UUID(h.project["id"]), s.id, utc_now()
    )
    endpoint = h.delivery_path + "/" + value["packet"]["id"]
    assert (
        h.client.get(endpoint).status_code
        == h.client.get(endpoint + "/download").status_code
        == 403
    )
    listing = h.client.get(h.delivery_path)
    assert listing.json()["packets"]["items"][0]["disclosure"] == "withheld"
    assert "Editable source handoff" not in listing.text


@pytest.mark.parametrize(
    "changes",
    [
        {"components": []},
        {"title": ""},
        {"limitations": ["a" * 1001]},
        {"components": [{"role": "source"}]},
    ],
)
def test_invalid_packet_commands_are_rejected_without_effects(artifacts_api, changes):
    h = artifacts_api
    artifact, _ = upload(h)
    body = command(h, artifact) | changes
    endpoint = h.project_path + "/delivery-packets"
    assert h.client.post(endpoint, headers=h.headers, json=body).status_code == 422
    assert h.client.get(endpoint).json()["packets"]["items"] == []
    assert h.client.get(endpoint + "/operations/" + body["idempotency_key"]).status_code == 404


def test_packet_paging_and_foreign_project_ids_are_validated(artifacts_api):
    h = artifacts_api
    value, _ = packet(h)
    for query in ("?limit=101", "?artifact_limit=0", "?offset=-1", "?artifact_offset=-1"):
        assert h.client.get(h.delivery_path + query).status_code == 422
    assert h.client.get(h.delivery_path + "/" + str(uuid4())).status_code == 404
    assert (
        h.client.get(f"/v2/projects/{uuid4()}/delivery-packets/{value['packet']['id']}").status_code
        == 404
    )
