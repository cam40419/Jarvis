from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import AppContainer
from jarvis.config import Settings


def test_health_does_not_require_authentication(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}


def test_capabilities_are_filtered_by_scope(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert len(client.get("/v1/capabilities", headers=auth_headers).json()) == 1
    # Self-asserted scope headers have no effect on database-derived permissions.
    spoofed = auth_headers | {"X-Scopes": ""}
    assert len(client.get("/v1/capabilities", headers=spoofed).json()) == 1


def test_echo_vertical_slice_is_idempotent_and_audited(
    client: TestClient, container: AppContainer, auth_headers: dict[str, str]
) -> None:
    body: dict[str, Any] = {
        "capability": "system.echo",
        "arguments": {"message": "hello"},
        "idempotency_key": "echo-request-1",
    }
    first = client.post("/v1/capabilities/invoke", headers=auth_headers, json=body)
    replay = client.post("/v1/capabilities/invoke", headers=auth_headers, json=body)
    assert first.status_code == 200
    assert first.json()["output"] == {"message": "hello"}
    assert replay.json()["replayed"] is True
    assert sum(e.event_type == "capability.invoked" for e in container.store.audit_events()) == 1


def test_job_api_replays_same_job(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = {"kind": "test.job", "input": {"value": 1}, "idempotency_key": "job-request-1"}
    first = client.post("/v1/jobs", headers=auth_headers, json=body)
    replay = client.post("/v1/jobs", headers=auth_headers, json=body)
    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json()["id"] == replay.json()["id"]


def test_api_returns_stable_domain_error_shape(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/v1/capabilities/invoke",
        headers=auth_headers | {"X-CSRF-Token": "wrong"},
        json={
            "capability": "system.echo",
            "arguments": {"message": "hello"},
            "idempotency_key": "echo-request-1",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_production_refuses_unsafe_authentication_configuration() -> None:
    with pytest.raises(ValueError, match="production requires"):
        Settings(environment="production")


def test_invalid_job_kind_is_a_client_error(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/v1/jobs",
        headers=auth_headers,
        json={
            "kind": "!",
            "input": {},
            "idempotency_key": "invalid-job-001",
        },
    )
    assert response.status_code == 422


def test_jobs_require_scopes_and_get_is_household_scoped(
    client: TestClient,
    container: AppContainer,
    auth_headers: dict[str, str],
) -> None:
    from uuid import uuid4

    from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership

    body = {"kind": "test.job", "input": {}, "idempotency_key": "scoped-job-001"}
    job = client.post("/v1/jobs", headers=auth_headers, json=body).json()
    path = f"/v1/jobs/{job['id']}"
    assert client.get(path, headers=auth_headers).json()["id"] == job["id"]
    container.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, household_id=DEV_HOUSEHOLD_ID, role="guest")
    )
    assert (
        client.post(
            "/v1/jobs", headers=auth_headers | {"X-Scopes": "jobs:write"}, json=body
        ).status_code
        == 403
    )
    assert client.get(path, headers=auth_headers | {"X-Scopes": "jobs:read"}).status_code == 403
    other = uuid4()
    container.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, household_id=other, role="owner")
    )
    switched = client.post(
        "/auth/household", headers=auth_headers, json={"household_id": str(other)}
    )
    assert switched.status_code == 200
    assert client.get(path).status_code == 404
