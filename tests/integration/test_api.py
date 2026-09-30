from typing import Any

import pytest
from fastapi.testclient import TestClient

from simon.api.app import AppContainer, create_app
from simon.config import Settings


def test_health_does_not_require_authentication(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}


def test_home_dashboard_is_the_entry_page(client: TestClient) -> None:
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/home"
    dashboard = client.get("/home")
    assert dashboard.status_code == 200
    assert "Your spaces" in dashboard.text
    assert '/assets/dashboard.js' in dashboard.text
    assert 'href="/chat"' in dashboard.text


def test_main_navigation_includes_displays_on_every_page(client: TestClient) -> None:
    for path in ("/home", "/automations", "/displays", "/chat"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'href="/displays"' in response.text
        assert 'aria-label="Main navigation"' in response.text


def test_lighting_colors_are_selected_before_explicitly_set(client: TestClient) -> None:
    script = client.get("/assets/dashboard.js")
    assert script.status_code == 200
    assert "['Red', '#ff0000']" in script.text
    assert "['Green', '#00ff00']" in script.text
    assert "['Blue', '#0000ff']" in script.text
    assert "colorSet.onclick = async" in script.text
    assert "color.onchange = () => control" not in script.text


def test_home_dashboard_links_respect_public_path() -> None:
    settings = Settings(
        public_path="/simon", storage_backend="memory", model_provider="local",
        home_auto_discovery=False, power_monitoring_enabled=False,
    )
    with TestClient(
        create_app(AppContainer(settings=settings)), base_url="http://localhost:8000"
    ) as client:
        assert client.get("/simon/", follow_redirects=False).headers["location"] == "/simon/home"
        page = client.get("/simon/home")
        assert page.status_code == 200
        assert 'src="/simon/assets/dashboard.js"' in page.text
        assert 'href="/simon/chat"' in page.text


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

    from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership

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
