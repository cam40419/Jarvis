"""Real API connection lifecycle, safe public responses, and CSRF checks."""

import httpx
import pytest

from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID


def test_clickup_ui_backend_lifecycle(client, container, auth_headers, tmp_path):
    service = container.connected.integrations
    container.settings.integration_key_file = tmp_path / "credentials.key"

    def send(request):
        assert request.headers["Authorization"].startswith("pk_")
        assert request.url.path == "/api/v2/team"
        return httpx.Response(200, json={"teams": [{"id": "123", "name": "Company"}]})

    service.clickup.http.transport = httpx.MockTransport(send)
    path = "/v1/connections/integrations"
    assert client.post(path + "/clickup", json={"credential": "pk_test"}).status_code == 403
    response = client.post(
        path + "/clickup",
        headers=auth_headers,
        json={"credential": "pk_test", "name": "My ClickUp"},
    )
    assert response.status_code == 200
    record = response.json()
    assert "pk_test" not in response.text
    assert "encrypted_secret" not in response.text
    assert client.get(path).json() == [record]
    persisted = container.store.integration_connections(DEV_WORKSPACE_ID, DEV_ACTOR_ID)[0]
    assert "pk_test" not in persisted.model_dump_json()
    assert (
        client.post(
            path + "/clickup/" + record["id"], headers=auth_headers, json={"credential": "pk_new"}
        ).status_code
        == 200
    )
    assert client.delete(path + "/" + record["id"], headers=auth_headers).status_code == 204
    assert client.get(path).json() == []


def test_validation_never_echoes_secret(client, auth_headers):
    response = client.post(
        "/v1/connections/integrations/clickup",
        headers=auth_headers,
        json={"credential": "sensitive-invalid-credential", "unknown": "sensitive"},
    )
    assert response.status_code == 422
    assert "sensitive" not in response.text


def test_connection_tests_require_csrf_and_persist_safe_results(
    client, container, auth_headers, tmp_path
):
    service = container.connected.integrations
    container.settings.integration_key_file = tmp_path / "key"
    service.clickup.http.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"type": "folder"})
    )
    base = "/v1/connections/integrations"
    linked = client.post(
        base + "/box",
        headers=auth_headers,
        json={"credential": "secret-box-token", "root_folder_id": "0"},
    )
    assert linked.status_code == 200
    path = base + "/" + linked.json()["id"] + "/test"
    assert client.post(path, json={}).status_code == 403
    checked = client.post(path, headers=auth_headers, json={})
    assert checked.status_code == 200 and checked.json()["status"] == "passed"
    assert "secret-box-token" not in checked.text
    assert client.get(base).json()[0]["settings"]["connection_test"] == checked.json()
    service.clickup.http.transport = httpx.MockTransport(lambda request: httpx.Response(401))
    failed = client.post(path, headers=auth_headers, json={})
    assert failed.json()["status"] == "failed" and "secret-box-token" not in failed.text


def test_google_app_setup_and_home_are_live(client, container, auth_headers, tmp_path):
    container.settings.integration_key_file = tmp_path / "credentials.key"
    path = "/v1/connections/integrations"
    response = client.post(
        path + "/google_app",
        headers=auth_headers,
        json={"client_id": "123-test.apps.googleusercontent.com", "credential": "client-secret"},
    )
    assert response.status_code == 200
    assert client.get("/v1/connections/google").json()["configured"]
    assert container.connected.settings.google_client_secret.get_secret_value() == "client-secret"
    response = client.post(
        path + "/home",
        headers=auth_headers,
        json={"endpoint": "http://localhost:8001", "credential": "home-secret"},
    )
    assert response.status_code == 200
    assert client.get("/v1/connections/home").json() == {
        "configured": True,
        "url": "http://localhost:8001",
    }
    assert (
        client.delete(path + "/" + response.json()["id"], headers=auth_headers).status_code == 204
    )
    assert not client.get("/v1/connections/home").json()["configured"]


def test_github_ui_connection_enables_owned_tools_and_disconnect_revokes_them(
    client, auth_headers, container, tmp_path
):
    from simon.adapters.github_tools import github_tool_definitions
    from simon.domain.models import ActorContext, Channel
    from simon.domain.tool_catalog import ToolCatalogError

    service = container.connected.integrations
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    definitions = {tool.id: tool for tool in github_tool_definitions(enabled=True)}

    def available(tool_id, denied=None):
        definition = definitions[tool_id].model_copy(
            update={"settings": {"ui_managed": True, "network": True}}
        )
        if denied is not None:
            with pytest.raises(ToolCatalogError) as caught:
                service.bind_tool(definition, actor)
            assert str(caught.value) == denied
            return False
        return service.bind_tool(definition, actor).configured

    container.settings.integration_key_file = tmp_path / "credentials.key"
    requests = []

    def send(request):
        requests.append(request)
        assert str(request.url) == "https://api.github.com/user"
        return httpx.Response(200, json={"login": "test-owner"})

    service.clickup.http.transport = httpx.MockTransport(send)
    path = "/v1/connections/integrations/github"
    body = {"credential": "github-api-test-secret", "repositories": ["example/allowed"]}
    assert client.post(path, json=body).status_code == 403
    response = client.post(path, headers=auth_headers, json=body)
    assert response.status_code == 200, response.text
    assert "github-api-test-secret" not in response.text
    assert available("github.repository")
    assert not available(
        "github.issue_create", "Enable issue and draft PR creation in your GitHub connection"
    )
    identifier = response.json()["id"]
    body["github_write_enabled"] = True
    assert client.post(path + "/" + identifier, headers=auth_headers, json=body).status_code == 200
    assert available("github.issue_create")
    assert len(requests) == 2  # Catalog checks never call the provider.
    assert (
        client.delete(
            "/v1/connections/integrations/" + identifier, headers=auth_headers
        ).status_code
        == 204
    )
    assert not available("github.repository", "Connect GitHub in Connections to use this skill")
