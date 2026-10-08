"""Native HTTP acceptance against independent PostgreSQL connections and app instances."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from simon.api.auth import session_cookie
from simon.config import Settings
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID, Membership
from simon.services.identity import IDENTITY_LOCK, csrf_token

pytestmark = pytest.mark.postgres

ORIGIN = "http://localhost:8000"
LOGIN_SECRET = "postgres-native-api-development-secret"
PROJECT_COMMAND = {
    "name": "Native database pilot",
    "objective": "Coordinate source review and approved work.",
    "idempotency_key": "postgres-project-create",
}
TASK_COMMAND = {"title": "Review project sources", "idempotency_key": "postgres-task-create"}


@pytest.fixture
def postgres_native_api(postgres_url, tmp_path):
    """The shared fixture supplies a newly migrated, disposable schema for this test."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from simon.adapters.postgres import PostgresStore
    from simon.api.app import AppContainer, create_app

    # Preserve the isolated schema while exercising non-UTC database reads on every host.
    options = conninfo_to_dict(postgres_url).get("options", "")
    postgres_url = make_conninfo(
        postgres_url, options=f"{options} -c timezone=America/New_York".strip()
    )
    bootstrap = PostgresStore(postgres_url)
    try:
        with bootstrap.transaction():
            configured = bootstrap.connection.execute(
                "SELECT current_setting('TimeZone') AS session_timezone"
            ).fetchone()
            assert configured == {"session_timezone": "America/New_York"}
        bootstrap.put_membership(
            Membership(actor_id=DEV_ACTOR_ID, workspace_id=DEV_WORKSPACE_ID, role="owner")
        )
    finally:
        bootstrap.close()

    settings = Settings(
        _env_file=None,
        environment="test",
        storage_backend="postgres",
        database_url=SecretStr(postgres_url),
        public_origin=ORIGIN,
        dev_login_enabled=True,
        dev_login_token=SecretStr(LOGIN_SECRET),
        model_provider="local",
        openai_api_key=None,
        external_providers_file=None,
        local_files_enabled=False,
        local_files_dir=tmp_path / "files",
        integration_key_file=tmp_path / "credentials.key",
        voice_enabled=False,
    )

    @contextmanager
    def open_api(*, cookies=None):
        store = PostgresStore(postgres_url, pool_size=4)
        try:
            container = AppContainer(store=store, settings=settings)
            with TestClient(create_app(container), base_url=ORIGIN, cookies=cookies) as client:
                yield client, container
        finally:
            store.close()

    return open_api


def login_owner(client):
    response = client.post(
        "/auth/dev-login", headers={"Origin": ORIGIN}, json={"token": LOGIN_SECRET}
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def login_member(client, container, actor_id, workspace_id, *, role="member"):
    container.store.put_membership(
        Membership(actor_id=actor_id, workspace_id=workspace_id, role=role)
    )
    token, _ = container.identity._issue(actor_id, workspace_id, "development")
    client.cookies.clear()
    client.cookies.set(session_cookie(container.identity), token)
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(token)}


def create_board(client, headers):
    created = client.post("/v2/projects", headers=headers, json=PROJECT_COMMAND)
    assert created.status_code == 201, created.text
    project = created.json()
    path = f"/v2/projects/{project['id']}"
    submitted = client.post(path + "/tasks", headers=headers, json=TASK_COMMAND)
    assert submitted.status_code == 201, submitted.text
    return project, submitted.json(), path


def native_events(store):
    return [
        event
        for event in store.audit_events(DEV_WORKSPACE_ID)
        if event.event_type.startswith("native.")
    ]


def test_native_http_retries_and_sessions_survive_recreated_app(postgres_native_api):
    with postgres_native_api() as (client, container):
        headers = login_owner(client)
        project, task, path = create_board(client, headers)
        task_path = path + f"/tasks/{task['id']}"
        update = {
            "title": "Compare source evidence",
            "description": "Record the source behind each conclusion.",
            "status": "todo",
            "assignment": {"kind": "pool"},
            "expected_version": task["version"],
            "idempotency_key": "postgres-task-update",
        }
        saved = client.put(task_path, headers=headers, json=update)
        assert saved.status_code == 200, saved.text
        cookies = dict(client.cookies)
        events = native_events(container.store)
        assert len(events) == 3
        event_ids = [event.id for event in events]

    with postgres_native_api(cookies=cookies) as (restarted, container):
        assert restarted.get("/auth/session").status_code == 200
        assert restarted.get(path).json() == project
        assert restarted.get(task_path).json() == saved.json()
        assert (
            restarted.post("/v2/projects", headers=headers, json=PROJECT_COMMAND).json() == project
        )
        assert restarted.post(path + "/tasks", headers=headers, json=TASK_COMMAND).json() == task
        replayed = restarted.put(task_path, headers=headers, json=update)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == saved.json()
        assert [event.id for event in native_events(container.store)] == event_ids
        conflicting = restarted.post(
            path + "/tasks", headers=headers, json={**TASK_COMMAND, "title": "Different work"}
        )
        assert conflicting.status_code == 409
        assert restarted.get(path + "/tasks").json() == [saved.json()]
        assert [event.id for event in native_events(container.store)] == event_ids
        outbox_ids = {event.id for event in container.store.outbox_events()}
        assert set(event_ids) <= outbox_ids


