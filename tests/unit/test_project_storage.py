"""File selection, isolated roots and optional provider failures across project runs."""

from uuid import uuid4

import httpx
import pytest

from simon.adapters.cloud_storage_tools import box_tool_definitions
from simon.adapters.memory import InMemoryStore
from simon.adapters.project_storage_tools import project_storage_definitions
from simon.domain.agent_platform import AgentProfile, AgentTaskSpec, PlatformManifest
from simon.domain.errors import AuthorizationError, InvalidTransitionError
from simon.domain.project_storage import ProjectStorageLocation, SaveProjectStorage
from simon.services.agent_platform import AgentPlatformService
from simon.services.project_storage import ProjectStorageService
from simon.services.project_work import ProjectWorkService
from tests.contract.test_project_files import FOLDER, setup_project


@pytest.fixture
def storage(tmp_path):
    connected, actor, files, project_id = setup_project(InMemoryStore())
    files.ensure(actor, project_id)
    connected.settings.integration_key_file = tmp_path / "key"
    work = ProjectWorkService(connected.store, project_resolver=files.project)
    return ProjectStorageService(work, connected), actor, files, project_id


def test_existing_drive_project_automatically_selects_its_folder_only(storage):
    service, actor, _files, project_id = storage
    state = service.snapshot(actor, project_id)
    assert state["version"] == 0
    assert [location["provider"] for location in state["locations"]] == ["google_drive"]
    assert state["locations"][0]["state"] == "ready"
    tools = service.agent_locations(actor, project_id)["locations"]
    assert tools[0]["linked_drive"]
    assert any(operation["operation"] == "create" for operation in tools[0]["operations"])


def test_multiple_drive_folders_persist_and_file_reads_cannot_escape(storage):
    service, actor, files, project_id = storage
    files.api.items["second-folder"] = {
        "id": "second-folder",
        "name": "References",
        "mimeType": FOLDER,
        "parents": [],
    }
    first = service._state(actor, project_id)[1][0]
    second = ProjectStorageLocation(
        id="references",
        name="References",
        provider="google_drive",
        account=files.binding(actor, project_id).google_email,
        folder_id="second-folder",
        writable=True,
    )
    service.save(
        actor, project_id, SaveProjectStorage(expected_version=0, locations=(first, second))
    )
    restored = ProjectStorageService(service.work, service.connected)
    assert len(restored.snapshot(actor, project_id)["locations"]) == 2
    created = service.execute(
        actor,
        project_id,
        "references",
        "create",
        {"name": "reference.md", "content": "Verified reference"},
        write=True,
        key="create-reference",
        revalidate=lambda: actor,
    )
    assert created["status"] == "succeeded"
    read = restored.execute(
        actor, project_id, "references", "read", {"file_id": created["file_id"]}
    )
    assert read["text"] == "Verified reference"
    assert read["location_id"] == "references"
    with pytest.raises(AuthorizationError):
        service.execute(actor, project_id, "linked-drive", "read", {"file_id": created["file_id"]})


def test_disconnected_google_account_is_skipped_without_errors(storage):
    service, actor, _files, project_id = storage
    connection = service.connected.connection(actor)
    service.connected.store.delete_google_connection(
        connection.workspace_id, connection.actor_id, connection.id
    )
    assert service.snapshot(actor, project_id)["locations"][0]["state"] == "unavailable"
    assert service.agent_locations(actor, project_id) == {"locations": []}
    filter_tool = service.tool_filter(actor, project_id)
    definitions = {tool.id: tool for tool in project_storage_definitions()}
    assert filter_tool(definitions["project.storage_locations"])
    assert not filter_tool(definitions["project.storage_read"])


def test_storage_edits_require_the_current_version_and_project_access(storage):
    service, actor, _files, project_id = storage
    service.save(actor, project_id, SaveProjectStorage(expected_version=0, locations=()))
    with pytest.raises(InvalidTransitionError):
        service.save(actor, project_id, SaveProjectStorage(expected_version=0, locations=()))
    with pytest.raises(Exception, match="Project not found"):
        service.snapshot(actor.model_copy(update={"actor_id": uuid4()}), project_id)
    assert not service.primary_selected(actor, project_id)


