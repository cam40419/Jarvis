from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError as SchemaError

from simon.config import Settings
from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.home import HomeStatus
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.workflows import (
    ControlWorkflow,
    SaveWorkflow,
    SaveWorkflowTrigger,
    StartWorkflow,
    WorkflowSpec,
)
from simon.services.identity import ROLE_SCOPES, IdentityService
from simon.services.workflows import WorkflowService
from tests.contract.test_home_tools import home_setup as fixture_home_setup


@pytest.fixture
def workflow_setup(store):
    member = Membership(actor_id=DEV_ACTOR_ID, household_id=DEV_HOUSEHOLD_ID, role="owner")
    store.put_membership(member)
    actor = ActorContext(
        actor_id=member.actor_id,
        household_id=member.household_id,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )
    now = [utc_now()]
    service = WorkflowService(
        store, IdentityService(store, Settings(_env_file=None)), lambda: now[0]
    )
    return service, actor, now


def definition(service, actor, steps=None):
    return service.save(
        actor,
        SaveWorkflow(
            spec=WorkflowSpec(
                name="Demo",
                steps=steps
                or [
                    {"id": "first", "action": "system.echo", "inputs": {"message": "hello"}},
                    {"id": "wait", "kind": "wait", "depends_on": ["first"], "delay_seconds": 10},
                    {"id": "last", "action": "home.inventory", "depends_on": ["wait"]},
                ],
            ),
            idempotency_key=str(uuid4()),
        ),
    )


def start(service, actor, saved, **kwargs):
    return service.start(
        actor,
        saved.id,
        StartWorkflow(definition_version=saved.version, idempotency_key=str(uuid4()), **kwargs),
    )


def control(service, actor, identifier, action):
    run = service.get(actor, identifier)
    return service.control(
        actor, identifier, ControlWorkflow(action=action, expected_version=run.version)
    )


def test_one_time_light_schedule_dispatches_once_with_stable_receipt(store, tmp_path):
    connected, actor, _ = fixture_home_setup.__wrapped__(store, tmp_path)
    now = [utc_now()]
    service = WorkflowService(
        store, connected.identity, lambda: now[0], home=connected.home
    )
    writes = []
    device_state = {"on": True}

    def set_device(device, change):
        writes.append((device.id, change.on))
        device_state["on"] = change.on

    connected.home.lifx.set = set_device
    connected.home.lifx.read = lambda device: HomeStatus(
        device_id=device.id, on=device_state["on"], online=True, capabilities=("power",)
    )
    saved = service.save(actor, SaveWorkflow(
        spec=WorkflowSpec(name="Evening beam", steps=[
            {"id": "light", "action": "home.set",
             "inputs": {"device_id": "office-beam", "on": True}},
        ]),
        idempotency_key=str(uuid4()),
    ))
    run = start(service, actor, saved, start_at=now[0] + timedelta(minutes=2))
    assert service.tick("scheduler") == 0 and writes == []
    now[0] += timedelta(minutes=2)
    claim = service.claim(run.id, "scheduler")
    assert claim is not None
    first = service.execute(claim)
    second = service.execute(claim)
    assert first == second and first["status"] == "succeeded"
    assert writes == [("office-beam", True)]
    assert service.finish(claim, first)
    service.tick("scheduler")
    assert service.get(actor, run.id).status == "succeeded"


