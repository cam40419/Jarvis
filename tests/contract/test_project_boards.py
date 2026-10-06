"""Durable board mirroring on memory and PostgreSQL; no provider network calls."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.adapters.clickup import task_marker
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.project_board_state import (
    BindProjectBoard,
    BoardBinding,
    ImportBoardTasks,
    PublishBoardTasks,
    ReconcileBoardOperation,
)
from simon.domain.project_boards import (
    BoardConnection,
    BoardList,
    BoardStatus,
    BoardTask,
    BoardTaskPage,
)
from simon.domain.project_work import ProjectCycleUpdate, ProjectTodo, ProjectWorkControl
from simon.domain.tool_catalog import ToolExecutionError
from simon.services.project_boards import ProjectBoardService
from tests.contract.test_project_work import project_work as project_work


class FakeClickUp:
    def __init__(self, h):
        self.h = h
        self.http = SimpleNamespace(environ={"CLICKUP_TOKEN": "pk_synthetic"})
        self.remote = {}
        self.calls = []
        self.fail_create = None
        self.fail_comment_readback = False
        self.fail_reads = False
        self.fail_single_reads = False
        self.readback_pending = False
        self.on_create = None
        self.board = BoardList(
            id="456",
            workspace_id="123",
            name="Launch",
            space_id="789",
            statuses=tuple(
                BoardStatus(status=name, type=kind)
                for name, kind in (
                    ("Open", "open"),
                    ("Working", "custom"),
                    ("Done", "closed"),
                    ("Blocked", "custom"),
                )
            ),
            url="https://app.clickup.com/123/v/li/456",
        )

    def called(self, name, *values):
        lock = getattr(self.h.store, "_lock", None)
        if lock is not None:
            assert not lock._is_owned(), "Provider I/O occurred inside a store transaction"
        self.calls.append((name, *values))

    def task(self, identifier, **changes):
        self.remote[identifier] = BoardTask(
            id=identifier,
            list_id="456",
            workspace_id="123",
            name="Task " + identifier,
            description="Research " + identifier,
            status="Open",
            status_type="open",
            date_updated="1000",
            url="https://app.clickup.com/t/" + identifier,
        ).model_copy(update=changes)
        return self.remote[identifier]

    def change(self, identifier, **changes):
        old = self.remote[identifier]
        self.remote[identifier] = old.model_copy(
            update={
                "date_updated": str(int(old.date_updated) + 1),
                **changes,
            }
        )
        return self.remote[identifier]

    def boards(self, *_):
        self.called("boards")
        return (self.board,)

    def list_metadata(self, *_):
        self.called("list")
        return self.board

    def tasks(self, *_, page=0):
        self.called("tasks", page)
        return BoardTaskPage(tasks=tuple(self.remote.values()), page=page)

    def get_tasks(self, connection, actor, task_ids, *, list_id):
        self.called("batch", tuple(task_ids))
        if self.fail_reads:
            raise ToolExecutionError("Synthetic read rate limit")
        return tuple(self.remote[key] for key in task_ids)

    def get_task(self, connection, actor, task_id, *, list_id):
        self.called("get", task_id)
        if (
            self.fail_reads
            or self.fail_single_reads
            or (self.fail_comment_readback and self.readback_pending)
        ):
            raise ToolExecutionError("Synthetic read failure")
        return self.remote[task_id]

    def create_task(self, connection, actor, list_id, *, name, description, status, marker):
        self.called("create", name)
        if self.on_create:
            self.on_create()
        if self.fail_create == "rejected":
            raise ValidationError("Synthetic required custom field")
        task = self.task(
            "created" + str(len(self.remote) + 1),
            name=name,
            description=description + "\n" + task_marker(marker),
            status=status,
        )
        if self.fail_create == "unknown":
            raise ToolExecutionError("Synthetic lost receipt", unknown=True)
        return task

    def add_dependency(self, connection, actor, task_id, depends_on, *, list_id):
        self.called("dependency", task_id, depends_on)
        self.change(task_id, dependencies=(*self.remote[task_id].dependencies, depends_on))
        return {"accepted": True}

    def update_status(self, connection, actor, task_id, *, list_id, status, expected_updated):
        self.called("status", task_id, status)
        assert self.remote[task_id].date_updated == expected_updated
        return self.change(
            task_id, status=status, status_type="closed" if status == "Done" else "custom"
        )

    def append_comment(self, connection, actor, task_id, *, list_id, text, marker):
        self.called("comment", task_id, text, marker)
        self.readback_pending = True
        return {"id": "42", "date": "1001"}


@pytest.fixture
def board(project_work):
    h = project_work
    h.adapter = FakeClickUp(h)
    h.connection = BoardConnection(
        id="company",
        name="Company",
        enabled=True,
        workspace_id=h.actor.workspace_id,
        actor_ids=frozenset({h.actor.actor_id}),
        credential_env="CLICKUP_TOKEN",
        clickup_workspace_id="123",
        list_ids=frozenset({"456"}),
    )
    h.bridge = ProjectBoardService(
        h.store, h.work, h.adapter, (h.connection,), clock=lambda: h.clock[0]
    )
    h.binding = BoardBinding(
        connection_id="company",
        list_id="456",
        status_map={
            "ready": "Open",
            "running": "Working",
            "done": "Done",
            "blocked": "Blocked",
        },
    )
    h.bound = h.bridge.bind(
        h.actor, h.project_id, BindProjectBoard(expected_version=0, binding=h.binding)
    )
    return h


def add(h, identifier="research", **changes):
    h.work.add_todo(
        h.actor,
        h.project_id,
        ProjectTodo(
            id=identifier,
            title="Research " + identifier,
            objective="Find evidence for " + identifier,
            **changes,
        ),
        idempotency_key="add-task-" + identifier,
    )
    return identifier


def publish(h, identifiers, key="publish-selected"):
    return h.bridge.publish(
        h.actor,
        h.project_id,
        PublishBoardTasks(
            todo_ids=tuple(identifiers),
            expected_version=h.bridge.get(h.actor, h.project_id).version,
            idempotency_key=key,
        ),
    )


def import_remote(h, identifiers, key="import-selected"):
    return h.bridge.import_tasks(
        h.actor,
        h.project_id,
        ImportBoardTasks(
            task_ids=tuple(identifiers),
            expected_version=h.bridge.get(h.actor, h.project_id).version,
            idempotency_key=key,
        ),
    )


def runtime(h, identifier, **changes):
    # Same atomic service path used by a coordinator after the real worker reports.
    with h.store.transaction(h.actor.workspace_id):
        row = h.work._job(h.actor, h.project_id)
        state = h.work.view(row)
        h.work._save(
            row,
            state.model_copy(
                update={
                    "todos": tuple(
                        todo.model_copy(update=changes) if todo.id == identifier else todo
                        for todo in state.todos
                    )
                }
            ),
        )


def test_binding_is_owner_scoped_and_optimistic(board):
    h = board
    assert h.bound.version >= 1
    assert len(h.bridge.connections(h.actor)) == 1
    stranger = h.actor.model_copy(update={"actor_id": uuid4()})
    assert h.bridge.connections(stranger) == []
    with pytest.raises(NotFoundError):
        h.bridge.get(stranger, h.project_id)
    with pytest.raises(AuthorizationError):
        h.bridge.bind(
            h.actor.model_copy(update={"scopes": frozenset({"jobs:read"})}),
            h.project_id,
            BindProjectBoard(expected_version=h.bound.version, binding=h.binding),
        )
    with pytest.raises(InvalidTransitionError):
        h.bridge.bind(
            h.actor, h.project_id, BindProjectBoard(expected_version=0, binding=h.binding)
        )


def test_import_selected_dependencies_atomic_idempotent_and_edit_guard(board):
    h = board
    h.adapter.task("parent")
    h.adapter.task("child", dependencies=("parent",))
    with pytest.raises(ValidationError, match="prerequisite"):
        import_remote(h, ["child"])
    assert not h.work.get(h.actor, h.project_id).todos
    version = h.bridge.get(h.actor, h.project_id).version
    request = ImportBoardTasks(
        task_ids=("child", "parent"),
        expected_version=version,
        idempotency_key="import-both-dependencies",
    )
    state = h.bridge.import_tasks(h.actor, h.project_id, request)
    h.bridge.import_tasks(h.actor, h.project_id, request)
    todos = h.work.get(h.actor, h.project_id).todos
    assert len(todos) == 2 and len(state.mappings) == 2
    child = next(todo for todo in todos if todo.depends_on)
    with pytest.raises(ValidationError, match="ClickUp"):
        h.work.update_todo(
            h.actor,
            h.project_id,
            child.model_copy(update={"title": "Override"}),
            expected_version=h.work.get(h.actor, h.project_id).version,
        )
    assert h.bridge.preview(h.actor, h.project_id).tasks


def test_publish_claims_before_io_and_restarts_without_duplicate_tasks(board):
    h = board
    add(h, "research")
    add(h, "draft", depends_on=("research",))

    def observed():
        pending = h.bridge.get(h.actor, h.project_id).pending_operation_ids
        assert len(pending) == 1
        assert (
            next(
                item for item in h.bridge.operations(h.actor, h.project_id) if item.id == pending[0]
            ).state
            == "running"
        )

    h.adapter.on_create = observed
    state = publish(h, ["research", "draft"])
    assert len(state.mappings) == 2
    assert len([call for call in h.adapter.calls if call[0] == "create"]) == 2
    assert len([call for call in h.adapter.calls if call[0] == "dependency"]) == 1
    publish(h, ["research", "draft"])
    assert len([call for call in h.adapter.calls if call[0] == "create"]) == 2
    with pytest.raises(IdempotencyConflictError):
        publish(h, ["research"])


def test_unknown_create_is_not_replayed_and_requires_verified_attachment(board):
    h = board
    add(h)
    h.adapter.fail_create = "unknown"
    with pytest.raises(ValidationError, match="unknown"):
        publish(h, ["research"])
    state = h.bridge.get(h.actor, h.project_id)
    operation = h.bridge.operations(h.actor, h.project_id)[0]
    assert operation.state == "unknown" and state.pending_operation_ids
    with pytest.raises(ValidationError):
        publish(h, ["research"], "new-publish-request")
    h.adapter.task("wrong")
    request = ReconcileBoardOperation(
        expected_version=state.version,
        operation_id=operation.id,
        resolution="attach",
        remote_id="wrong",
        note="Inspect matching provider reference",
    )
    with pytest.raises(ValidationError, match="reference"):
        h.bridge.reconcile(h.actor, h.project_id, request)
    state = h.bridge.reconcile(
        h.actor, h.project_id, request.model_copy(update={"remote_id": "created1"})
    )
    assert len(state.mappings) == 1 and not state.pending_operation_ids
    assert len([call for call in h.adapter.calls if call[0] == "create"]) == 1


@pytest.mark.parametrize("failure", ["rejected", "unknown"])
def test_reviewed_create_can_be_abandoned_but_never_automatically_republished(board, failure):
    h = board
    add(h)
    h.adapter.fail_create = failure
    with pytest.raises(ValidationError):
        publish(h, ["research"])
    state = h.bridge.get(h.actor, h.project_id)
    op = h.bridge.operations(h.actor, h.project_id)[0]
    state = h.bridge.reconcile(
        h.actor,
        h.project_id,
        ReconcileBoardOperation(
            expected_version=state.version,
            operation_id=op.id,
            resolution="acknowledge",
            note="Inspected the board; abandon this request. I will explicitly publish if needed.",
        ),
    )
    assert state.abandoned_create_todo_ids == ("research",)
    assert not state.mappings and not state.pending_operation_ids
    with pytest.raises(InvalidTransitionError, match="not be replayed"):
        publish(h, ["research"])
    h.adapter.fail_create = None
    state = publish(h, ["research"], "explicit-publish-after-review")
    assert len(state.mappings) == 1 and not state.abandoned_create_todo_ids


def test_completion_survives_idle_sync_and_human_text_edits_and_posts_progress(board):
    h = board
    add(h)
    state = publish(h, ["research"])
    remote_id = state.mappings[0].remote_id
    runtime(
        h, "research", status="done", progress=100, result="Evidence saved locally", run_id=uuid4()
    )
    h.adapter.change(remote_id, name="Human refined title", description="Human business edit")
    h.clock[0] += timedelta(seconds=121)
    assert h.bridge.tick() == 1
    todo = h.work.get(h.actor, h.project_id).todos[0]
    assert (todo.status, todo.progress, todo.title) == ("done", 100, "Human refined title")
    assert todo.result == "Evidence saved locally"
    assert len([call for call in h.adapter.calls if call[0] == "comment"]) == 1
    assert not [call for call in h.adapter.calls if call[0] == "status"]
    assert h.bridge.tick() == 0
    h.clock[0] += timedelta(seconds=121)
    assert h.bridge.tick() == 1
    assert len([call for call in h.adapter.calls if call[0] == "comment"]) == 1


def test_comment_readback_failure_is_unknown_and_review_suppresses_replay(board):
    h = board
    add(h)
    publish(h, ["research"])
    runtime(h, "research", status="done", progress=100, result="Ready")
    h.adapter.fail_comment_readback = True
    with pytest.raises(ValidationError, match="unknown"):
        h.bridge.sync(h.actor, h.project_id)
    state = h.bridge.get(h.actor, h.project_id)
    op = next(item for item in h.bridge.operations(h.actor, h.project_id) if item.kind == "comment")
    assert op.state == "unknown"
    h.adapter.fail_comment_readback = False
    state = h.bridge.reconcile(
        h.actor,
        h.project_id,
        ReconcileBoardOperation(
            expected_version=state.version,
            operation_id=op.id,
            resolution="acknowledge",
            note="Inspected the comment on the provider; keep existing progress and do not repost.",
        ),
    )
    assert not state.pending_operation_ids
    h.bridge.sync(h.actor, h.project_id)
    assert len([call for call in h.adapter.calls if call[0] == "comment"]) == 1


def test_status_opt_in_handles_human_reopen_and_later_execution(board):
    h = board
    h.bridge.bind(
        h.actor,
        h.project_id,
        BindProjectBoard(
            expected_version=h.bound.version,
            binding=h.binding.model_copy(update={"sync_status": True}),
        ),
    )
    add(h)
    state = publish(h, ["research"])
    remote_id = state.mappings[0].remote_id
    runtime(h, "research", status="done", progress=100, run_id=uuid4())
    h.bridge.sync(h.actor, h.project_id)
    assert h.adapter.remote[remote_id].status == "Done"
    h.adapter.change(remote_id, status="Open", status_type="open")
    h.bridge.sync(h.actor, h.project_id)
    assert h.work.get(h.actor, h.project_id).todos[0].status == "todo"
    runtime(h, "research", status="done", progress=100, run_id=uuid4())
    h.bridge.sync(h.actor, h.project_id)
    assert h.adapter.remote[remote_id].status == "Done"
    assert len([call for call in h.adapter.calls if call[0] == "status"]) == 2


def test_read_retry_is_capped_and_batches_only_five_mappings(board):
    h = board
    for number in range(7):
        h.adapter.task("item" + str(number))
    import_remote(h, tuple(h.adapter.remote))
    h.clock[0] += timedelta(seconds=121)
    h.bridge.tick()
    assert len([call for call in h.adapter.calls if call[0] == "batch"][-1][1]) == 5
    h.adapter.fail_reads = True
    for count in range(1, 5):
        h.clock[0] += timedelta(hours=1)
        assert h.bridge.tick() == 1
        state = h.bridge.get(h.actor, h.project_id)
        assert len(state.mappings) == 7 and state.read_failure_count == count
        assert state.blocked_reasons and h.bridge.tick() == 0
    h.clock[0] += timedelta(hours=1)
    assert h.bridge.tick() == 0
    h.adapter.fail_reads = False
    h.bridge.sync(h.actor, h.project_id)
    assert not h.bridge.get(h.actor, h.project_id).blocked_reasons


def test_revoked_live_permission_prevents_provider_write(board):
    h = board
    add(h)
    h.permissions["write"] = False
    with pytest.raises(ValidationError):
        publish(h, ["research"])
    assert not [call for call in h.adapter.calls if call[0] == "create"]
    assert h.bridge.operations(h.actor, h.project_id)[0].state == "failed"


def test_cycle_pause_and_changed_binding_fence_before_auto_publication(board, monkeypatch):
    h = board
    h.bridge.bind(
        h.actor,
        h.project_id,
        BindProjectBoard(
            expected_version=h.bound.version,
            binding=h.binding.model_copy(update={"auto_publish": True}),
        ),
    )
    add(h)
    work = h.work.request_cycle(h.actor, h.project_id, "Research launch", "cycle-board-request")
    cycle = work.active_cycle
    h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        cycle.id,
        lambda state: ProjectCycleUpdate(
            cycle=state.active_cycle.model_copy(
                update={
                    "phase": "planning",
                    "planning_plan_id": uuid4(),
                    "planning_run_id": uuid4(),
                }
            ),
        ),
    )
    h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        cycle.id,
        lambda state: ProjectCycleUpdate(
            cycle=state.active_cycle.model_copy(
                update={"phase": "ready", "execution_plan_id": uuid4()}
            ),
            todos=(state.todos[0].model_copy(update={"cycle_id": cycle.id}),),
        ),
    )
    ready = h.work.get(h.actor, h.project_id)
    h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(
            expected_version=ready.version,
            action="run_ready",
        ),
    )
    cycle = h.work.get(h.actor, h.project_id).active_cycle
    original = h.bridge._operation

    def changed(*args, **kwargs):
        h.bridge.configured_connections = (
            h.connection.model_copy(update={"clickup_workspace_id": "999"}),
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(h.bridge, "_operation", changed)
    with pytest.raises(InvalidTransitionError, match="connection changed"):
        h.bridge.before_execution(h.actor, h.project_id, cycle)
    assert not [call for call in h.adapter.calls if call[0] == "create"]


def test_archive_without_status_change_cancels_mirror_and_skips_external_writes(board):
    h = board
    h.adapter.task("archived-later")
    state = import_remote(h, ["archived-later"])
    h.adapter.change("archived-later", archived=True)
    h.bridge.sync(h.actor, h.project_id)
    todo = h.work.get(h.actor, h.project_id).todos[0]
    assert todo.id == state.mappings[0].todo_id and todo.status == "cancelled"
    assert h.bridge.get(h.actor, h.project_id).mappings[0].remote_archived
    assert not [call for call in h.adapter.calls if call[0] in {"status", "comment"}]


def test_stale_claim_survives_service_restart_and_never_resends(board, monkeypatch):
    h = board
    add(h)
    send = h.adapter.create_task

    def interrupted(*args, **kwargs):
        send(*args, **kwargs)
        raise SystemExit("Simulated process interruption after provider accepted")

    monkeypatch.setattr(h.adapter, "create_task", interrupted)
    with pytest.raises(SystemExit):
        publish(h, ["research"])
    op = h.bridge.operations(h.actor, h.project_id)[0]
    assert op.state == "running"
    h.work = h.reconstruct()
    h.bridge = ProjectBoardService(
        h.store, h.work, h.adapter, (h.connection,), clock=lambda: h.clock[0]
    )
    h.clock[0] += timedelta(seconds=181)
    assert h.bridge.tick() == 1
    assert h.bridge.operations(h.actor, h.project_id)[0].state == "unknown"
    assert h.bridge.get(h.actor, h.project_id).blocked_reasons
    h.clock[0] += timedelta(hours=1)
    assert h.bridge.tick() == 0
    assert len([call for call in h.adapter.calls if call[0] == "create"]) == 1


def test_binding_cannot_change_while_a_durable_write_is_in_flight(board):
    h = board
    add(h)

    def during_send():
        current = h.bridge.get(h.actor, h.project_id)
        with pytest.raises(InvalidTransitionError, match="Settle"):
            h.bridge.bind(
                h.actor,
                h.project_id,
                BindProjectBoard(
                    expected_version=current.version,
                    binding=h.binding.model_copy(update={"sync_progress": False}),
                ),
            )

    h.adapter.on_create = during_send
    state = publish(h, ["research"])
    assert state.binding.sync_progress and len(state.mappings) == 1


def test_paused_cycle_prevents_auto_publish_even_with_saved_execution_approval(board):
    h = board
    h.bridge.bind(
        h.actor,
        h.project_id,
        BindProjectBoard(
            expected_version=h.bound.version,
            binding=h.binding.model_copy(update={"auto_publish": True}),
        ),
    )
    add(h)
    state = h.work.request_cycle(h.actor, h.project_id, "Research", "paused-board-cycle")
    cycle = state.active_cycle
    h.work.control(
        h.actor, h.project_id, ProjectWorkControl(expected_version=state.version, action="pause")
    )
    with pytest.raises(InvalidTransitionError, match="paused"):
        h.bridge.before_execution(h.actor, h.project_id, cycle)
    assert not [call for call in h.adapter.calls if call[0] == "create"]


def test_active_planning_rejects_metadata_import_and_remote_changes_block_execution(board):
    h = board
    h.adapter.task("selected")
    import_remote(h, ["selected"])
    state = h.work.request_cycle(h.actor, h.project_id, "Research", "changed-board-cycle")
    cycle = state.active_cycle
    h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        cycle.id,
        lambda work: ProjectCycleUpdate(
            cycle=work.active_cycle.model_copy(
                update={
                    "phase": "planning",
                    "planning_plan_id": uuid4(),
                    "planning_run_id": uuid4(),
                }
            ),
        ),
    )
    with pytest.raises(InvalidTransitionError, match="settle"):
        h.bridge.sync(h.actor, h.project_id)
    h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        cycle.id,
        lambda work: ProjectCycleUpdate(
            cycle=work.active_cycle.model_copy(
                update={"phase": "ready", "execution_plan_id": uuid4()}
            ),
            todos=(work.todos[0].model_copy(update={"cycle_id": cycle.id}),),
        ),
    )
    ready = h.work.get(h.actor, h.project_id)
    h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(expected_version=ready.version, action="run_ready"),
    )
    h.adapter.change("selected", name="Human changed task after plan")
    with pytest.raises(ValidationError, match="changed during planning"):
        h.bridge.before_execution(
            h.actor, h.project_id, h.work.get(h.actor, h.project_id).active_cycle
        )


def test_concurrent_publish_keys_cannot_create_a_second_mapping(board, monkeypatch):
    h = board
    add(h)
    operation = h.bridge._operation
    competing = [True]

    def interleaved(*args, **kwargs):
        if competing[0]:
            competing[0] = False
            publish(h, ["research"], "concurrent-publish-key")
        return operation(*args, **kwargs)

    monkeypatch.setattr(h.bridge, "_operation", interleaved)
    with pytest.raises(InvalidTransitionError, match="already published"):
        publish(h, ["research"])
    assert len(h.bridge.get(h.actor, h.project_id).mappings) == 1
    assert len([call for call in h.adapter.calls if call[0] == "create"]) == 1


def test_successful_pull_does_not_reset_repeated_progress_preflight_failures(board):
    h = board
    add(h)
    publish(h, ["research"])
    runtime(h, "research", status="done", progress=100)
    h.adapter.fail_single_reads = True
    for count in range(1, 5):
        h.clock[0] += timedelta(hours=1)
        assert h.bridge.tick() == 1
        assert h.bridge.get(h.actor, h.project_id).read_failure_count == count
    h.clock[0] += timedelta(hours=1)
    assert h.bridge.tick() == 0
    assert not [call for call in h.adapter.calls if call[0] == "comment"]
