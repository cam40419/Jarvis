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
    denied = auth_headers | {"X-Scopes": ""}
    assert client.get("/v1/capabilities", headers=denied).json() == []


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
    assert len(container.store.audit_events()) == 1


def test_job_api_replays_same_job(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
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
        headers=auth_headers | {"X-Scopes": ""},
        json={
            "capability": "system.echo",
            "arguments": {"message": "hello"},
            "idempotency_key": "echo-request-1",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_production_refuses_development_header_authentication() -> None:
    with pytest.raises(RuntimeError, match="production authentication"):
        AppContainer(settings=Settings(environment="production"))
