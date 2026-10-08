"""Database-backed encrypted enrollment, durable uncertainty and shared admission races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_models import ModelUsage
from simon.services.identity import ROLE_SCOPES
from tests.api.test_native_models_api import (
    configure_model_http,
    enroll_model,
    grant_budget,
    probe_model,
    update_policy,
)
from tests.api.test_native_project_api import create_project
from tests.api.test_native_project_postgres_api import (
    login_owner,
)
from tests.api.test_native_project_postgres_api import (
    postgres_native_api as postgres_native_api,
)

pytestmark = pytest.mark.postgres


def test_project_models_keys_policies_and_usage_survive_restart_without_repeated_probe(
    postgres_native_api,
):
    with postgres_native_api() as (client, container):
        headers = login_owner(client)
        state = configure_model_http(container, keyed=True, paid=True, preserve_cipher=True)
        project = create_project(client, headers)
        path = f"/v2/projects/{project['id']}/models"
        model, enrollment = enroll_model(
            client, path, headers, credential="synthetic-durable-project-key"
        )
        grant_budget(client, path, headers, workspace=True)
        grant_budget(client, path, headers, cloud=True)
        checked, probe = probe_model(client, path, headers, model)
        assert checked["ready"] and len(state.calls) == 1
        update_policy(client, path, headers, planning_model_id=model["id"])
        credential = container.store.project_model_credential(
            DEV_WORKSPACE_ID, UUID(project["id"]), UUID(model["id"]), 1
        )
        assert "synthetic-durable-project-key" not in credential.model_dump_json()
        assert container.settings.integration_key_file.is_file()
        key_bytes = container.settings.integration_key_file.read_bytes()
        before = client.get(path).json()
        usage_before = client.get(path + "/usage").json()
        cookies = dict(client.cookies)
        audit_before = [event.id for event in container.store.audit_events(DEV_WORKSPACE_ID)]

    with postgres_native_api(cookies=cookies) as (client, container):
        state = configure_model_http(container, keyed=True, paid=True, preserve_cipher=True)
        assert client.get(path).json() == before
        assert client.get(path + "/usage").json() == usage_before
        assert client.post(path + "/connections", headers=headers, json=enrollment).json() == model
        assert client.get(path + "/operations/" + enrollment["idempotency_key"]).json() == model
        replay = client.post(
            path + f"/connections/{model['id']}/probe", headers=headers, json=probe
        )
        assert replay.status_code == 200 and replay.json()["ready"]
        assert state.calls == []
        assert container.settings.integration_key_file.read_bytes() == key_bytes
        assert [
            event.id for event in container.store.audit_events(DEV_WORKSPACE_ID)
        ] == audit_before
        # A new deliberate check proves restored ciphertext is usable with the restored key.
        updated, _ = probe_model(client, path, headers, replay.json())
        assert updated["ready"] and len(state.calls) == 1
        assert state.calls[0].headers["Authorization"] == "Bearer synthetic-durable-project-key"
        assert client.get(path).json()["project_totals"]["charged_lifetime"] == 240
        assert client.get(path + "/usage?limit=1").json()["has_more"]


def test_uncertain_probe_and_reservation_remain_after_restart_without_redispatch(
    postgres_native_api,
):
    with postgres_native_api() as (client, container):
        headers = login_owner(client)
        state = configure_model_http(container, keyed=True, paid=True, preserve_cipher=True)
        project = create_project(client, headers)
        path = f"/v2/projects/{project['id']}/models"
        model, _ = enroll_model(client, path, headers, credential="synthetic-uncertain-key")
        grant_budget(client, path, headers, workspace=True)
        grant_budget(client, path, headers, cloud=True)

        def timeout(request):
            raise httpx.ReadTimeout("Synthetic response loss", request=request)

        state.hook = timeout
        failed, command = probe_model(client, path, headers, model)
        assert failed["qualification_status"] == "failed"
        before = client.get(path + "/usage").json()
        assert len(before["items"]) == 1
        liability = before["items"][0]
        assert liability["status"] == "unknown" and liability["held_microusd"] > 0
        cookies = dict(client.cookies)

    with postgres_native_api(cookies=cookies) as (client, container):
        state = configure_model_http(container, keyed=True, paid=True, preserve_cipher=True)
        replay = client.post(
            path + f"/connections/{model['id']}/probe", headers=headers, json=command
        )
        assert replay.status_code == 200 and not replay.json()["ready"]
        assert state.calls == []
        assert client.get(path + "/usage").json() == before
        assert (
            client.get(path).json()["workspace_totals"]["held_microusd"]
            == liability["held_microusd"]
        )
        assert not client.get(path).json()["routing"]["ready"]


def test_two_postgres_apps_share_one_atomic_workspace_reservation_ceiling(postgres_native_api):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )
    with (
        postgres_native_api() as (first_client, first_container),
        postgres_native_api() as (second_client, second_container),
    ):
        first_headers, second_headers = login_owner(first_client), login_owner(second_client)
        configure_model_http(first_container, paid=True)
        configure_model_http(second_container, paid=True)
        candidates = []
        for index, (client, container, headers) in enumerate(
            (
                (first_client, first_container, first_headers),
                (second_client, second_container, second_headers),
            )
        ):
            project = create_project(client, headers, key=f"concurrent-model-project-{index}")
            path = f"/v2/projects/{project['id']}/models"
            model, _ = enroll_model(client, path, headers)
            if index == 0:
                grant_budget(client, path, headers, workspace=True)
            grant_budget(client, path, headers)
            ready, _ = probe_model(client, path, headers, model)
            binding = container.project_models.resolve(
                actor, UUID(project["id"]), UUID(ready["id"])
            )
            now = utc_now()
            usage = ModelUsage(
                workspace_id=DEV_WORKSPACE_ID,
                project_id=UUID(project["id"]),
                operation_id=uuid4(),
                phase="generation",
                requested_by=DEV_ACTOR_ID,
                model_id=binding.model.id,
                model_version=binding.model.version,
                credential_revision=0,
                template_id=binding.model.template_id,
                model=binding.endpoint.model,
                endpoint_fingerprint=binding.fingerprint,
                endpoint_snapshot=binding.endpoint.model_dump(mode="json"),
                input_rate=Decimal(1),
                output_rate=Decimal(2),
                reserved_microusd=700,
                held_microusd=700,
                started_at=now,
                deadline_at=now + timedelta(minutes=3),
            )
            candidates.append((container, usage))
        grant_budget(first_client, path, first_headers, workspace=True, amount=1240)
        rendezvous = Barrier(2)

        def reserve(candidate):
            container, usage = candidate
            rendezvous.wait(timeout=10)
            try:
                return container.model_usage.reserve(
                    actor, usage.project_id, usage.operation_id, (usage,)
                )
            except InvalidTransitionError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, candidates))
        winners = [result for result in results if result]
        assert len(winners) == 1
        for container, usage in candidates:
            totals = container.store.model_usage_totals(DEV_WORKSPACE_ID, None, utc_now())
            assert (
                totals.charged_lifetime == 240
                and totals.held_microusd == 700
                and totals.active_calls == 1
            )
            admitted = container.store.model_usage_for_operation(
                DEV_WORKSPACE_ID, usage.project_id, usage.operation_id
            )
            assert admitted == ((usage,) if usage.id == winners[0][0].id else ())