def test_print_trigger_runs_device_until_print_finishes_plus_delay(store, tmp_path):
    connected, actor, _ = fixture_home_setup.__wrapped__(store, tmp_path)
    now = [utc_now()]
    printer_state = {"configured": True, "online": True, "state": "FINISH"}

    class Printer:
        def status(self, _actor):
            return printer_state.copy()

    service = WorkflowService(
        store, connected.identity, lambda: now[0], home=connected.home, printer=Printer()
    )
    writes = []
    device_state = {"on": True}

    def set_device(device, change):
        writes.append((device.id, change.on))
        device_state["on"] = change.on

    connected.home.lifx.set = set_device
    connected.home.lifx.read = lambda device: HomeStatus(
        device_id=device.id, on=device_state["on"], online=True, capabilities=("power",)
    )
    saved = service.save(actor, SaveWorkflow(
        spec=WorkflowSpec(
            name="Print air cleaning",
            steps=[
                {"id": "purifier_on", "action": "home.set",
                 "inputs": {"device_id": "office-beam", "on": True}},
                {"id": "print_done", "kind": "condition",
                 "condition": "printer.not_printing", "depends_on": ["purifier_on"]},
                {"id": "clear_air", "kind": "wait", "delay_seconds": 600,
                 "depends_on": ["print_done"]},
                {"id": "purifier_off", "action": "home.set",
                 "inputs": {"device_id": "office-beam", "on": False},
                 "depends_on": ["clear_air"]},
            ],
        ),
        idempotency_key=str(uuid4()),
    ))
    service.create_trigger(actor, saved.id, SaveWorkflowTrigger(
        definition_version=saved.version,
        trigger={"kind": "printer.print_started"},
        idempotency_key=str(uuid4()),
    ))
    trigger = service.triggers(actor, 0, 10)[0]
    assert trigger.definition_id == saved.id and trigger.enabled

    assert service.tick("printer-trigger") == 0
    printer_state["state"] = "RUNNING"
    now[0] += timedelta(seconds=10)
    assert service.tick("printer-trigger") == 1
    assert writes == [("office-beam", True)]
    service.tick("printer-trigger")
    assert len(service.runs(actor, 0, 10)) == 1

    now[0] += timedelta(seconds=10)
    printer_state["state"] = "FINISH"
    service.tick("printer-trigger")
    now[0] += timedelta(seconds=5)
    service.tick("printer-trigger")
    service.tick("printer-trigger")
    assert writes == [("office-beam", True)]

    now[0] += timedelta(minutes=10)
    service.tick("printer-trigger")
    assert service.tick("printer-trigger") == 1
    service.tick("printer-trigger")
    service.tick("printer-trigger")
    assert writes == [("office-beam", True), ("office-beam", False)]
    final_run = service.runs(actor, 0, 10)[0]
    assert final_run.status == "succeeded", (
        [(step.step.id, step.status, step.due_at) for step in final_run.steps],
        final_run.next_wake_at,
    )


def test_device_power_off_state_can_trigger_a_custom_workflow(store, tmp_path):
    connected, actor, _ = fixture_home_setup.__wrapped__(store, tmp_path)
    now = [utc_now()]
    device_state = {"on": False}
    connected.home.lifx.read = lambda device: HomeStatus(
        device_id=device.id,
        on=device_state["on"],
        online=True,
        capabilities=("power",),
    )
    service = WorkflowService(
        store, connected.identity, lambda: now[0], home=connected.home
    )
    saved = service.save(actor, SaveWorkflow(
        spec=WorkflowSpec(
            name="Device switched off",
            steps=[{"id": "record", "action": "system.echo"}],
        ),
        idempotency_key=str(uuid4()),
    ))
    service.create_trigger(actor, saved.id, SaveWorkflowTrigger(
        definition_version=saved.version,
        trigger={
            "kind": "device.state",
            "device_id": "office-beam",
            "field": "on",
            "operator": "equals",
            "value": False,
        },
        idempotency_key=str(uuid4()),
    ))

    assert service.tick("device-trigger") == 0
    assert service.runs(actor, 0, 10) == ()
    now[0] += timedelta(seconds=10)
    device_state["on"] = True
    assert service.tick("device-trigger") == 0
    assert service.runs(actor, 0, 10) == ()
    now[0] += timedelta(seconds=10)
    device_state["on"] = False
    assert service.tick("device-trigger") == 1
    service.tick("device-trigger")
    assert len(service.runs(actor, 0, 10)) == 1
    assert service.runs(actor, 0, 10)[0].definition_id == saved.id

    now[0] += timedelta(seconds=10)
    service.tick("device-trigger")
    assert len(service.runs(actor, 0, 10)) == 1


