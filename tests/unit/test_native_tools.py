from uuid import uuid4

import pytest

from simon.adapters.google import CALENDAR_SCOPE, DRIVE_READ_SCOPE, ConnectedError
from simon.adapters.memory import InMemoryStore
from simon.adapters.native_tools import (
    NativeToolTransport,
    bind_native_tools,
    native_tool_definitions,
    native_tool_status,
    native_transport_factory,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError
from simon.services.agent_worker import WorkerCheckpointError
from tests.contract.test_google_read_permissions import grant
from tests.contract.test_local_files import local_setup


@pytest.fixture
def runtime(tmp_path):
    store = InMemoryStore()
    connected, actor, files, host = local_setup(store, tmp_path)
    run_id = uuid4()
    transport = NativeToolTransport(
        connected,
        actor=actor,
        run_id=run_id,
        revalidate=lambda: actor,
    )
    definitions = {item.id: item for item in native_tool_definitions(connected)}
    registry = TransportRegistry()
    registry.register("native", transport)

    def invoke(name, arguments=None, *, action="write", invocation=None, scopes=None):
        definition = definitions["native." + name]
        context = ToolExecutionContext(
            actor_id=actor.actor_id,
            workspace_id=actor.workspace_id,
            run_id=run_id,
            agent_id="worker",
            invocation_id=invocation or uuid4(),
            allowed_tool_ids=frozenset({definition.id}),
            scopes=actor.scopes if scopes is None else frozenset(scopes),
            authorized_action=action,
        )
        return registry.execute(definition, arguments or {}, context).output

    return connected, actor, files, host, transport, invoke


def test_local_write_read_revision_and_invocation_idempotency(runtime):
    _, _, files, _, transport, invoke = runtime
    path = {"root": "workspace", "path": "reports/notes.md"}
    arguments = {**path, "content": "Original"}
    invocation = uuid4()
    first = invoke("local_file_write", arguments, invocation=invocation)
    assert first == invoke("local_file_write", arguments, invocation=invocation)
    assert invoke("local_file_read", path)["text"] == "Original"
    saved = invoke(
        "local_file_edit",
        {
            **path,
            "revision": first["revision"],
            "old_text": "Original",
            "new_text": "Updated",
        },
    )
    assert saved["revision"] != first["revision"]
    with pytest.raises(ValidationError, match="changed"):
        invoke("local_file_write", {**path, "content": "Stale", "revision": first["revision"]})
    assert (files.workspace(transport.actor) / "reports/notes.md").read_text() == "Updated"


def test_drive_read_guidance_preserves_listing_account():
    definition = next(
        item for item in native_tool_definitions() if item.id == "native.drive_read_file"
    )
    assert "source_account_id or account_id as the account argument" in definition.description


def test_action_and_profile_scopes_cannot_be_bypassed(runtime):
    _, actor, _, _, transport, invoke = runtime
    arguments = {"root": "workspace", "path": "notes.md", "content": "Private"}
    with pytest.raises(AuthorizationError, match="action policy"):
        invoke("local_file_write", arguments, action="read")
    with pytest.raises(AuthorizationError, match="permission"):
        invoke("local_file_write", arguments, scopes={"jobs:read"})
    # Even a caller that skips TransportRegistry cannot downgrade the operation contract.
    definition = next(t for t in native_tool_definitions() if t.id == "native.local_file_write")
    definition = definition.model_copy(
        update={
            "side_effect": False,
            "action_policy": "read",
            "required_scopes": frozenset(),
        }
    )
    context = ToolExecutionContext(
        actor_id=actor.actor_id,
        workspace_id=actor.workspace_id,
        run_id=transport.run_id,
        agent_id="worker",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=actor.scopes,
    )
    with pytest.raises(AuthorizationError, match="write-authorized"):
        transport(definition, arguments, context)


@pytest.mark.parametrize("path", ["../outside.txt", "C:/Windows/win.ini", ".env", ".git/config"])
def test_local_paths_cannot_escape_or_reveal_protected_files(runtime, path):
    *_, invoke = runtime
    with pytest.raises((AuthorizationError, ValidationError, ToolExecutionError)):
        invoke("local_file_read", {"root": "workspace", "path": path})


def test_agent_never_exposes_host_roots_even_for_server_owner(runtime):
    _, actor, files, host, transport, invoke = runtime
    (host / "host-only.txt").write_text("secret")
    assert "downloads" in files.roots(actor)
    roots = invoke("local_files_roots")
    assert roots["roots"] == [{"root": "workspace", "name": "My workspace"}]
    definition = next(t for t in native_tool_definitions() if t.id == "native.local_file_read")
    context = ToolExecutionContext(
        actor_id=actor.actor_id,
        workspace_id=actor.workspace_id,
        run_id=transport.run_id,
        agent_id="worker",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=actor.scopes,
    )
    with pytest.raises(AuthorizationError, match="account workspace"):
        transport(definition, {"root": "downloads", "path": "host-only.txt"}, context)


def test_revalidation_rejects_owner_changes_and_cancellation(runtime):
    _, actor, _, _, transport, invoke = runtime
    transport.revalidate = lambda: actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(AuthorizationError, match="access changed"):
        invoke("local_files_roots")

    def cancelled():
        raise WorkerCheckpointError("cancelled")

    transport.revalidate = cancelled
    with pytest.raises(WorkerCheckpointError):
        invoke("local_file_write", {"root": "workspace", "path": "notes", "content": "x"})


def test_actor_scope_revocation_before_read_and_after_cloud_result(runtime):
    connected, actor, _, _, transport, invoke = runtime
    grant(connected, actor, (DRIVE_READ_SCOPE,))

    def read(token, request):
        transport.revalidate = lambda: actor.model_copy(update={"scopes": frozenset()})
        return {"text": "Must not be released"}

    connected.api.drive_file = read
    with pytest.raises(AuthorizationError, match="permissions changed"):
        invoke("drive_read_file", {"id": "file"})
    with pytest.raises(AuthorizationError, match="permissions changed"):
        invoke("local_files_roots")


def test_google_reads_are_scoped_to_connected_accounts(runtime):
    connected, _, _, _, _, invoke = runtime
    grant(connected, runtime[1], (DRIVE_READ_SCOPE,))
    calls = []
    connected.api.drive_search = lambda token, request: calls.append(request) or {"files": []}
    result = invoke("drive_search_files", {"query": "notes"})
    assert result["account_email"] == "owner@example.com" and len(calls) == 1
    with pytest.raises(ToolExecutionError, match="Native read") as raised:
        invoke("drive_search_files", {"account": "another@example.com"})
    assert not raised.value.unknown
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        ValidationError("private filename and provider detail"),
        ConnectedError("private provider body"),
    ],
)
def test_expected_read_failures_are_known_sanitized_and_not_retried(runtime, failure):
    connected, actor, _, _, _, invoke = runtime
    grant(connected, actor, (DRIVE_READ_SCOPE,))
    calls = []

    def read(token, request):
        calls.append(request)
        raise failure

    connected.api.drive_file = read
    with pytest.raises(ToolExecutionError) as raised:
        invoke("drive_read_file", {"id": "private-resource"})
    assert raised.value.unknown is False
    assert str(raised.value) == "Native read could not be completed."
    assert len(calls) == 1


