from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.adapters.google import ConnectedError
from simon.adapters.native_tools import (
    NativeToolTransport,
    native_tool_definitions,
    native_tool_status,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import StartAgentRun
from simon.domain.connected_tools import CalendarDraft
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import JobStatus
from simon.domain.tool_catalog import ToolExecutionContext, ToolExecutionError
from simon.services.agent_calendar import WRITE_SCOPES, AgentCalendarService
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from tests.contract.test_connected import EVENT, connected_setup


def calendar_setup(store, tmp_path, *, claim=True):
    connected, actor, _ = connected_setup(store)
    definitions = tuple(
        tool
        for tool in native_tool_definitions(connected)
        if tool.id.startswith("native.calendar_")
    )
    profile = AgentProfile(
        id="calendar",
        instructions="Manage the assigned calendar event.",
        tool_ids=tuple(tool.id for tool in definitions),
        tool_scopes=WRITE_SCOPES,
        max_action="write",
    )
    platform = AgentPlatformService(
        store,
        PlatformManifest(
            tools=definitions,
            agents=(profile,),
            teams=(TeamTemplate(id="calendar", name="Calendar", agent_ids=("calendar",)),),
            models=(
                ModelEndpoint(
                    id="mock",
                    provider="openai_compatible",
                    model="synthetic",
                    base_url="http://localhost:1234/v1",
                    local=True,
                    capabilities=frozenset({"text", "tools"}),
                ),
            ),
        ),
        state_dir=tmp_path,
        environ={},
        available_transports=("native",),
    )
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: actor)
    plan = platform.plan(
        actor,
        PlanTeamRequest(
            team_id="calendar",
            idempotency_key="calendar-plan",
            tasks=(
                AgentTaskSpec(
                    id="event",
                    agent_id="calendar",
                    objective="Add the requested lunch event.",
                    tool_ids=profile.tool_ids,
                ),
            ),
        ),
    )
    assert plan.state == "planned", plan
    run = runs.start(actor, plan.id, StartAgentRun(idempotency_key="calendar-start"))
    if claim:
        run = runs.claim(run.id)
    service = AgentCalendarService(connected)
    calls = []

    def execute(token, action, sender):
        calls.append(action)
        assert store.get_job(action.id).status == JobStatus.RUNNING
        assert action.immediate and action.calendar.title == "Lunch"
        return action.id.hex, "https://www.google.com/calendar/event?eid=synthetic"

    connected.api.execute = execute
    return SimpleNamespace(
        connected=connected,
        actor=actor,
        platform=platform,
        runs=runs,
        run=run,
        service=service,
        calls=calls,
        definitions={t.id: t for t in definitions},
    )


@pytest.fixture
def calendar(store, tmp_path):
    return calendar_setup(store, tmp_path)


def checkpoint(h, invocation=None):
    invocation = invocation or uuid4()
    h.runs.task_update(
        h.run.id,
        "event",
        lambda task: task.model_copy(
            update={
                "status": "running",
                "events": (
                    *task.events,
                    {
                        "event": "tool_dispatch",
                        "tool_id": "native.calendar_create_event",
                        "invocation_id": str(invocation),
                    },
                ),
            }
        ),
        executor_id=h.run.executor_id,
    )
    return invocation


def create(h, invocation=None, **changes):
    return h.service.create(
        h.actor,
        h.run.id,
        "calendar",
        invocation or checkpoint(h),
        CalendarDraft(**{**EVENT, **changes}),
        lambda: h.runs.live_actor(h.runs.job(h.run.id)),
    )


