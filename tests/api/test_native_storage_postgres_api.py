"""Cross-process storage admission and filesystem recovery using the real database."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from simon.domain.errors import StorageQuotaExceededError
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import ActorContext, Channel
from simon.domain.native_intake import SourceUpload
from simon.services.identity import ROLE_SCOPES
from tests.api.test_native_project_postgres_api import create_board, login_owner
from tests.api.test_native_project_postgres_api import postgres_native_api as postgres_native_api
from tests.api.test_native_storage_api import entitlement, upload_body

pytestmark = pytest.mark.postgres


def initialize(client, headers):
    project, _, path = create_board(client, headers)
    response = client.put(
        path + "/intake",
        headers=headers,
        json={"expected_version": 0, "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    return project, path


def test_source_reservation_survives_failed_commit_restart_and_downgrade(
    postgres_native_api, monkeypatch
):
    with postgres_native_api() as (client, container):
        headers = login_owner(client)
        project, path = initialize(client, headers)
        assert (
            client.post(
                path + "/storage/entitlement", headers=headers, json=entitlement(limit=6)
            ).status_code
            == 200
        )
        body = upload_body()

        def fail_after_disk(*args):
            raise RuntimeError("injected transaction failure after filesystem write")

        with monkeypatch.context() as failure:
            failure.setattr(container.native_intake, "_touch", fail_after_disk)
            with pytest.raises(RuntimeError, match="after filesystem"):
                client.post(path + "/intake/sources", headers=headers, json=body)
        value = client.get(path + "/storage").json()
        assert value["reserved_bytes"] == value["used_bytes"] == 6
        assert value["retained_bytes"] == 0 and value["reservation_count"] == 1
        assert not container.store.native_intake_sources(DEV_WORKSPACE_ID, UUID(project["id"]))
        assert (
            client.post(
                path + "/storage/entitlement", headers=headers, json=entitlement(1, 0)
            ).status_code
            == 200
        )
        cookies = dict(client.cookies)
    with postgres_native_api(cookies=cookies) as (client, container):
        assert client.get(path + "/storage").json()["reserved_bytes"] == 6
        completed = client.post(path + "/intake/sources", headers=headers, json=body)
        assert completed.status_code == 201, completed.text
        value = client.get(path + "/storage").json()
        assert value["retained_bytes"] == value["overage_bytes"] == 6
        assert value["reserved_bytes"] == 0 and value["reservation_count"] == 0
        assert (
            client.get(path + f"/intake/sources/{completed.json()['id']}/content").content == b"abc"
        )
        assert (
            client.post(path + "/intake/sources", headers=headers, json=body).json()
            == completed.json()
        )


def test_separate_services_compete_atomically_for_one_workspace_allowance(postgres_native_api):
    with postgres_native_api() as (client, first), postgres_native_api() as (_, second):
        headers = login_owner(client)
        project, path = initialize(client, headers)
        response = client.post(
            "/v2/projects",
            headers=headers,
            json={
                "name": "Second project",
                "objective": "Shared workspace storage",
                "idempotency_key": str(uuid4()),
            },
        )
        assert response.status_code == 201, response.text
        other = response.json()
        assert (
            client.put(
                f"/v2/projects/{other['id']}/intake",
                headers=headers,
                json={"expected_version": 0, "idempotency_key": str(uuid4())},
            ).status_code
            == 200
        )
        assert (
            client.post(
                path + "/storage/entitlement", headers=headers, json=entitlement(limit=6)
            ).status_code
            == 200
        )
        actor = ActorContext(
            actor_id=DEV_ACTOR_ID,
            workspace_id=DEV_WORKSPACE_ID,
            channel=Channel.API,
            scopes=ROLE_SCOPES["owner"],
        )
        barrier = Barrier(2)

        def upload(service, project_id):
            barrier.wait(timeout=10)
            try:
                service.upload(actor, project_id, SourceUpload.model_validate(upload_body()))
                return "retained"
            except StorageQuotaExceededError:
                return "denied"

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(upload, first.native_intake, UUID(project["id"]))
            two = pool.submit(upload, second.native_intake, UUID(other["id"]))
            assert sorted([one.result(timeout=30), two.result(timeout=30)]) == [
                "denied",
                "retained",
            ]
        assert first.store.storage_usage(DEV_WORKSPACE_ID) == {
            "reserved_bytes": 0,
            "retained_bytes": 6,
            "used_bytes": 6,
            "reservation_count": 0,
        }