def test_explicit_unknown_read_outcome_is_not_downgraded(runtime):
    connected, actor, _, _, _, invoke = runtime
    grant(connected, actor, (DRIVE_READ_SCOPE,))

    def read(token, request):
        raise ConnectedError("private uncertain provider detail", unknown=True)

    connected.api.drive_file = read
    with pytest.raises(ToolExecutionError) as raised:
        invoke("drive_read_file", {"id": "resource"})
    assert raised.value.unknown is True
    assert str(raised.value) == "Native read outcome is uncertain."


def test_native_read_model_validation_is_known_without_provider_dispatch(runtime):
    connected, actor, _, _, _, invoke = runtime
    grant(connected, actor, (CALENDAR_SCOPE,))
    calls = []
    connected.api.events = lambda *args: calls.append(args) or {}
    with pytest.raises(ToolExecutionError) as raised:
        invoke(
            "calendar_list_events",
            {
                "start": "2026-10-02T12:00:00Z",
                "end": "2026-10-01T12:00:00Z",
            },
        )
    assert not raised.value.unknown
    assert str(raised.value) == "Native read arguments were not accepted."
    assert not calls


@pytest.mark.parametrize("error_type", [AuthorizationError, ToolCatalogError, NotFoundError])
def test_read_authorization_catalog_and_hidden_resource_failures_still_halt(runtime, error_type):
    connected, actor, _, _, _, invoke = runtime
    grant(connected, actor, (DRIVE_READ_SCOPE,))

    def read(token, request):
        raise error_type("Read access is unavailable")

    connected.api.drive_file = read
    with pytest.raises(error_type):
        invoke("drive_read_file", {"id": "resource"})


