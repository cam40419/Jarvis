"""Package approval and immutable ZIP evidence survive real database app restarts."""

import pytest

from simon.domain.identity import DEV_WORKSPACE_ID
from tests.api.test_native_acceptance_api import complete, setup_api
from tests.api.test_native_project_postgres_api import login_owner
from tests.api.test_native_project_postgres_api import postgres_native_api as postgres_native_api

pytestmark = pytest.mark.postgres


def test_package_restart_replays_profile_validation_acceptance_and_exact_zip(postgres_native_api):
    with postgres_native_api() as (client, container):
        headers = login_owner(client)
        h = setup_api(client, container, headers)
        accepted, validation, approval = complete(h)
        usage = container.store.storage_usage(DEV_WORKSPACE_ID)
        download = client.get(h.project_path + "/delivery-packets/" + h.packet["id"] + "/download")
        assert download.status_code == 200, download.text
        original_zip = download.content
        cookies = dict(client.cookies)
    with postgres_native_api(cookies=cookies) as (client, container):
        h.client, h.container = client, container
        for suffix, command in (
            ("/profile", h.assignment),
            ("/validate", validation),
            ("/decisions", approval),
        ):
            operation = client.put if suffix == "/profile" else client.post
            response = operation(h.acceptance_path + suffix, headers=headers, json=command)
            assert response.status_code == 201, response.text
            assert response.json()["latest_acceptance"]["id"] == accepted["latest_acceptance"]["id"]
        assert container.store.storage_usage(DEV_WORKSPACE_ID) == usage
        assert client.get(h.acceptance_path).json()["applicability"]["accepted"]
        download = client.get(h.project_path + "/delivery-packets/" + h.packet["id"] + "/download")
        assert download.status_code == 200 and download.content == original_zip


def test_package_database_membership_revocation_fences_reports_receipts_and_approval(
    postgres_native_api,
):
    from simon.domain.identity import DEV_ACTOR_ID
    from simon.services.identity import IDENTITY_LOCK

    with postgres_native_api() as (client, container):
        headers = login_owner(client)
        h = setup_api(client, container, headers)
        complete(h)
        with (
            postgres_native_api() as (_, other),
            other.store.transaction(IDENTITY_LOCK),
            other.store.transaction(DEV_WORKSPACE_ID),
        ):
            other.store.delete_membership(DEV_ACTOR_ID, DEV_WORKSPACE_ID)
            assert other.store.memberships(DEV_ACTOR_ID) == ()
        assert client.get(h.acceptance_path).status_code == 403
        assert (
            client.get(
                h.acceptance_path + "/operations/" + h.assignment["idempotency_key"]
            ).status_code
            == 403
        )