def test_trigger_can_enter_workflow_at_a_specific_step(workflow_setup):
    service, actor, now = workflow_setup
    printer_state = {"online": True, "state": "FINISH"}

    class Printer:
        def status(self, _actor):
            return printer_state.copy()

    service.printer = Printer()
    saved = definition(service, actor, steps=[
        {"id": "first", "action": "system.echo", "inputs": {"message": "skip"}},
        {"id": "second", "action": "system.echo", "inputs": {"message": "run"},
         "depends_on": ["first"]},
        {"id": "third", "action": "system.echo", "inputs": {"message": "continue"},
         "depends_on": ["second"]},
    ])
    trigger = service.create_trigger(actor, saved.id, SaveWorkflowTrigger(
        definition_version=saved.version,
        trigger={"kind": "printer.print_started"},
        start_step_id="second",
        idempotency_key=str(uuid4()),
    ))
    assert trigger.start_step_id == "second"

    service.tick("targeted-trigger")
    printer_state["state"] = "RUNNING"
    now[0] += timedelta(seconds=10)
    assert service.tick("targeted-trigger") == 1
    run = service.runs(actor, 0, 10)[0]
    assert run.steps[0].status == "succeeded"
    assert run.steps[0].result == {"skipped": "trigger_entry"}
    assert run.steps[1].result != {"skipped": "trigger_entry"}
    assert run.steps[2].status == "pending"


def test_trigger_rejects_unknown_start_step(workflow_setup):
    service, actor, _ = workflow_setup
    saved = definition(service, actor)
    with pytest.raises(ValidationError, match="start step"):
        service.create_trigger(actor, saved.id, SaveWorkflowTrigger(
            definition_version=saved.version,
            trigger={"kind": "printer.print_started"},
            start_step_id="missing",
            idempotency_key=str(uuid4()),
        ))
def test_print_finished_trigger_arms_before_firing(workflow_setup):
    service, actor, now = workflow_setup
    printer_state = {"online": True, "state": "FINISH"}

    class Printer:
        def status(self, _actor):
            return printer_state.copy()

    service.printer = Printer()
    saved = service.save(actor, SaveWorkflow(
        spec=WorkflowSpec(
            name="Print finished",
            steps=[{"id": "record", "action": "system.echo"}],
        ),
        idempotency_key=str(uuid4()),
    ))
    service.create_trigger(actor, saved.id, SaveWorkflowTrigger(
        definition_version=saved.version,
        trigger={"kind": "printer.print_finished"},
        idempotency_key=str(uuid4()),
    ))
    service.tick("finish-trigger")
    assert service.runs(actor, 0, 10) == ()

    now[0] += timedelta(seconds=10)
    printer_state["state"] = "RUNNING"
    service.tick("finish-trigger")
    now[0] += timedelta(seconds=10)
    printer_state["state"] = "FINISH"
    assert service.tick("finish-trigger") == 1
    assert len(service.runs(actor, 0, 10)) == 1


