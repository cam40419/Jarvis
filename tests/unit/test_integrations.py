"""Connection lifecycle and live credential lookup shared by API and workers."""

from uuid import uuid4

import httpx
import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.optional_http import BoundedHTTP
from simon.config import Settings
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.integrations import ConnectIntegration
from simon.services.integrations import IntegrationService
from tests.unit.test_clickup import metadata, task
from tests.unit.test_external_actions import actor


def service(tmp_path, store=None):
    workspaces = [{"id": "123", "name": "Company"}]
    requests = []

    def send(request):
        requests.append(request)
        path = request.url.path
        if path == "/api/v2/team":
            value = {"teams": workspaces}
        elif path == "/api/v2/team/123/space":
            value = {"spaces": [{"id": "789"}]}
        elif path == "/api/v2/team/123/shared":
            value = {"shared": {"lists": [], "folders": [], "tasks": []}}
        elif path == "/api/v2/space/789/list":
            value = {"lists": [{"id": "456"}]}
        elif path == "/api/v2/space/789/folder":
            value = {"folders": [{"id": "999"}]}
        elif path == "/api/v2/folder/999/list":
            value = {"lists": [{"id": "457"}]}
        elif path in {"/api/v2/list/456", "/api/v2/list/457"}:
            value = metadata(id=path.rsplit("/", 1)[1])
        elif path == "/api/v2/list/456/task":
            value = {"tasks": [task()], "last_page": True}
        elif path == "/api/v2/task/task1":
            value = task()
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=value)

    result = IntegrationService(
        store or InMemoryStore(),
        Settings(_env_file=None, integration_key_file=tmp_path / "credentials.key"),
        http=BoundedHTTP(transport=httpx.MockTransport(send)),
    )
    return result, workspaces, requests


def test_clickup_full_discovery_credentials_and_reconnect(tmp_path):
    api, workspaces, requests = service(tmp_path)
    body = ConnectIntegration(credential="pk_test-secret")
    public = api.connect(actor(), "clickup", body)
    record = api.store.integration_connections(actor().workspace_id, actor().actor_id)[0]
    assert "pk_test-secret" not in record.model_dump_json()
    assert "encrypted_secret" not in public
    connection = api.board_connections(actor())[0]
    worker, _, _ = service(tmp_path, api.store)
    assert worker.credentials[connection.credential_env] == "pk_test-secret"
    # Both folderless Lists and folder Lists are available without configured List IDs.
    boards = api.clickup.boards(connection, actor())
    assert {board.id for board in boards} == {"456", "457"}
    assert api.clickup.tasks(connection, actor(), "456").tasks[0].id == "task1"
    assert api.clickup.accessible_task(connection, actor(), "task1").id == "task1"
    workspaces.append({"id": "124", "name": "New workspace"})
    api._workspace_cache.clear()
    assert {row.clickup_workspace_id for row in api.board_connections(actor())} == {"123", "124"}
    api.connect(actor(), "clickup", ConnectIntegration(credential="pk_rotated"), public["id"])
    assert worker.credentials[connection.credential_env] == "pk_rotated"
    assert api.board_connections(actor())[0].id == connection.id
    api.disconnect(actor(), public["id"])
    assert worker.credentials.get(connection.credential_env) is None
    assert requests


def test_connection_isolation_and_worker_cannot_connect(tmp_path):
    api, _, _ = service(tmp_path)
    public = api.connect(actor(), "clickup", ConnectIntegration(credential="pk_test-secret"))
    other = actor(actor_id=uuid4())
    assert api.list(other) == []
    assert api.board_connections(other) == ()
    with pytest.raises(NotFoundError):
        api.disconnect(other, public["id"])
    with pytest.raises(NotFoundError):
        api.connect(other, "clickup", ConnectIntegration(credential="pk_test"), public["id"])
    from simon.domain.models import Channel

    with pytest.raises(AuthorizationError):
        api.connect(
            actor().model_copy(update={"channel": Channel.WORKER}),
            "clickup",
            ConnectIntegration(credential="pk_test"),
        )


def test_twilio_and_gateway_are_persisted_and_resolved(tmp_path):
    api, _, _ = service(tmp_path)
    api.connect(
        actor(),
        "twilio",
        ConnectIntegration(
            credential="phone-secret", account_sid="AC" + "a" * 32, from_number="+15551234567"
        ),
    )
    api.connect(
        actor(),
        "gateway",
        ConnectIntegration(
            credential="booking-secret",
            endpoint="https://booking.example.test",
            merchant_names={"shop": "My shop"},
        ),
    )
    definitions = api.external_connections(actor())
    assert {row.kind for row in definitions} == {"twilio", "gateway"}
    assert {api.credentials[row.credential_env] for row in definitions} == {
        "phone-secret",
        "booking-secret",
    }


def test_discovery_still_checks_workspace_boundaries(tmp_path):
    api, _, _ = service(tmp_path)
    api.connect(actor(), "clickup", ConnectIntegration(credential="pk_test-secret"))
    connection = api.board_connections(actor())[0]
    # Discovery is not a bypass for foreign account or Workspace access.
    with pytest.raises(AuthorizationError):
        api.clickup.tasks(connection, actor(actor_id=uuid4()), "456")
    foreign = connection.model_copy(update={"workspace_id": "999"})
    with pytest.raises(AuthorizationError):
        api.clickup.tasks(foreign, actor(), "456")


def test_shared_lists_do_not_grant_other_lists_in_the_same_private_space(tmp_path):
    api, _, _ = service(tmp_path)
    api.connect(actor(), "clickup", ConnectIntegration(credential="pk_shared-token"))
    connection = api.board_connections(actor())[0]

    def send(request):
        path = request.url.path
        if path == "/api/v2/team":
            data = {"teams": [{"id": "123"}]}
        elif path == "/api/v2/team/123/space":
            data = {"spaces": []}
        elif path == "/api/v2/team/123/shared":
            data = {"shared": {"lists": [{"id": "456"}], "folders": [], "tasks": [{"id": "task1"}]}}
        elif path in {"/api/v2/list/456", "/api/v2/list/457"}:
            data = metadata(id=path.rsplit("/", 1)[1])
        elif path == "/api/v2/task/task1":
            data = task()
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)

    api.clickup.http.transport = httpx.MockTransport(send)
    assert [row.id for row in api.clickup.boards(connection, actor())] == ["456"]
    assert api.clickup.list_metadata(connection, actor(), "456").space_id == "789"
    with pytest.raises(AuthorizationError):
        api.clickup.list_metadata(connection, actor(), "457")
    assert api.clickup.shared_task_ids(connection, actor()) == ("task1",)
    assert api.clickup.accessible_task(connection, actor(), "task1").id == "task1"


def test_only_site_admin_can_configure_google_app(tmp_path):
    api, _, _ = service(tmp_path)
    with pytest.raises(AuthorizationError):
        api.connect(
            actor(actor_id=uuid4()),
            "google_app",
            ConnectIntegration(
                client_id="123-test.apps.googleusercontent.com", credential="google-secret"
            ),
        )
