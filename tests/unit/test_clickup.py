"""ClickUp wire contracts and isolation; every request uses httpx.MockTransport."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.clickup import (
    ClickUpAdapter,
    board_configuration_reasons,
    load_board_connections,
    task_marker,
)
from simon.adapters.optional_http import BoundedHTTP
from simon.domain.errors import AuthorizationError, InvalidTransitionError
from simon.domain.project_boards import BoardConnection
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionError
from tests.unit.test_external_actions import actor

TOKEN = "pk_synthetic-clickup-token"


def connection(**updates):
    return BoardConnection(
        id="company",
        name="Company ClickUp",
        enabled=True,
        workspace_id=actor().workspace_id,
        actor_ids=frozenset({actor().actor_id}),
        clickup_workspace_id="123",
        list_ids=frozenset({"456"}),
        credential_env="CLICKUP_TOKEN",
    ).model_copy(update=updates)


def metadata(**updates):
    return {
        "id": "456",
        "name": "Company project",
        "space": {"id": "789"},
        "archived": False,
        "statuses": [
            {"status": "to do", "type": "open", "color": "#123456"},
            {"status": "in progress", "type": "custom", "color": "#224466"},
            {"status": "complete", "type": "closed", "color": "#668899"},
        ],
        **updates,
    }


def task(identifier="task1", **updates):
    return {
        "id": identifier,
        "name": "Human task name",
        "description": "Human task description",
        "status": {"status": "to do", "type": "open"},
        "date_updated": "1780000000000",
        "team_id": "123",
        "space": {"id": "789"},
        "list": {"id": "456"},
        "assignees": [{"id": 100, "username": "Human owner", "email": "private@example.test"}],
        "dependencies": [],
        "archived": False,
        "url": "https://attacker.invalid/response-url-must-not-be-used",
        **updates,
    }


def adapter(handler=None, *, environ=None):
    requests = []

    def send(request):
        requests.append(request)
        custom = handler(request) if handler else None
        if custom is not None:
            return custom
        path = request.url.path
        if path == "/api/v2/team":
            return httpx.Response(200, json={"teams": [{"id": "123", "name": "Company"}]})
        if path == "/api/v2/team/123/space":
            return httpx.Response(200, json={"spaces": [{"id": "789", "archived": False}]})
        if path == "/api/v2/list/456":
            return httpx.Response(200, json=metadata())
        if path == "/api/v2/list/456/task" and request.method == "GET":
            return httpx.Response(200, json={"tasks": [task()], "last_page": True})
        if path.startswith("/api/v2/task/") and request.method == "GET":
            return httpx.Response(200, json=task(path.rsplit("/", 1)[1]))
        raise AssertionError(f"Unexpected synthetic request: {request.method} {path}")

    return ClickUpAdapter(
        BoundedHTTP(
            transport=httpx.MockTransport(send),
            environ={"CLICKUP_TOKEN": TOKEN} if environ is None else environ,
        )
    ), requests


def test_lists_are_allowlisted_and_belong_to_verified_workspace():
    client, requests = adapter()
    boards = client.boards(connection(), actor())
    assert len(boards) == 1 and boards[0].id == "456"
    assert boards[0].statuses[-1].type == "closed"
    assert all(request.url.host == "api.clickup.com" for request in requests)
    assert all(request.headers["Authorization"] == TOKEN for request in requests)
    assert [request.url.path for request in requests] == [
        "/api/v2/team",
        "/api/v2/team/123/space",
        "/api/v2/list/456",
    ]


@pytest.mark.parametrize("grant", ["actor", "workspace", "scope", "list", "disabled", "secret"])
def test_local_unauthorized_or_unconfigured_connections_never_make_requests(grant):
    configured, who, list_id, environ = connection(), actor(), "456", {"CLICKUP_TOKEN": TOKEN}
    if grant == "actor":
        who = who.model_copy(update={"actor_id": uuid4()})
    elif grant == "workspace":
        who = who.model_copy(update={"workspace_id": uuid4()})
    elif grant == "scope":
        who = who.model_copy(update={"scopes": frozenset()})
    elif grant == "list":
        list_id = "999"
    elif grant == "disabled":
        configured = configured.model_copy(update={"enabled": False})
    else:
        environ = {}
    client, requests = adapter(environ=environ)
    with pytest.raises((AuthorizationError, ToolCatalogError)):
        client.list_metadata(configured, who, list_id)
    assert not requests


@pytest.mark.parametrize("boundary", ["workspace", "list_space", "task_list", "task_workspace"])
def test_remote_resource_cannot_escape_workspace_or_home_list(boundary):
    def send(request):
        if boundary == "workspace" and request.url.path.endswith("/team"):
            return httpx.Response(200, json={"teams": [{"id": "999"}]})
        if boundary == "list_space" and request.url.path.endswith("/list/456"):
            return httpx.Response(200, json=metadata(space={"id": "999"}))
        if request.url.path.endswith("/task/task1"):
            changed = {"list": {"id": "999"}} if boundary == "task_list" else {"team_id": "999"}
            return httpx.Response(200, json=task(**changed))
        return None

    client, requests = adapter(send)
    with pytest.raises((AuthorizationError, ToolExecutionError)):
        client.get_task(connection(), actor(), "task1", list_id="456")
    assert all(request.method == "GET" for request in requests)


def test_tasks_are_bounded_paginated_and_return_safe_scoped_snapshots():
    def send(request):
        if request.url.path.endswith("/list/456/task"):
            rows = [task(f"task{number}") for number in range(100)]
            rows[0]["dependencies"] = [
                {"task_id": "task0", "depends_on": "task2", "workspace_id": "123", "type": 1},
                {"task_id": "task3", "depends_on": "task0", "workspace_id": "123", "type": 1},
            ]
            return httpx.Response(200, json={"tasks": rows, "last_page": False})
        return None

    client, requests = adapter(send)
    page = client.tasks(connection(auth_type="oauth"), actor(), "456", page=3)
    assert page.page == 3 and page.next_page == 4 and len(page.tasks) == 100
    assert page.tasks[0].dependencies == ("task2",)
    assert page.tasks[0].assignees[0].name == "Human owner"
    assert page.tasks[0].url == "https://app.clickup.com/t/task0"
    assert "private@example" not in page.model_dump_json()
    assert requests[-1].url.params["page"] == "3"
    assert requests[-1].url.params["include_closed"] == "true"
    assert requests[-1].url.params["include_timl"] == "false"
    assert requests[-1].headers["Authorization"] == "Bearer " + TOKEN
    for invalid in (-1, True, 1001):
        with pytest.raises(ToolCatalogError):
            client.tasks(connection(), actor(), "456", page=invalid)


def test_batched_task_reads_reuse_only_one_request_scoped_verification():
    client, requests = adapter()
    result = client.get_tasks(connection(), actor(), ("task1", "task2"), list_id="456")
    assert [item.id for item in result] == ["task1", "task2"]
    assert len(requests) == 5  # Three hierarchy checks plus two task reads.
    client.get_tasks(connection(), actor(), ("task1", "task2"), list_id="456")
    assert len(requests) == 10
    assert sum(request.url.path.endswith("/team") for request in requests) == 2
    with pytest.raises(AuthorizationError):
        client.get_tasks(connection(), actor(actor_id=uuid4()), ("task1",), list_id="456")
    assert len(requests) == 10
    for invalid in ((), ("task1",) * 26, ("task1", "task1"), "task1"):
        with pytest.raises(ToolCatalogError):
            client.get_tasks(connection(), actor(), invalid, list_id="456")
    assert len(requests) == 10


def test_batch_rejects_cross_list_task_without_returning_partial_snapshot():
    def send(request):
        if request.url.path.endswith("/task/task2"):
            return httpx.Response(200, json=task("task2", list={"id": "999"}))
        return None

    client, requests = adapter(send)
    with pytest.raises(ToolExecutionError):
        client.get_tasks(connection(), actor(), ("task1", "task2"), list_id="456")
    assert len(requests) == 5 and all(request.method == "GET" for request in requests)


def test_snapshot_does_not_change_when_provider_reorders_assignees_or_dependencies():
    reverse = False

    def send(request):
        if request.url.path.endswith("/task/task1"):
            people = [{"id": 200, "username": "Second"}, {"id": 100, "username": "First"}]
            links = [
                {"task_id": "task1", "depends_on": value, "workspace_id": "123"}
                for value in ("task3", "task2")
            ]
            return httpx.Response(
                200,
                json=task(
                    assignees=list(reversed(people)) if reverse else people,
                    dependencies=list(reversed(links)) if reverse else links,
                ),
            )
        return None

    client, _ = adapter(send)
    first = client.get_task(connection(), actor(), "task1", list_id="456")
    reverse = True
    assert client.get_task(connection(), actor(), "task1", list_id="456") == first


def test_create_honors_required_fields_and_appends_durable_marker_without_fake_idempotency():
    def send(request):
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json=task(
                    "created123",
                    name=body["name"],
                    description=body["description"],
                ),
            )
        return None

    client, requests = adapter(send)
    result = client.create_task(
        connection(),
        actor(),
        "456",
        name="Managed task",
        description="Project intent",
        status="to do",
        marker="operation:123",
    )
    assert result.id == "created123" and task_marker("operation:123") in result.description
    write = requests[-1]
    body = json.loads(write.content)
    assert body["check_required_custom_fields"] is True and body["notify_all"] is False
    assert set(body) == {
        "name",
        "description",
        "status",
        "check_required_custom_fields",
        "notify_all",
    }
    assert "Idempotency-Key" not in write.headers


def test_status_update_detects_human_edit_and_sends_only_status():
    def send(request):
        if request.method == "PUT":
            return httpx.Response(
                200,
                json=task(
                    status={"status": "complete", "type": "closed"}, date_updated="1780000001000"
                ),
            )
        return None

    client, requests = adapter(send)
    with pytest.raises(InvalidTransitionError):
        client.update_status(
            connection(),
            actor(),
            "task1",
            list_id="456",
            status="complete",
            expected_updated="1770000000000",
        )
    assert not any(request.method == "PUT" for request in requests)
    changed = client.update_status(
        connection(),
        actor(),
        "task1",
        list_id="456",
        status="complete",
        expected_updated="1780000000000",
    )
    assert changed.status == "complete" and changed.name == "Human task name"
    assert changed.description == "Human task description"
    assert json.loads(requests[-1].content) == {"status": "complete"}
    with pytest.raises(ToolCatalogError):
        client.update_status(
            connection(),
            actor(),
            "task1",
            list_id="456",
            status="not a status",
            expected_updated="1780000000000",
        )


def test_dependencies_validate_both_endpoints_and_comments_do_not_rewrite_description():
    def send(request):
        if request.url.path.endswith("/dependency"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/comment"):
            return httpx.Response(
                200, json={"id": "45678", "date": 1780000002000, "hist_id": "789"}
            )
        return None

    client, requests = adapter(send)
    result = client.add_dependency(connection(), actor(), "task1", "task2", list_id="456")
    assert result["accepted"] is True
    assert json.loads(requests[-1].content) == {"depends_on": "task2"}
    assert {request.url.path for request in requests} >= {
        "/api/v2/task/task1",
        "/api/v2/task/task2",
    }
    receipt = client.append_comment(
        connection(), actor(), "task1", list_id="456", text="Progress", marker="comment:123"
    )
    assert receipt["comment_id"] == "45678"
    assert json.loads(requests[-1].content) == {
        "comment_text": "Progress\n\n" + task_marker("comment:123"),
        "notify_all": False,
    }
    with pytest.raises(ToolCatalogError):
        client.add_dependency(connection(), actor(), "task1", "task1", list_id="456")


@pytest.mark.parametrize("failure", ["timeout", "server", "invalid", "redirect", "wrong_receipt"])
def test_uncertain_writes_are_unknown_and_never_automatically_retried(failure):
    def send(request):
        if request.method != "POST":
            return None
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "server":
            return httpx.Response(503, json={"secret": TOKEN})
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.invalid"})
        return httpx.Response(200, json={} if failure == "invalid" else task())

    client, requests = adapter(send)
    with pytest.raises(ToolExecutionError) as result:
        client.create_task(
            connection(),
            actor(),
            "456",
            name="Managed task",
            description="Description",
            status="to do",
            marker="operation:456",
        )
    assert result.value.unknown and TOKEN not in str(result.value)
    assert len([request for request in requests if request.method == "POST"]) == 1
    assert all(request.url.host == "api.clickup.com" for request in requests)


def test_input_paths_and_connection_file_reject_arbitrary_endpoints_and_inline_secrets(tmp_path):
    client, requests = adapter()
    for identifier in ("../other", "task?other=1", "https://other.invalid", "task%2Fother"):
        with pytest.raises(ToolCatalogError):
            client.get_task(connection(), actor(), identifier, list_id="456")
    assert not requests
    assert load_board_connections(None) == ()
    path = tmp_path / "boards.json"
    valid = connection().model_dump(mode="json")
    path.write_text(json.dumps([valid]))
    assert load_board_connections(path) == (connection(),)
    for data in (
        [{**valid, "endpoint": "https://other.invalid"}],
        [{**valid, "token": TOKEN}],
        [valid, valid],
        {"connections": []},
    ):
        path.write_text(json.dumps(data))
        with pytest.raises(ToolCatalogError):
            load_board_connections(path)
    with pytest.raises(ValidationError):
        BoardConnection.model_validate({**valid, "list_ids": ["../../other"]})
    assert board_configuration_reasons(connection(), {"CLICKUP_TOKEN": "not-a-personal-token"})


def test_oversized_task_page_and_credential_echo_are_rejected():
    def send(request):
        if request.url.path.endswith("/list/456/task"):
            return httpx.Response(200, json={"tasks": [task()] * 101})
        return None

    client, _ = adapter(send)
    with pytest.raises(ToolExecutionError):
        client.tasks(connection(), actor(), "456")
    client, _ = adapter(lambda _: httpx.Response(200, json={"secret": TOKEN}))
    with pytest.raises(ToolExecutionError) as error:
        client.boards(connection(), actor())
    assert TOKEN not in str(error.value)