def test_native_http_tenant_isolation_persists_across_apps(postgres_native_api):
    with postgres_native_api() as (owner_client, owner_container):
        headers = login_owner(owner_client)
        project, task, path = create_board(owner_client, headers)
        with postgres_native_api() as (other_client, other_container):
            other_headers = login_member(
                other_client, other_container, uuid4(), uuid4(), role="owner"
            )
            foreign, foreign_task, foreign_path = create_board(other_client, other_headers)
            assert foreign["id"] != project["id"]
            assert foreign_task["id"] != task["id"]
            assert other_client.get("/v2/projects").json() == [foreign]
            assert owner_client.get("/v2/projects").json() == [project]
            for suffix in ("", "/members", "/tasks", f"/tasks/{task['id']}"):
                assert other_client.get(path + suffix).status_code == 404
            assert owner_client.get(foreign_path).status_code == 404
            assert (
                other_client.post(
                    path + "/tasks", headers=other_headers, json=TASK_COMMAND
                ).status_code
                == 404
            )
            claim = other_client.post(
                path + f"/tasks/{task['id']}/claim",
                headers=other_headers,
                json={"expected_version": 1, "idempotency_key": "foreign-task-claim"},
            )
            assert claim.status_code == 404
            assert owner_client.get(path + f"/tasks/{task['id']}").json() == task
            assert len(native_events(owner_container.store)) == 2


def test_native_http_access_rechecks_roles_changed_by_another_connection(postgres_native_api):
    with postgres_native_api() as (owner_client, owner_container):
        headers = login_owner(owner_client)
        project, task, path = create_board(owner_client, headers)
        member_id = uuid4()
        with postgres_native_api() as (member_client, member_container):
            member_headers = login_member(
                member_client, member_container, member_id, DEV_WORKSPACE_ID
            )
            joined = owner_client.put(
                path + "/members",
                headers=headers,
                json={
                    "actor_id": str(member_id),
                    "expected_version": project["version"],
                    "idempotency_key": "postgres-member-share",
                },
            )
            assert joined.status_code == 200, joined.text
            assert member_client.get(path + "/tasks").json() == [task]
            member_command = {"title": "Member draft", "idempotency_key": "member-draft-create"}
            created = member_client.post(
                path + "/tasks", headers=member_headers, json=member_command
            )
            assert created.status_code == 201, created.text
            with owner_container.store.transaction(IDENTITY_LOCK):
                owner_container.store.put_membership(
                    Membership(actor_id=member_id, workspace_id=DEV_WORKSPACE_ID, role="guest")
                )
            assert member_client.get(path).status_code == 200
            assert member_client.get("/auth/session").json()["scopes"] == ["system:read"]
            assert (
                member_client.post(
                    path + "/tasks", headers=member_headers, json=member_command
                ).status_code
                == 403
            )
            with owner_container.store.transaction(IDENTITY_LOCK):
                owner_container.store.delete_membership(member_id, DEV_WORKSPACE_ID)
            assert member_client.get(path).status_code == 403
            assert member_client.get("/v2/projects").status_code == 403
            assert (
                member_client.post(
                    path + "/tasks", headers=member_headers, json=member_command
                ).status_code
                == 403
            )
            assert len(owner_client.get(path + "/tasks").json()) == 2


def test_two_postgres_apps_atomically_claim_one_pool_task(postgres_native_api):
    with postgres_native_api() as (owner_client, owner_container):
        owner_headers = login_owner(owner_client)
        project, task, path = create_board(owner_client, owner_headers)
        with postgres_native_api() as (member_client, member_container):
            member_id = uuid4()
            member_headers = login_member(
                member_client, member_container, member_id, DEV_WORKSPACE_ID
            )
            joined = owner_client.put(
                path + "/members",
                headers=owner_headers,
                json={
                    "actor_id": str(member_id),
                    "expected_version": project["version"],
                    "idempotency_key": "postgres-claim-member",
                },
            )
            assert joined.status_code == 200, joined.text
            before = [event.id for event in native_events(owner_container.store)]
            rendezvous = Barrier(2)
            claim_path = path + f"/tasks/{task['id']}/claim"

            def claim(candidate):
                client, headers, actor_id = candidate
                rendezvous.wait(timeout=10)
                return client.post(
                    claim_path,
                    headers=headers,
                    json={"expected_version": 1, "idempotency_key": f"claim-{actor_id}"},
                )

            candidates = [
                (owner_client, owner_headers, DEV_ACTOR_ID),
                (member_client, member_headers, member_id),
            ]
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, candidates))
            assert sorted(result.status_code for result in results) == [200, 409]
            winning_index = next(i for i, result in enumerate(results) if result.status_code == 200)
            winner = results[winning_index].json()
            assert winner["version"] == 2
            assert winner["status"] == "in_progress"
            assert winner["assignment"] == {
                "kind": "human",
                "actor_id": str(candidates[winning_index][2]),
                "agent_id": None,
            }
            assert owner_client.get(path + f"/tasks/{task['id']}").json() == winner
            assert member_client.get(path + f"/tasks/{task['id']}").json() == winner
            events = native_events(owner_container.store)
            assert len(events) == len(before) + 1
            assert events[-1].event_type == "native.task.claimed"
            winning_client, winning_headers, winning_actor = candidates[winning_index]
            replay = winning_client.post(
                claim_path,
                headers=winning_headers,
                json={"expected_version": 1, "idempotency_key": f"claim-{winning_actor}"},
            )
            assert replay.status_code == 200
            assert replay.json() == winner
            assert [event.id for event in native_events(owner_container.store)] == [
                event.id for event in events
            ]
