"""Real sessions, scoped immutable handoffs and native worker takeover through HTTP."""

import base64
import io
import json
import zipfile
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import utc_now
from simon.domain.native_projects import NativeProjectMember
from simon.domain.native_storage import StorageEntitlement
from tests.api.test_native_artifacts_api import artifacts_api as artifacts_api
from tests.api.test_native_artifacts_api import upload
from tests.api.test_native_execution_api import claim_execution, lease_command, start_execution
from tests.api.test_native_execution_api import execution_api as execution_api
from tests.api.test_native_project_api import sign_in
from tests.contract.test_native_intake_store import source


def handoff(h, recipient=DEV_ACTOR_ID):
    path = h.project_path + "/handoffs"
    command = {
        "title": "Authorized successor handoff",
        "recipient_id": str(recipient),
        "idempotency_key": str(uuid4()),
    }
    response = h.client.post(path, headers=h.headers, json=command)
    assert response.status_code == 201, response.text
    return path, response.json(), command


def test_api_exact_snapshot_receipt_export_and_read_only_verify(artifacts_api):
    h = artifacts_api
    upload(h)
    path, value, command = handoff(h)
    endpoint = path + "/" + value["handoff"]["id"]
    assert (
        h.client.post(path, headers=h.headers, json=command).json()["handoff"] == value["handoff"]
    )
    receipt = h.client.get(path + "/operations/" + command["idempotency_key"])
    assert receipt.json() == {"kind": "create", "id": value["handoff"]["id"]}
    exported = h.client.get(endpoint + "/download")
    assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert json.loads(archive.read("manifest.json")) == value["handoff"]["manifest"]
        assert "current-status.json" in archive.namelist()
        assert (
            archive.read(next(name for name in archive.namelist() if name.startswith("files/")))
            == b"Candidate supported by source evidence."
        )
    verified = h.client.post(
        endpoint + "/verify",
        headers=h.headers,
        json={"content_base64": base64.b64encode(exported.content).decode()},
    )
    assert verified.status_code == 200 and verified.json()["valid"]
    assert not h.calls


def test_handoff_auth_origin_csrf_workspace_and_worker_credentials(artifacts_api):
    h = artifacts_api
    path, value, command = handoff(h)
    for changes in (
        {"Origin": "https://untrusted.example"},
        {"X-CSRF-Token": "invalid"},
        {"X-Workspace-ID": str(uuid4())},
    ):
        assert h.client.post(path, headers=h.headers | changes, json=command).status_code == 403
    assert h.client.post(path, json=command).status_code == 403
    assert h.client.get(path, headers=h.runner_headers).status_code == 401
    h.client.cookies.clear()
    assert h.client.get(path + "/" + value["handoff"]["id"]).status_code == 401