def test_drive_only_project_omits_box_even_from_an_explicit_generated_task(storage, tmp_path):
    service, actor, _files, project_id = storage
    manifest = PlatformManifest(
        agents=(AgentProfile(id="worker", instructions="Read project files"),),
        tools=(*box_tool_definitions(), *project_storage_definitions()),
    )
    platform = AgentPlatformService(
        service.work.store,
        manifest,
        state_dir=tmp_path / "agents",
        integrations=service.connected.integrations,
        available_transports=("box", "project_storage"),
    )
    platform.project_tool_filter_factory = service.tool_filter
    profile = platform.profiles(actor, project_id)[0]
    task = platform._task(
        actor,
        uuid4(),
        AgentTaskSpec(
            id="read",
            agent_id="worker",
            objective="Read project sources",
            tool_ids=("box.list", "project.storage_read"),
        ),
        profile,
        project_id,
    )
    assert task.tool_ids == ("project.storage_read",)
    assert not any("box" in reason.casefold() for reason in task.blocked_reasons)


def test_connected_box_is_also_omitted_when_project_has_only_drive(storage, tmp_path):
    service, actor, _files, project_id = storage
    from simon.domain.integrations import ConnectIntegration

    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200, json={"type": "folder", "id": "0"})

    service.connected.integrations.clickup.http.transport = httpx.MockTransport(send)
    service.connected.integrations.connect(
        actor, "box", ConnectIntegration(credential="private-token", root_folder_id="0")
    )
    platform = AgentPlatformService(
        service.work.store,
        PlatformManifest(tools=box_tool_definitions()),
        state_dir=tmp_path / "agents",
        integrations=service.connected.integrations,
        available_transports=("box",),
    )
    platform.project_tool_filter_factory = service.tool_filter
    assert all(row["state"] == "configured" for row in platform.tool_statuses(actor))
    assert all(row["state"] == "unavailable" for row in platform.tool_statuses(actor, project_id))
    assert len(requests) == 1  # Opening readiness never calls Box.


def test_connected_dropbox_subfolder_can_be_added_and_disconnect_is_optional(storage):
    service, actor, _files, project_id = storage
    from simon.domain.integrations import ConnectIntegration

    requests = []

    def send(request):
        requests.append(request)
        if request.url.path.endswith("get_metadata"):
            return httpx.Response(200, json={".tag": "folder", "path_lower": "/project"})
        return httpx.Response(200, json={"entries": [], "has_more": False, "cursor": "cursor"})

    service.connected.integrations.clickup.http.transport = httpx.MockTransport(send)
    connection = service.connected.integrations.connect(
        actor, "dropbox", ConnectIntegration(credential="private-token", root_path="/Project")
    )
    first = service._state(actor, project_id)[1][0]
    second = ProjectStorageLocation(
        id="dropbox",
        name="Source archive",
        provider="dropbox",
        connection_id=connection["id"],
        path="Archive",
    )
    state = service.save(
        actor, project_id, SaveProjectStorage(expected_version=0, locations=(first, second))
    )
    assert state["locations"][1]["state"] == "ready"
    assert "private-token" not in str(state)
    assert b"/Project/Archive" in requests[-1].content
    service.connected.integrations.disconnect(actor, connection["id"])
    assert len(service.agent_locations(actor, project_id)["locations"]) == 1
    assert service.snapshot(actor, project_id)["locations"][1]["state"] == "unavailable"


def test_storage_operation_revalidates_changed_selection_before_returning(storage):
    service, actor, files, project_id = storage
    original = files.api.list_files

    def concurrent_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        service.save(actor, project_id, SaveProjectStorage(expected_version=0, locations=()))
        return result

    files.api.list_files = concurrent_edit
    with pytest.raises(AuthorizationError):
        service.execute(actor, project_id, "linked-drive", "list", {})


def test_read_operations_cannot_dispatch_a_write_and_drive_aliases_are_canonical(storage):
    service, actor, files, project_id = storage
    with pytest.raises(AuthorizationError, match="action policy"):
        service.execute(actor, project_id, "linked-drive", "create", {"name": "must-not-exist"})
    files.api.items["root"] = {
        "id": "real-root",
        "name": "My Drive",
        "mimeType": FOLDER,
        "parents": [],
    }
    location = ProjectStorageLocation(
        id="drive-root",
        name="Whole drive",
        provider="google_drive",
        account=str(service.connected.connection(actor).id),
        folder_id="root",
    )
    state = service.save(
        actor, project_id, SaveProjectStorage(expected_version=0, locations=(location,))
    )
    assert state["locations"][0]["folder_id"] == "real-root"
    assert state["locations"][0]["account"] == service.connected.connection(actor).email