def test_calendar_creates_once_and_receipt_survives_reconstruction(calendar):
    h = calendar
    invocation = checkpoint(h)
    first = create(h, invocation)
    assert first.status == "succeeded" and first.provider_id == first.action_id.hex
    assert create(h, invocation) == first
    # New invocations and equivalent UTC offsets still identify the same provider event.
    assert create(h, start="2026-10-01T16:00:00Z", end="2026-10-01T17:00:00Z") == first
    assert len(h.calls) == 1
    with pytest.raises(IdempotencyConflictError):
        create(h, invocation, title="Changed event")
    restored = AgentCalendarService(h.connected)
    assert restored.get(h.actor, h.run.id, first.action_id) == first
    assert restored.list_for_run(h.actor, h.run.id) == (first,)
    # The agent never creates a fake chat run or bypasses the chat-original guard.
    assert h.connected.store.action(first.action_id) is None
    with pytest.raises(AuthorizationError, match="original request"):
        h.connected.create_calendar_event(
            h.actor,
            h.run.id,
            CalendarDraft(**EVENT),
            lambda: h.actor,
        )


@pytest.mark.parametrize("unknown", [False, True])
def test_failed_and_unknown_receipts_never_redispatch(calendar, unknown):
    h = calendar

    def fail(*_):
        h.calls.append(1)
        raise ConnectedError("Provider rejected or connection dropped", unknown=unknown)

    h.connected.api.execute = fail
    first = create(h)
    assert first.status == ("unknown" if unknown else "failed")
    assert create(h) == first and len(h.calls) == 1
    if unknown:
        with pytest.raises(ValidationError, match="uncertain"):
            create(h, title="Try again as a renamed event")
        assert len(h.calls) == 1


def test_concurrent_calls_observe_durable_claim_without_network_lock(calendar):
    h = calendar
    started, release = Event(), Event()

    def wait(token, action, sender):
        h.calls.append(action)
        started.set()
        assert release.wait(5)
        return action.id.hex, None

    h.connected.api.execute = wait
    first, second = checkpoint(h), checkpoint(h)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(create, h, first)
        try:
            assert started.wait(5)
            repeated = create(h, second)
            assert repeated.status == "executing" and len(h.calls) == 1
        finally:
            release.set()
        assert future.result().action_id == repeated.action_id


@pytest.mark.parametrize("reason", ["checkpoint", "cancelled", "read_only", "tool", "local_only"])
def test_live_run_checkpoint_and_saved_grants_cannot_be_bypassed(calendar, reason):
    h = calendar
    invocation = checkpoint(h)
    if reason == "checkpoint":
        invocation = uuid4()
    elif reason == "cancelled":
        h.runs.cancel(h.actor, h.run.id)
    else:
        job = h.connected.store.get_job(h.run.plan_id)
        values = job.input
        profile = values["configuration"]["agents"][0]
        if reason == "read_only":
            profile["max_action"] = "read"
        elif reason == "tool":
            profile["tool_ids"] = []
        else:
            profile["privacy"] = "local_only"
        h.connected.store.save_job(job.model_copy(update={"input": values}), job.version)
    with pytest.raises(AuthorizationError):
        h.service.create(
            h.actor, h.run.id, "calendar", invocation, CalendarDraft(**EVENT), lambda: h.actor
        )
    assert not h.calls


def test_native_tool_action_account_scope_and_unavailable_google(calendar):
    h = calendar
    invocation = checkpoint(h)
    transport = NativeToolTransport(
        h.connected, actor=h.actor, run_id=h.run.id, revalidate=lambda: h.actor
    )
    registry = TransportRegistry()
    registry.register("native", transport)
    definition = h.definitions["native.calendar_create_event"]
    context = ToolExecutionContext(
        actor_id=h.actor.actor_id,
        workspace_id=h.actor.workspace_id,
        run_id=h.run.id,
        agent_id="calendar",
        invocation_id=invocation,
        allowed_tool_ids=frozenset(h.definitions),
        scopes=h.actor.scopes,
        authorized_action="read",
    )
    with pytest.raises(AuthorizationError):
        registry.execute(definition, EVENT, context)
    context = context.model_copy(update={"authorized_action": "write"})
    with pytest.raises(ConnectedError, match="not connected"):
        registry.execute(definition, {**EVENT, "account": "other@example.com"}, context)
    assert not h.calls
    receipt = registry.execute(definition, EVENT, context).output
    assert receipt["created"] and not receipt["requires_confirmation"]
    h.connected.disconnect(h.actor)
    unavailable = native_tool_status(h.connected, h.actor, definition.id)
    assert unavailable["reason"] == "Connect a Google account"
    status = h.definitions["native.calendar_action_status"]
    assert native_tool_status(h.connected, h.actor, status.id)["available"]
    result = registry.execute(status, {"action_id": receipt["action_id"]}, context).output
    assert result["status"] == "succeeded"
    with pytest.raises(AuthorizationError, match="Connect a Google"):
        registry.execute(definition, EVENT, context)