def test_read_failure_revalidates_access_before_becoming_recoverable(runtime):
    connected, actor, _, _, transport, invoke = runtime
    grant(connected, actor, (DRIVE_READ_SCOPE,))

    def read(token, request):
        transport.revalidate = lambda: actor.model_copy(update={"scopes": frozenset()})
        raise ConnectedError("Provider denied the read")

    connected.api.drive_file = read
    with pytest.raises(AuthorizationError, match="permissions changed"):
        invoke("drive_read_file", {"id": "resource"})


@pytest.mark.parametrize(
    "failure", [ValidationError("Invalid write"), ConnectedError("Write failed")]
)
def test_write_failures_are_never_converted_to_recoverable_reads(runtime, failure):
    connected, _, _, _, _, invoke = runtime

    def write(*args):
        raise failure

    connected.local_files.run = write
    with pytest.raises(type(failure)):
        invoke("local_file_write", {"root": "workspace", "path": "report.md", "content": "Text"})


def test_unexpected_read_exception_is_not_classified_as_a_known_failure(runtime):
    connected, actor, _, _, _, invoke = runtime
    grant(connected, actor, (DRIVE_READ_SCOPE,))

    def read(token, request):
        raise RuntimeError("Unexpected internal exception")

    connected.api.drive_file = read
    with pytest.raises(RuntimeError):
        invoke("drive_read_file", {"id": "resource"})


def test_binding_installs_only_explicit_ids_and_cannot_weaken_contract(runtime):
    connected, actor, _, _, _, _ = runtime
    canonical = next(t for t in native_tool_definitions() if t.id == "native.local_file_write")
    weak = canonical.model_copy(
        update={
            "side_effect": False,
            "action_policy": "read",
            "required_scopes": frozenset(),
        }
    )
    manifest = bind_native_tools((weak,), connected)
    assert manifest == (canonical,)
    assert not bind_native_tools((), connected)
    assert native_tool_status(connected, actor, canonical.id)["available"]
    assert not native_tool_status(connected, actor, "native.drive_read_file")["available"]
    with pytest.raises(ToolCatalogError, match="Unknown native"):
        bind_native_tools(
            (canonical.model_copy(update={"id": "native.execute_shell"}),),
            connected,
        )
    connected.settings = connected.settings.model_copy(update={"local_files_enabled": False})
    bound = bind_native_tools((canonical,), connected)
    assert not bound[0].configured
    assert "native" in native_transport_factory(connected)(actor, uuid4(), lambda: actor)


def test_cached_templates_are_owned_copies_and_settings_are_rechecked(runtime):
    connected, actor, _, _, _, _ = runtime
    first = next(t for t in native_tool_definitions(connected) if t.id == "native.local_file_read")
    first.input_schema["properties"]["root"].clear()
    first.settings["network"] = True
    second = next(t for t in native_tool_definitions(connected) if t.id == first.id)
    assert "pattern" in second.input_schema["properties"]["root"]
    assert second.settings["network"] is False
    connected.settings = connected.settings.model_copy(update={"local_files_enabled": False})
    assert not native_tool_status(connected, actor, first.id)["available"]
    assert not next(t for t in native_tool_definitions(connected) if t.id == first.id).configured
