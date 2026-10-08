"""Removed workflows cannot be reached through HTTP, chat tools, or file roots."""

from uuid import uuid4

import pytest

from simon.adapters.native_tools import native_tool_definitions
from simon.domain.errors import AuthorizationError
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import ActorContext, Channel
from simon.services.identity import ROLE_SCOPES


@pytest.mark.parametrize(
    "path",
    [
        "/v1/projects",
        "/v1/assistant-tasks",
        "/v1/work/overview",
        "/v1/agent-platform/catalog",
        "/v1/project-boards/connections",
        "/v1/agent-setup-assistant",
    ],
)
def test_retired_authorities_are_not_http_routes(client, auth_headers, path):
    assert client.get(path).status_code == 404
    assert client.post(path, headers=auth_headers, json={}).status_code == 404
    assert not any(route.startswith(path) for route in client.get("/openapi.json").json()["paths"])


def test_no_retired_project_tools_or_memory_backed_file_roots(container):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )
    assert not any(
        name.startswith(("project_", "task_")) for name in container.connected.available(actor)
    )
    assert not any("project" in item.id for item in native_tool_definitions())
    container.settings.local_files_enabled = True
    with pytest.raises(AuthorizationError, match="Unknown local root"):
        container.connected.local_files.path(actor, "project:" + str(uuid4()), "file.txt")