def test_definition_versions_pinned_and_waits_resume_after_restart(workflow_setup):
    service, actor, now = workflow_setup
    saved = definition(service, actor)
    run = start(service, actor, saved)
    changed = service.save(
        actor,
        SaveWorkflow(
            spec=WorkflowSpec(
                name="Edited",
                steps=[
                    {"id": "different", "action": "system.echo"},
                ],
            ),
            expected_version=1,
            idempotency_key=str(uuid4()),
        ),
        saved.id,
    )
    assert changed.version == 2 and service.definition(actor, saved.id, 1) == saved
    assert service.definitions(actor, 0, 10) == (changed,)
    assert service.definitions(actor, 1, 10) == ()
    assert service.tick("one") == 1
    service.tick("one")
    waiting = service.get(actor, run.id)
    assert waiting.status == "waiting" and waiting.next_wake_at == now[0] + timedelta(seconds=10)
    assert waiting.steps[0].result == {"value": {"message": "hello"}}
    assert waiting.definition_version == 1
    assert not service.tick("two")
    from simon.adapters.postgres import PostgresStore

    store = (
        PostgresStore(service.store._database_url)
        if isinstance(service.store, PostgresStore)
        else service.store
    )
    restarted = WorkflowService(
        store, IdentityService(store, Settings(_env_file=None)), lambda: now[0]
    )
    now[0] += timedelta(seconds=10)
    restarted.tick("restart")  # complete wait
    assert restarted.tick("restart") == 1
    restarted.tick("restart")  # finalize run
    done = restarted.get(actor, run.id)
    assert done.status == "succeeded" and done.next_wake_at is None
    assert done.steps[-1].result == {
        "source": "saved_inventory",
        "live_status": False,
        "devices": [],
    }
    assert len(done.steps[0].attempts) == 1
    assert restarted.jobs.get(actor, done.steps[0].attempts[0].job_id).status == "succeeded"
    assert restarted.runs(actor, 0, 10) == (done,)
    events = restarted.events(actor, run.id, 0)
    assert len(events) == done.version and events[-1].type == "succeeded"
    assert restarted.events(actor, run.id, done.version) == ()
    assert restarted.health(actor)["worker_online"]
    now[0] += timedelta(seconds=46)
    assert not restarted.health(actor)["worker_online"]


def test_duplicate_starts_concurrent_claims_and_stale_fencing(workflow_setup):
    service, actor, now = workflow_setup
    saved = definition(service, actor, [{"id": "first", "action": "system.echo"}])
    request = StartWorkflow(definition_version=1, idempotency_key=str(uuid4()))
    with ThreadPoolExecutor(max_workers=3) as pool:
        runs = list(pool.map(lambda _: service.start(actor, saved.id, request), range(3)))
    assert len({r.id for r in runs}) == 1
    run = runs[0]
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda i: service.claim(run.id, str(i)), range(3)))
    claim = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    now[0] += timedelta(seconds=31)
    newer = service.claim(run.id, "recovered")
    assert newer and newer.lease_id != claim.lease_id
    assert not service.finish(claim, {"stale": True})
    assert service.finish(newer, {"fresh": True})
    service.tick("finish")
    result = service.get(actor, run.id)
    assert result.status == "succeeded" and result.steps[0].result == {"fresh": True}
    assert [a.status for a in result.steps[0].attempts] == ["interrupted", "succeeded"]
    assert service.start(actor, saved.id, request) == result
    assert not service.finish(newer, {"replay": True})


def test_pause_cancel_and_access_revocation_in_flight(workflow_setup):
    service, actor, now = workflow_setup
    saved = definition(service, actor)
    run = start(service, actor, saved)
    claim = service.claim(run.id, "worker")
    control(service, actor, run.id, "pause")
    assert service.finish(claim, {"done": True})
    assert service.get(actor, run.id).status == "paused"
    assert not service.tick("worker")
    control(service, actor, run.id, "resume")
    service.tick("worker")
    now[0] += timedelta(seconds=10)
    service.tick("worker")
    second = service.claim(run.id, "worker")
    cancelled = control(service, actor, run.id, "cancel")
    assert cancelled.status == "cancelled"
    assert not service.finish(second, {"late": True})
    assert service.jobs.get(actor, second.steps[-1].attempts[0].job_id).status == "cancelled"
    with pytest.raises(InvalidTransitionError):
        control(service, actor, run.id, "resume")
    other = start(service, actor, saved)
    claim = service.claim(other.id, "worker")
    service.store.delete_membership(actor.actor_id, actor.household_id)
    assert not service.finish(claim, {"private": "must not publish"})
    assert service.store.workflow_run(other.id).status == "needs_attention"
    assert "must not publish" not in service.store.workflow_run(other.id).model_dump_json()
    with pytest.raises(AuthorizationError):
        service.get(actor, other.id)


