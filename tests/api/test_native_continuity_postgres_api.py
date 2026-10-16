"""Actual PostgreSQL handoff evidence, admission and receipts survive app restart."""

import io
import json
import zipfile
from uuid import UUID

import pytest

from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import utc_now
from tests.api.test_native_delivery_postgres_api import no_model_calls, setup_delivery, upload_file
from tests.api.test_native_project_postgres_api import postgres_native_api as postgres_native_api

pytestmark = pytest.mark.postgres


def test_handoff_restart_exact_manifest_originals_durable_receipt_and_source_revocation(
    postgres_native_api,
):
    with postgres_native_api() as (client, container):
        h = setup_delivery(client, container, evidence=True)
        endpoint = h.path + "/handoffs"
        command = {
            "recipient_id": str(DEV_ACTOR_ID),
            "title": "Durable exact continuity",
            "idempotency_key": "postgres-continuity-capture",
        }
        created = client.post(endpoint, headers=h.headers, json=command)
        assert created.status_code == 201, created.text
        value = created.json()
        handoff_id = UUID(value["handoff"]["id"])
        charge = container.store.storage_charge(DEV_WORKSPACE_ID, "continuity", handoff_id)
        assert charge.state == "retained"
        detail_path = endpoint + "/" + str(handoff_id)
        cookies = dict(client.cookies)

    with postgres_native_api(cookies=cookies) as (client, container):
        no_model_calls(container)
        current = client.get(detail_path)
        assert current.status_code == 200, current.text
        assert current.json()["handoff"] == value["handoff"]
        replay = client.post(endpoint, headers=h.headers, json=command)
        assert replay.status_code == 201 and replay.json()["handoff"] == value["handoff"]
        assert container.store.storage_charge(DEV_WORKSPACE_ID, "continuity", handoff_id) == charge
        assert client.get(endpoint + "/operations/" + command["idempotency_key"]).json() == {
            "kind": "create",
            "id": str(handoff_id),
        }
        exported = client.get(detail_path + "/download")
        assert exported.status_code == 200, exported.text
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert json.loads(archive.read("manifest.json")) == value["handoff"]["manifest"]
            assert (
                archive.read(next(name for name in archive.namelist() if name.startswith("files/")))
                == h.raw
            )
        container.store.revoke_native_intake_source(
            DEV_WORKSPACE_ID, UUID(h.project["id"]), h.source.id, utc_now()
        )
        withheld = client.get(detail_path).json()
        assert not withheld["disclosure_allowed"] and "manifest" not in withheld["handoff"]
        assert client.get(detail_path + "/download").status_code == 403
        assert container.store.storage_charge(DEV_WORKSPACE_ID, "continuity", handoff_id) == charge


def test_new_output_head_marks_handoff_stale_without_rewriting_historical_manifest(
    postgres_native_api,
):
    with postgres_native_api() as (client, container):
        h = setup_delivery(client, container)
        endpoint = h.path + "/handoffs"
        response = client.post(
            endpoint,
            headers=h.headers,
            json={
                "recipient_id": str(DEV_ACTOR_ID),
                "title": "Original exact state",
                "idempotency_key": "postgres-original-handoff",
            },
        )
        assert response.status_code == 201, response.text
        original = response.json()["handoff"]
        task = client.get(h.path + "/tasks/" + h.artifact["artifact"]["task_id"]).json()
        upload_file(
            client,
            h.headers,
            h.path,
            task,
            b"Changed current editable candidate.",
            artifact=h.artifact["artifact"],
        )
        detail_path = endpoint + "/" + original["id"]
        status = client.get(detail_path)
        assert status.status_code == 200 and not status.json()["current"]
        assert status.json()["handoff"] == original
        cookies = dict(client.cookies)

    with postgres_native_api(cookies=cookies) as (client, container):
        no_model_calls(container)
        detail = client.get(detail_path).json()
        assert not detail["current"] and detail["handoff"] == original
        exported = client.get(detail_path + "/download")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert json.loads(archive.read("manifest.json")) == original["manifest"]
            assert not json.loads(archive.read("current-status.json"))["current"]
