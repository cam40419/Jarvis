"""Delivery files, authority snapshots and receipts survive real database app restarts."""

import base64
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import utc_now
from simon.domain.native_intake import IntakeSource
from simon.services.identity import IDENTITY_LOCK
from tests.api.test_native_artifacts_api import head
from tests.api.test_native_project_postgres_api import create_board, login_member, login_owner
from tests.api.test_native_project_postgres_api import postgres_native_api as postgres_native_api

pytestmark = pytest.mark.postgres


def no_model_calls(container):
    def unexpected(request):
        pytest.fail("Human-only artifact and packet commands must not dispatch model calls.")

    container.project_models.transport = httpx.MockTransport(unexpected)


def upload_file(client, headers, path, task, raw, *, artifact=None, filename="editable.txt"):
    response = client.post(
        path + "/artifacts/upload",
        headers=headers,
        json={
            "task_id": task["id"],
            "expected_task_version": task["version"],
            "expected_version": artifact["version"] if artifact else 0,
            "artifact_id": artifact["id"] if artifact else None,
            "filename": filename,
            "media_type": "text/plain",
            "content_base64": base64.b64encode(raw).decode("ascii"),
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def setup_delivery(client, container, *, evidence=False):
    no_model_calls(container)
    headers = login_owner(client)
    project, task, path = create_board(client, headers)
    source = None
    if evidence:
        source = IntakeSource(
            workspace_id=DEV_WORKSPACE_ID,
            project_id=UUID(project["id"]),
            source_key="requirements.txt",
            filename="requirements.txt",
            media_type="text/plain",
            revision=1,
            sha256=hashlib.sha256(b"Original requirement.").hexdigest(),
            size_bytes=len(b"Original requirement."),
            text="Original requirement.",
            extraction_status="text",
            created_by=DEV_ACTOR_ID,
        )
        container.store.insert_native_intake_source(source)
    policy = client.put(
        path + "/artifacts/policy",
        headers=headers,
        json={
            "expected_version": 0,
            "require_agent_review": False,
            "idempotency_key": "postgres-human-only-review-policy",
        },
    )
    assert policy.status_code == 200, policy.text
    raw = b"Editable original with its full requirement and source evidence.\n"
    artifact = upload_file(client, headers, path, task, raw)
    revision = head(artifact)
    approval = client.post(
        path + f"/artifacts/{artifact['artifact']['id']}/decisions",
        headers=headers,
        json={
            "expected_version": artifact["artifact"]["version"],
            "expected_task_version": artifact["task"]["version"],
            "expected_policy_version": policy.json()["version"],
            "revision_id": revision["id"],
            "sha256": revision["sha256"],
            "decision": "approved",
            "idempotency_key": "postgres-human-file-approval",
        },
    )
    assert approval.status_code == 200, approval.text
    artifact = approval.json()
    assert artifact["status"] == "accepted"
    body = {
        "title": "Durable editable delivery",
        "components": [
            {
                "artifact_id": artifact["artifact"]["id"],
                "revision_id": revision["id"],
                "sha256": revision["sha256"],
                "role": "source",
            }
        ],
        "limitations": ["Native application rendering has not been validated."],
        "idempotency_key": "postgres-delivery-capture",
    }
    endpoint = path + "/delivery-packets"
    captured = client.post(endpoint, headers=headers, json=body)
    assert captured.status_code == 201, captured.text
    value = captured.json()
    packet_path = endpoint + "/" + value["packet"]["id"]
    downloaded = client.get(packet_path + "/download")
    assert downloaded.status_code == 200, downloaded.text
    return SimpleNamespace(
        headers=headers,
        project=project,
        path=path,
        artifact=artifact,
        raw=raw,
        source=source,
        body=body,
        endpoint=endpoint,
        packet_path=packet_path,
        value=value,
        downloaded=downloaded.content,
    )


def archive_files(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {path: archive.read(path) for path in archive.namelist()}


def test_packet_restart_preserves_exact_files_receipts_and_human_acceptance_authority(
    postgres_native_api,
):
    with postgres_native_api() as (client, container):
        h = setup_delivery(client, container, evidence=True)
        decision = h.artifact["decisions"][0]
        authority = decision["authority_snapshot"]
        assert authority["project_version"] == h.project["version"]
        assert authority["owner_authority"]["actor_id"] == str(DEV_ACTOR_ID)
        assert authority["owner_authority"]["workspace_role"] == "owner"
        assert authority["source_authority"]["source_ids"] == [str(h.source.id)]
        cookies = dict(client.cookies)
        audit_ids = [event.id for event in container.store.audit_events(DEV_WORKSPACE_ID)]

    with postgres_native_api(cookies=cookies) as (client, container):
        no_model_calls(container)
        assert client.get("/auth/session").status_code == 200
        value = client.get(h.packet_path)
        assert value.status_code == 200, value.text
        assert value.json() == h.value
        artifact = client.get(h.path + "/artifacts/" + h.artifact["artifact"]["id"])
        assert artifact.status_code == 200 and artifact.json()["status"] == "accepted"
        assert artifact.json()["decisions"][0]["authority_snapshot"] == authority
        stored = container.store.artifact_decisions(
            DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(head(h.artifact)["id"])
        )[0]
        assert stored.authority_snapshot == authority
        receipt = client.get(h.endpoint + "/operations/" + h.body["idempotency_key"])
        assert receipt.status_code == 200
        assert receipt.json() == {"packet_id": h.value["packet"]["id"]}
        replay = client.post(h.endpoint, headers=h.headers, json=h.body)
        assert replay.status_code == 201 and replay.json() == h.value
        exported = client.get(h.packet_path + "/download")
        assert exported.status_code == 200 and exported.content == h.downloaded
        files = archive_files(exported.content)
        assert json.loads(files["manifest.json"]) == h.value["packet"]["manifest"]
        export_path = next(iter(json.loads(files["export-paths.json"]).values()))
        assert files[export_path] == h.raw
        assert len(container.store.delivery_packets(DEV_WORKSPACE_ID, UUID(h.project["id"]))) == 1
        assert [event.id for event in container.store.audit_events(DEV_WORKSPACE_ID)] == audit_ids

        replacement = h.source.model_copy(
            update={
                "id": uuid4(),
                "revision": 2,
                "text": "Changed requirement.",
                "sha256": hashlib.sha256(b"Changed requirement.").hexdigest(),
                "size_bytes": len(b"Changed requirement."),
                "created_at": utc_now(),
            }
        )
        container.store.insert_native_intake_source(replacement)
        artifact = client.get(h.path + "/artifacts/" + h.artifact["artifact"]["id"]).json()
        assert artifact["status"] != "accepted"
        assert artifact["decisions"][0]["authority_snapshot"] == authority
        assert client.get(h.packet_path).json()["applicability"]["current"] is False


def test_new_head_makes_old_packet_stale_without_changing_its_manifest_or_originals(
    postgres_native_api,
):
    with postgres_native_api() as (client, container):
        h = setup_delivery(client, container)
        cookies = dict(client.cookies)
    with postgres_native_api(cookies=cookies) as (client, container):
        no_model_calls(container)
        new = upload_file(
            client,
            h.headers,
            h.path,
            h.artifact["task"],
            b"New editable content requiring fresh approval.\n",
            artifact=h.artifact["artifact"],
        )
        assert head(new)["id"] != head(h.artifact)["id"]
        old = client.get(h.packet_path)
        assert old.status_code == 200, old.text
        assert old.json()["packet"] == h.value["packet"]
        assert old.json()["applicability"]["current"] is False
        assert old.json()["applicability"]["components_accepted"] is False
        replay = client.post(h.endpoint, headers=h.headers, json=h.body)
        assert replay.status_code == 201 and replay.json()["packet"] == h.value["packet"]
        exported = client.get(h.packet_path + "/download")
        assert exported.status_code == 200
        before, after = archive_files(h.downloaded), archive_files(exported.content)
        assert {path: raw for path, raw in before.items() if path != "current-status.json"} == {
            path: raw for path, raw in after.items() if path != "current-status.json"
        }
        assert json.loads(after["current-status.json"])["applicability"]["current"] is False


def test_membership_loss_on_another_connection_blocks_packet_export_after_restart(
    postgres_native_api,
):
    with postgres_native_api() as (owner, container):
        h = setup_delivery(owner, container)
        member_id = uuid4()
        with postgres_native_api() as (member, second):
            login_member(member, second, member_id, DEV_WORKSPACE_ID)
            joined = owner.put(
                h.path + "/members",
                headers=h.headers,
                json={
                    "actor_id": str(member_id),
                    "expected_version": h.project["version"],
                    "idempotency_key": "postgres-delivery-member-share",
                },
            )
            assert joined.status_code == 200, joined.text
            assert member.get(h.packet_path + "/download").status_code == 200
            cookies = dict(member.cookies)
        with postgres_native_api(cookies=cookies) as (member, second):
            no_model_calls(second)
            assert member.get(h.packet_path + "/download").status_code == 200
            with container.store.transaction(IDENTITY_LOCK):
                container.store.delete_native_project_member(
                    DEV_WORKSPACE_ID, UUID(h.project["id"]), member_id
                )
            for path in (h.endpoint, h.packet_path, h.packet_path + "/download"):
                assert member.get(path).status_code == 404
            with container.store.transaction(IDENTITY_LOCK):
                container.store.delete_membership(member_id, DEV_WORKSPACE_ID)
            assert member.get(h.packet_path + "/download").status_code == 403
            assert owner.get(h.packet_path + "/download").status_code == 200


def test_historical_decision_missing_additive_authority_defaults_remains_readable_and_stale(
    postgres_native_api,
):
    with postgres_native_api() as (client, container):
        h = setup_delivery(client, container)
        decision = container.store.artifact_decisions(
            DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(head(h.artifact)["id"])
        )[0]
        historical = decision.model_copy(update={"id": uuid4(), "created_at": utc_now()})
        snapshot = historical.model_dump(mode="json", exclude={"authority_snapshot"})
        with container.store.transaction(DEV_WORKSPACE_ID):
            container.store.connection.execute(
                "INSERT INTO native_artifact_decisions "
                "(id,workspace_id,project_id,artifact_id,revision_id,sha256,review_id,"
                "repair_task_id,decided_by,decision,created_at,snapshot) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    historical.id,
                    historical.workspace_id,
                    historical.project_id,
                    historical.artifact_id,
                    historical.revision_id,
                    historical.sha256,
                    historical.review_id,
                    historical.repair_task_id,
                    historical.decided_by,
                    historical.decision,
                    historical.created_at,
                    Jsonb(snapshot),
                ),
            )
        cookies = dict(client.cookies)
    with postgres_native_api(cookies=cookies) as (client, container):
        no_model_calls(container)
        result = client.get(h.path + "/artifacts/" + h.artifact["artifact"]["id"])
        assert result.status_code == 200, result.text
        legacy = next(
            item for item in result.json()["decisions"] if item["id"] == str(historical.id)
        )
        assert legacy["authority_snapshot"] == {}
        assert result.json()["status"] != "accepted"
        stored = container.store.artifact_decisions(
            DEV_WORKSPACE_ID, UUID(h.project["id"]), UUID(head(h.artifact)["id"])
        )
        assert next(item for item in stored if item.id == historical.id).authority_snapshot == {}
