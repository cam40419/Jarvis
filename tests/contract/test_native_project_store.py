"""Shared project persistence, relational isolation, revocation and atomic updates."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.identity import Membership
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_projects import (
    NativeProject,
    NativeProjectMember,
    NativeTask,
    TaskAssignment,
)
from simon.services.audit import AuditService
from simon.services.canonical import digest


def seed_native_projects(store):
    workspace, foreign_workspace = uuid4(), uuid4()
    owner, member, outsider = uuid4(), uuid4(), uuid4()
    for actor_id, workspace_id, role in (
        (owner, workspace, "owner"),
        (member, workspace, "member"),
        (outsider, foreign_workspace, "owner"),
    ):
        store.put_membership(Membership(actor_id=actor_id, workspace_id=workspace_id, role=role))
    first = NativeProject(
        workspace_id=workspace,
        name="Shared project",
        objective="Produce reviewed work",
        created_by=owner,
    )
    second = first.model_copy(
        update={
            "id": uuid4(),
            "name": "Other project",
            "created_at": first.created_at + timedelta(seconds=1),
        }
    )
    foreign = NativeProject(
        workspace_id=foreign_workspace,
        name="Other workspace",
        objective="Keep work isolated",
        created_by=outsider,
    )
    for project in (first, second, foreign):
        store.insert_native_project(project)
        store.put_native_project_member(
            NativeProjectMember(
                workspace_id=project.workspace_id,
                project_id=project.id,
                actor_id=project.created_by,
                role="owner",
            )
        )
    return SimpleNamespace(
        store=store,
        workspace=workspace,
        foreign_workspace=foreign_workspace,
        owner=owner,
        member=member,
        outsider=outsider,
        first=first,
        second=second,
        foreign=foreign,
    )


@pytest.fixture
def native(store):
    return seed_native_projects(store)


def member_record(h, actor_id=None, project=None):
    return NativeProjectMember(
        workspace_id=h.workspace,
        project_id=(project or h.first).id,
        actor_id=actor_id or h.member,
    )


def task_record(h, **changes):
    return NativeTask(
        workspace_id=h.workspace,
        project_id=h.first.id,
        title="Prepare a review packet",
        created_by=h.owner,
        **changes,
    )


def test_native_project_and_task_queries_are_shared_and_scoped(native):
    h = native
    assert h.store.native_projects(h.workspace, h.member, 0, 10) == ()
    h.store.put_native_project_member(member_record(h))
    assert h.store.native_projects(h.workspace, h.member, 0, 10) == (h.first,)
    assert h.store.native_projects(h.workspace, h.owner, 0, 1) == (h.second,)
    assert h.store.native_projects(h.workspace, h.owner, 1, 1) == (h.first,)
    assert h.store.native_projects(h.workspace, h.member, 0, 10, all_projects=True) == (
        h.second,
        h.first,
    )
    assert h.store.native_project(h.foreign_workspace, h.first.id) is None
    assert h.store.native_project_members(h.foreign_workspace, h.first.id) == ()
    assert h.store.native_project_member(h.foreign_workspace, h.first.id, h.owner) is None
    first = task_record(h)
    second = task_record(h, created_at=first.created_at + timedelta(seconds=1))
    h.store.insert_native_task(first)
    h.store.insert_native_task(second)
    assert h.store.native_task(h.workspace, h.first.id, first.id) == first
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 1) == (first,)
    assert h.store.native_tasks(h.workspace, h.first.id, 1, 1) == (second,)
    assert h.store.native_task(h.foreign_workspace, h.first.id, first.id) is None
    assert h.store.native_task(h.workspace, h.second.id, first.id) is None
    assert h.store.native_tasks(h.workspace, h.second.id, 0, 10) == ()
    assert h.store.native_tasks(h.foreign_workspace, h.first.id, 0, 10) == ()


def test_member_and_task_references_cannot_cross_project_or_workspace(native):
    h = native
    for invalid_member in (
        member_record(h).model_copy(update={"project_id": h.foreign.id}),
        member_record(h, actor_id=h.outsider),
        member_record(h, actor_id=uuid4()),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.put_native_project_member(invalid_member)
    h.store.put_native_project_member(member_record(h, project=h.second))
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_task(
            task_record(h, assignment=TaskAssignment(kind="human", actor_id=h.member))
        )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_task(task_record(h).model_copy(update={"project_id": h.foreign.id}))
    h.store.put_native_project_member(member_record(h))
    assigned = task_record(h, assignment=TaskAssignment(kind="human", actor_id=h.member))
    h.store.insert_native_task(assigned)
    assert h.store.native_assigned_task_exists(h.workspace, h.first.id, h.member)
    assert not h.store.native_assigned_task_exists(h.workspace, h.second.id, h.member)
    assert not h.store.native_assigned_task_exists(h.foreign_workspace, h.first.id, h.member)
    with pytest.raises(InvalidTransitionError):
        h.store.delete_native_project_member(h.workspace, h.first.id, h.member)
    assert h.store.native_project_member(h.workspace, h.first.id, h.member) is not None


def test_member_role_update_preserves_join_date_and_scoped_delete(native):
    h = native
    member = member_record(h)
    h.store.put_native_project_member(member)
    h.store.put_native_project_member(
        member.model_copy(update={"role": "owner", "created_at": utc_now() + timedelta(days=1)})
    )
    saved = h.store.native_project_member(h.workspace, h.first.id, h.member)
    assert saved == member.model_copy(update={"role": "owner"})
    h.store.delete_native_project_member(h.foreign_workspace, h.first.id, h.member)
    assert h.store.native_project_member(h.workspace, h.first.id, h.member) == saved
    h.store.delete_native_project_member(h.workspace, h.first.id, h.member)
    assert h.store.native_project_member(h.workspace, h.first.id, h.member) is None


def test_optimistic_updates_preserve_scope_and_immutable_provenance(native):
    h = native
    task = task_record(h)
    h.store.insert_native_task(task)
    for changes in (
        {"version": 3},
        {"workspace_id": h.foreign_workspace},
        {"created_by": h.member},
        {"created_at": utc_now() + timedelta(days=1)},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_native_project(h.first.model_copy(update={"version": 2, **changes}), 1)
        with pytest.raises(InvalidTransitionError):
            h.store.update_native_task(task.model_copy(update={"version": 2, **changes}), 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_task(
            task.model_copy(update={"version": 2, "project_id": h.second.id}), 1
        )
    invalid_assignee = task.model_copy(
        update={"version": 2, "assignment": TaskAssignment(kind="human", actor_id=h.member)}
    )
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_task(invalid_assignee, 1)
    changed_project = h.first.model_copy(update={"version": 2, "name": "Updated project"})
    changed_task = task.model_copy(update={"version": 2, "title": "Updated task"})
    h.store.update_native_project(changed_project, 1)
    h.store.update_native_task(changed_task, 1)
    assert h.store.native_project(h.workspace, h.first.id) == changed_project
    assert h.store.native_task(h.workspace, h.first.id, task.id) == changed_task
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_project(changed_project, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_task(changed_task, 1)


def test_inserts_cannot_overwrite_existing_ids_or_skip_initial_version(native):
    h = native
    task = task_record(h)
    h.store.insert_native_task(task)
    for project in (
        h.first.model_copy(update={"name": "Overwrite"}),
        h.first.model_copy(update={"id": uuid4(), "version": 2}),
        h.first.model_copy(update={"id": uuid4(), "created_by": h.outsider}),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_native_project(project)
    for candidate in (
        task.model_copy(update={"title": "Overwrite"}),
        task.model_copy(update={"id": uuid4(), "version": 2}),
        task.model_copy(update={"id": uuid4(), "created_by": h.outsider}),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_native_task(candidate)
    assert h.store.native_project(h.workspace, h.first.id) == h.first
    assert h.store.native_task(h.workspace, h.first.id, task.id) == task


def test_concurrent_task_update_has_one_winner(native):
    h = native
    original = task_record(h)
    h.store.insert_native_task(original)

    def edit(number):
        candidate = original.model_copy(update={"version": 2, "title": f"Editor {number}"})
        try:
            h.store.update_native_task(candidate, 1)
        except InvalidTransitionError:
            return None
        return candidate

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(edit, range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert h.store.native_task(h.workspace, h.first.id, original.id) == winners[0]


def test_project_member_task_audit_and_receipt_rollback_together(native):
    h = native
    project = h.first.model_copy(update={"id": uuid4()})
    member = member_record(h, project=project)
    task = task_record(h).model_copy(update={"project_id": project.id})
    actor = ActorContext(actor_id=h.owner, workspace_id=h.workspace, channel=Channel.API)
    before_audit = h.store.audit_events(h.workspace)
    before_outbox = h.store.outbox_events()
    with pytest.raises(RuntimeError, match="rollback"), h.store.transaction(h.workspace):
        h.store.insert_native_project(project)
        h.store.put_native_project_member(member)
        h.store.insert_native_task(task)
        h.store.execute_once("native-rollback", "request-one", digest({}), lambda: {"ok": True})
        AuditService(h.store).record(
            event_type="native.test",
            actor=actor,
            resource_type="native_project",
            resource_id=str(project.id),
            payload={},
        )
        raise RuntimeError("rollback")
    assert h.store.native_project(h.workspace, project.id) is None
    assert h.store.native_project_members(h.workspace, project.id) == ()
    assert h.store.native_tasks(h.workspace, project.id, 0, 10) == ()
    assert h.store.audit_events(h.workspace) == before_audit
    assert h.store.outbox_events() == before_outbox
    assert h.store.execute_once(
        "native-rollback", "request-one", digest({}), lambda: {"ok": False}
    ) == ({"ok": False}, False)


def test_workspace_revocation_preserves_work_and_pools_only_revoked_assignments(native):
    h = native
    h.store.put_native_project_member(member_record(h))
    assignment = TaskAssignment(kind="human", actor_id=h.member)
    running = task_record(h, status="in_progress", assignment=assignment)
    review = task_record(h, status="in_review", assignment=assignment)
    other = task_record(h, assignment=TaskAssignment(kind="human", actor_id=h.owner))
    for task in (running, review, other):
        h.store.insert_native_task(task)
    h.store.delete_membership(h.member, h.workspace)
    assert h.store.memberships(h.member) == ()
    assert h.store.native_project_member(h.workspace, h.first.id, h.member) is None
    for original, status in ((running, "todo"), (review, "in_review")):
        saved = h.store.native_task(h.workspace, h.first.id, original.id)
        assert saved.assignment == TaskAssignment()
        assert saved.status == status
        assert saved.version == 2
        assert saved.created_by == original.created_by
    assert h.store.native_task(h.workspace, h.first.id, other.id) == other
    saved_project = h.store.native_project(h.workspace, h.first.id)
    assert saved_project == h.first.model_copy(
        update={"version": 2, "updated_at": saved_project.updated_at}
    )
    assert h.store.native_project(h.workspace, h.second.id) == h.second
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_project(h.first.model_copy(update={"version": 2}), 1)


def test_workspace_revocation_rolls_back_atomically(native):
    h = native
    member = member_record(h)
    h.store.put_native_project_member(member)
    task = task_record(h, assignment=TaskAssignment(kind="human", actor_id=h.member))
    h.store.insert_native_task(task)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.delete_membership(h.member, h.workspace)
        raise RuntimeError("rollback revocation")
    assert h.store.memberships(h.member)
    assert h.store.native_project_member(h.workspace, h.first.id, h.member) == member
    assert h.store.native_task(h.workspace, h.first.id, task.id) == task
    assert h.store.native_project(h.workspace, h.first.id) == h.first


def test_revoking_project_creator_preserves_history_and_other_workspace_assignments(native):
    h = native
    h.store.put_membership(
        Membership(actor_id=h.owner, workspace_id=h.foreign_workspace, role="member")
    )
    h.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=h.foreign_workspace, project_id=h.foreign.id, actor_id=h.owner
        )
    )
    local_task = task_record(h, assignment=TaskAssignment(kind="human", actor_id=h.owner))
    foreign_task = NativeTask(
        workspace_id=h.foreign_workspace,
        project_id=h.foreign.id,
        title="Keep this workspace assignment",
        created_by=h.outsider,
        assignment=TaskAssignment(kind="human", actor_id=h.owner),
    )
    h.store.insert_native_task(local_task)
    h.store.insert_native_task(foreign_task)
    h.store.delete_membership(h.owner, h.workspace)
    saved_project = h.store.native_project(h.workspace, h.first.id)
    assert saved_project == h.first.model_copy(
        update={"version": 2, "updated_at": saved_project.updated_at}
    )
    assert h.store.native_project(h.workspace, h.second.id).version == 2
    assert h.store.native_project(h.foreign_workspace, h.foreign.id) == h.foreign
    saved = h.store.native_task(h.workspace, h.first.id, local_task.id)
    assert saved.created_by == h.owner and saved.assignment == TaskAssignment()
    assert h.store.native_task(h.foreign_workspace, h.foreign.id, foreign_task.id) == foreign_task
    assert h.store.native_project_member(h.foreign_workspace, h.foreign.id, h.owner) is not None
    assert {m.workspace_id for m in h.store.memberships(h.owner)} == {h.foreign_workspace}
    h.store.delete_membership(h.owner, h.workspace)
    assert h.store.native_project(h.workspace, h.first.id) == saved_project


@pytest.mark.postgres
def test_native_records_survive_new_postgres_store(postgres_url):
    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url)
    try:
        h = seed_native_projects(store)
        member = member_record(h)
        store.put_native_project_member(member)
        task = task_record(h, assignment=TaskAssignment(kind="human", actor_id=h.member))
        store.insert_native_task(task)
    finally:
        store.close()
    restored = PostgresStore(postgres_url)
    try:
        assert restored.native_project(h.workspace, h.first.id) == h.first
        assert restored.native_project_member(h.workspace, h.first.id, h.member) == member
        assert restored.native_task(h.workspace, h.first.id, task.id) == task
    finally:
        restored.close()


@pytest.mark.postgres
def test_database_foreign_keys_reject_cross_scope_rows(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url)
    try:
        h = seed_native_projects(store)
        task = task_record(h)
        store.insert_native_task(task)
        statements = (
            (
                "INSERT INTO native_project_members "
                "(workspace_id,project_id,actor_id,role,created_at) "
                "VALUES (%s,%s,%s,'member',now())",
                (h.workspace, h.foreign.id, h.member),
            ),
            (
                "INSERT INTO native_project_members "
                "(workspace_id,project_id,actor_id,role,created_at) "
                "VALUES (%s,%s,%s,'member',now())",
                (h.workspace, h.first.id, h.outsider),
            ),
            ("UPDATE native_tasks SET project_id=%s WHERE id=%s", (h.foreign.id, task.id)),
            (
                "UPDATE native_tasks SET assignment_kind='human',assignee_actor_id=%s WHERE id=%s",
                (h.member, task.id),
            ),
        )
        for statement, parameters in statements:
            with pytest.raises(psycopg.errors.ForeignKeyViolation), store.transaction():
                store.connection.execute(statement, parameters)
        with pytest.raises(psycopg.errors.CheckViolation), store.transaction():
            store.connection.execute(
                "UPDATE native_tasks SET assignment_kind='human' WHERE id=%s", (task.id,)
            )
        assert store.native_task(h.workspace, h.first.id, task.id) == task
    finally:
        store.close()