def test_retry_limit_schedule_deadline_and_dag(workflow_setup, monkeypatch):
    service, actor, now = workflow_setup
    saved = definition(
        service,
        actor,
        [
            {"id": "a", "action": "system.echo"},
            {"id": "b", "action": "system.echo"},
            {"id": "join", "kind": "wait", "depends_on": ["a", "b"]},
        ],
    )
    run = start(service, actor, saved, start_at=now[0] + timedelta(seconds=20))
    assert not service.tick("worker")
    now[0] += timedelta(seconds=20)
    for _ in range(4):
        service.tick("worker")
    assert service.get(actor, run.id).status == "succeeded"
    expiring = start(service, actor, saved, expires_at=now[0] + timedelta(seconds=1))
    now[0] += timedelta(seconds=2)
    service.tick("worker")
    assert service.get(actor, expiring.id).status == "expired"
    deadline = definition(
        service, actor, [{"id": "late", "action": "system.echo", "start_by": now[0]}]
    )
    late = start(service, actor, deadline)
    service.tick("worker")
    assert service.get(actor, late.id).status == "failed"
    assert service.get(actor, late.id).steps[0].status == "failed"
    failing = start(service, actor, saved)

    def fail(_):
        raise ValueError("private exception data")

    monkeypatch.setattr(service, "execute", fail)
    for _ in range(4):
        service.tick("worker")
        now[0] += timedelta(seconds=5)
    failure = service.get(actor, failing.id)
    assert failure.status == "failed" and len(failure.steps[0].attempts) == 3
    assert "private exception data" not in failure.model_dump_json()


def test_scope_conflicts_and_atomic_rollback(workflow_setup, monkeypatch):
    service, actor, now = workflow_setup
    saved = definition(service, actor)
    other = actor.model_copy(update={"actor_id": uuid4()})
    service.store.put_membership(
        Membership(actor_id=other.actor_id, household_id=actor.household_id, role="owner")
    )
    assert not service.definitions(other, 0, 10)
    with pytest.raises(NotFoundError):
        service.definition(other, saved.id)
    with pytest.raises(InvalidTransitionError):
        service.save(
            actor,
            SaveWorkflow(spec=saved.spec, expected_version=0, idempotency_key=str(uuid4())),
            saved.id,
        )
    with pytest.raises(ValidationError):
        start(service, actor, saved, start_at=now[0] - timedelta(seconds=1))
    run = start(service, actor, saved)
    with pytest.raises(NotFoundError):
        service.get(other, run.id)
    with pytest.raises(InvalidTransitionError):
        service.control(actor, run.id, ControlWorkflow(action="cancel", expected_version=99))
    original = service.store.append_workflow_event

    def crash(event):
        original(event)
        raise RuntimeError("crash")

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "append_workflow_event", crash)
        with pytest.raises(RuntimeError):
            service.claim(run.id, "crashing")
    assert service.get(actor, run.id) == run
    assert len(service.events(actor, run.id, 0)) == 1
    assert service.claim(run.id, "recovered")
    job_id = service.get(actor, run.id).steps[0].attempts[0].job_id
    with pytest.raises(NotFoundError):
        service.jobs.get(other, job_id)


@pytest.mark.parametrize(
    "steps",
    [
        [{"id": "a", "action": "home.control"}],
        [{"id": "a", "action": "system.echo", "depends_on": ["b"]}],
        [{"id": "a", "action": "system.echo", "depends_on": ["a"]}],
        [{"id": "a", "action": "system.echo"}, {"id": "a", "action": "system.echo"}],
        [{"id": "a", "kind": "wait", "action": "system.echo"}],
        [{"id": "a", "action": "home.inventory", "inputs": {"execute": "no"}}],
        [{"id": "a", "action": "system.echo", "inputs": {"big": "x" * 4096}}],
        [{"id": "a", "kind": "wait", "not_before": "2026-10-01T12:00:00"}],
    ],
)
def test_workflow_schema_rejects_invalid_graphs_and_unsupported_actions(steps):
    with pytest.raises(SchemaError):
        WorkflowSpec(name="Invalid", steps=steps)