def test_native_unknown_outcome_halts_instead_of_reporting_success(calendar):
    h = calendar
    invocation = checkpoint(h)
    h.connected.api.execute = lambda *_: (_ for _ in ()).throw(
        ConnectedError("Response lost", unknown=True),
    )
    definition = h.definitions["native.calendar_create_event"]
    context = ToolExecutionContext(
        actor_id=h.actor.actor_id,
        workspace_id=h.actor.workspace_id,
        run_id=h.run.id,
        agent_id="calendar",
        invocation_id=invocation,
        allowed_tool_ids=frozenset(h.definitions),
        scopes=h.actor.scopes,
        authorized_action="write",
    )
    with pytest.raises(ToolExecutionError) as error:
        NativeToolTransport(
            h.connected, actor=h.actor, run_id=h.run.id, revalidate=lambda: h.actor
        )(definition, EVENT, context)
    assert error.value.unknown
    assert h.service.list_for_run(h.actor, h.run.id)[0].status == "unknown"


def test_receipt_access_is_owner_workspace_and_run_scoped(calendar):
    h = calendar
    receipt = create(h)
    for actor in (
        h.actor.model_copy(update={"actor_id": uuid4()}),
        h.actor.model_copy(update={"workspace_id": uuid4()}),
    ):
        with pytest.raises((AuthorizationError, NotFoundError)):
            h.service.list_for_run(actor, h.run.id)
    with pytest.raises(NotFoundError):
        h.service.get(h.actor, h.run.id, uuid4())
    with pytest.raises(NotFoundError):
        h.service.get(h.actor, uuid4(), receipt.action_id)


def test_stop_after_claim_before_dispatch_keeps_failed_receipt_without_write(calendar):
    h = calendar
    invocation = checkpoint(h)
    checks = []

    def revalidate():
        checks.append(1)
        if len(checks) == 2:
            h.runs.cancel(h.actor, h.run.id)
        return h.actor

    receipt = h.service.create(
        h.actor, h.run.id, "calendar", invocation, CalendarDraft(**EVENT), revalidate
    )
    assert receipt.status == "failed" and not h.calls
    assert h.service.list_for_run(h.actor, h.run.id) == (receipt,)


def test_oauth_scope_change_after_token_refresh_prevents_provider_write(calendar):
    h = calendar
    original = h.connected.access_token

    def revoked(actor, connection):
        token = original(actor, connection)
        h.connected.store.save_google_connection(connection.model_copy(update={"scopes": ()}))
        return token

    h.connected.access_token = revoked
    receipt = create(h)
    assert receipt.status == "failed" and not h.calls
    availability = native_tool_status(h.connected, h.actor, "native.calendar_create_event")
    assert not availability["available"] and "Reconnect Google" in availability["reason"]


def test_run_creation_limit_and_receipt_mismatch_fail_closed(calendar):
    h = calendar
    for index in range(3):
        h.connected.api.execute = lambda token, action, sender: (action.id.hex, None)
        assert create(h, title=f"Meeting {index}").status == "succeeded"
    with pytest.raises(ValidationError, match="At most three"):
        create(h, title="Fourth meeting")


def test_mismatched_provider_receipt_is_unknown_and_never_success(calendar):
    h = calendar
    h.connected.api.execute = lambda *_: ("different-event", None)
    receipt = create(h)
    assert receipt.status == "unknown" and receipt.provider_id is None