def test_named_member_recipient_reads_but_does_not_inherit_owner_resume_authority(artifacts_api):
    h = artifacts_api
    owner_cookies = dict(h.client.cookies)
    recipient, recipient_headers = sign_in(h.client, h.container)
    recipient_cookies = dict(h.client.cookies)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID, project_id=UUID(h.project["id"]), actor_id=recipient
        )
    )
    h.client.cookies.clear()
    h.client.cookies.update(owner_cookies)
    run, _ = start_execution(h)
    path, value, command = handoff(h, recipient)
    h.client.cookies.clear()
    h.client.cookies.update(recipient_cookies)
    endpoint = path + "/" + value["handoff"]["id"]
    assert h.client.get(endpoint + "/download").status_code == 200
    assert not h.client.get(endpoint).json()["can_resume"]
    assert h.client.post(path, headers=recipient_headers, json=command).status_code == 403
    response = h.client.post(
        endpoint + "/resume",
        headers=recipient_headers,
        json={
            "manifest_sha256": value["handoff"]["manifest_sha256"],
            "run_id": run["id"],
            "expected_run_version": run["version"],
            "runner_id": h.runner["runner"]["id"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 403
    h.container.store.delete_native_project_member(
        DEV_WORKSPACE_ID, UUID(h.project["id"]), recipient
    )
    assert h.client.get(endpoint).status_code == 404


def test_revocation_masks_saved_snapshot_and_blocks_export(artifacts_api):
    h = artifacts_api
    evidence = source(
        SimpleNamespace(
            workspace=DEV_WORKSPACE_ID,
            first=SimpleNamespace(id=UUID(h.project["id"])),
            owner=DEV_ACTOR_ID,
        ),
        text="Private measurement",
    )
    h.container.store.insert_native_intake_source(evidence)
    path, value, _ = handoff(h)
    endpoint = path + "/" + value["handoff"]["id"]
    h.container.store.revoke_native_intake_source(
        DEV_WORKSPACE_ID, UUID(h.project["id"]), evidence.id, utc_now()
    )
    withheld = h.client.get(endpoint).json()
    assert not withheld["disclosure_allowed"] and "manifest" not in withheld["handoff"]
    assert h.client.get(endpoint + "/download").status_code == 403
    assert h.client.get(path).json()["items"][0]["title"] == "Withheld handoff"


def test_second_human_and_worker_http_takeover_completed_checkpoint(monkeypatch, artifacts_api):
    h = artifacts_api
    run, _ = start_execution(h)
    lease = claim_execution(h)
    execution = h.container.native_execution
    apply = execution._advance_checkpoint
    monkeypatch.setattr(
        execution,
        "_advance_checkpoint",
        lambda *args: (_ for _ in ()).throw(RuntimeError("Checkpoint crash")),
    )
    with pytest.raises(RuntimeError, match="Checkpoint crash"):
        h.runner_client.post(
            "/v2/execution-worker/step", headers=h.runner_headers, json=lease_command(lease)
        )
    monkeypatch.setattr(execution, "_advance_checkpoint", apply)
    current = execution._run(DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(run["id"]))
    execution._save(current, lease_until=utc_now() - timedelta(seconds=1))
    owner_cookies = dict(h.client.cookies)
    recipient, headers = sign_in(h.client, h.container)
    recipient_cookies = dict(h.client.cookies)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(h.project["id"]),
            actor_id=recipient,
            role="owner",
        )
    )
    worker = h.client.post(
        h.path + "/runners",
        headers=headers,
        json={"name": "Second native runtime", "idempotency_key": str(uuid4())},
    )
    assert worker.status_code == 201, worker.text
    h.client.cookies.clear()
    h.client.cookies.update(owner_cookies)
    path, value, _ = handoff(h, recipient)
    h.client.cookies.clear()
    h.client.cookies.update(recipient_cookies)
    current = execution._run(DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(run["id"]))
    body = {
        "manifest_sha256": value["handoff"]["manifest_sha256"],
        "run_id": run["id"],
        "expected_run_version": current.version,
        "runner_id": worker.json()["runner"]["id"],
        "idempotency_key": str(uuid4()),
    }
    endpoint = path + "/" + value["handoff"]["id"] + "/resume"
    assert h.client.post(endpoint, headers=headers, json=body).json()["status"] == "authorized"
    with TestClient(h.client.app, base_url="http://localhost:8000") as successor:
        worker_headers = {"Authorization": "Bearer " + worker.json()["token"]}
        new_lease = successor.post(
            "/v2/execution-worker/claim",
            headers=worker_headers,
            json={"idempotency_key": str(uuid4())},
        ).json()
        finished = successor.post(
            "/v2/execution-worker/step", headers=worker_headers, json=lease_command(new_lease)
        )
        assert finished.status_code == 200 and finished.json()["status"] == "completed"
    assert len(h.calls) == 1


def test_handoff_resources_match_storage_administrator_redaction_for_member_and_export(
    artifacts_api,
):
    h = artifacts_api
    owner_cookies = dict(h.client.cookies)
    recipient, _ = sign_in(h.client, h.container)
    recipient_cookies = dict(h.client.cookies)
    h.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=DEV_WORKSPACE_ID, project_id=UUID(h.project["id"]), actor_id=recipient
        )
    )
    h.container.store.save_storage_entitlement(
        StorageEntitlement(
            workspace_id=DEV_WORKSPACE_ID,
            version=1,
            plan_key="trial",
            limit_bytes=1_000_000,
            reason="Administrative allowance note is private",
            issued_by=DEV_ACTOR_ID,
        ),
        0,
    )
    h.client.cookies.clear()
    h.client.cookies.update(owner_cookies)
    path, value, _ = handoff(h, recipient)
    endpoint = path + "/" + value["handoff"]["id"]
    entitlement = value["current_resources"]["storage_entitlement"]
    assert entitlement["reason"] == "Administrative allowance note is private"
    assert entitlement["issued_by"] == str(DEV_ACTOR_ID)
    h.client.cookies.clear()
    h.client.cookies.update(recipient_cookies)
    detail = h.client.get(endpoint).json()
    public = detail["current_resources"]["storage_entitlement"]
    assert public["reason"] == "" and public["issued_by"] is None
    assert public["limit_bytes"] == entitlement["limit_bytes"]
    assert public == h.client.get(h.project_path + "/storage").json()["entitlement"]
    exported = h.client.get(endpoint + "/download")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        report = json.loads(archive.read("current-status.json"))
    assert report["current_resources"]["storage_entitlement"] == public
    assert b"Administrative allowance note is private" not in exported.content
