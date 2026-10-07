"""Atomic sharing, optimistic edits and revocation for the native board foundation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.domain.accounts import ManagedAccount
from simon.domain.errors import AuthorizationError, InvalidTransitionError, NotFoundError
from simon.domain.identity import Membership
from simon.domain.models import ActorContext, Channel
from simon.domain.native_projects import (
    CreateNativeProject,
    CreateNativeTask,
    PutNativeProjectMember,
    TaskAssignment,
    UpdateNativeProject,
    UpdateNativeTask,
    VersionedNativeCommand,
)
from simon.services.identity import ROLE_SCOPES
from simon.services.native_projects import NativeProjectService


@pytest.fixture
def board():
    store = InMemoryStore()
    workspace_id = uuid4()

    def actor(role="member"):
        member = Membership(actor_id=uuid4(), workspace_id=workspace_id, role=role)
        store.put_membership(member)
        return ActorContext(
            actor_id=member.actor_id,
            workspace_id=workspace_id,
            scopes=ROLE_SCOPES[role],
            channel=Channel.API,
        )

    owner, colleague, admin = actor(), actor(), actor("owner")
    service = NativeProjectService(store)
    command = CreateNativeProject(
        name="Research pilot",
        objective="Review evidence and coordinate decisions.",
        idempotency_key="research-create",
    )
    project = service.create_project(owner, command)
    service.put_member(
        owner,
        project.id,
        PutNativeProjectMember(
            actor_id=colleague.actor_id,
            expected_version=project.version,
            idempotency_key="colleague-join",
        ),
    )
    return store, service, owner, colleague, admin, service.get_project(owner, project.id), command


def test_simultaneous_pool_claims_have_one_winner_and_one_event(board):
    store, service, owner, colleague, _, project, _ = board
    task = service.create_task(
        owner,
        project.id,
        CreateNativeTask(
            title="Check source evidence",
            idempotency_key="claimable-task",
        ),
    )
    rendezvous = Barrier(2)
    before = len(store.audit_events(owner.workspace_id))

    def claim(actor):
        rendezvous.wait(timeout=5)
        try:
            return service.claim_task(
                actor,
                project.id,
                task.id,
                VersionedNativeCommand(
                    expected_version=1,
                    idempotency_key=f"claim-{actor.actor_id}",
                ),
            )
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, [owner, colleague]))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].version == 2
    assert winners[0].assignment.actor_id in {owner.actor_id, colleague.actor_id}
    assert service.get_task(owner, project.id, task.id) == winners[0]
    assert len(store.audit_events(owner.workspace_id)) == before + 1


def test_audit_failure_rolls_back_task_and_idempotency_receipt(board, monkeypatch):
    store, service, owner, _, _, project, _ = board
    command = CreateNativeTask(title="Draft", idempotency_key="atomic-create")
    before = (len(store.audit_events()), len(store.outbox_events()))
    original = service.audit.record

    def failed_audit(**kwargs):
        original(**kwargs)
        raise RuntimeError("Simulated failure after the outbox event")

    monkeypatch.setattr(service.audit, "record", failed_audit)
    with pytest.raises(RuntimeError, match="Simulated failure"):
        service.create_task(owner, project.id, command)
    assert service.tasks(owner, project.id) == ()
    assert (len(store.audit_events()), len(store.outbox_events())) == before
    monkeypatch.setattr(service.audit, "record", original)
    result = service.create_task(owner, project.id, command)
    assert service.create_task(owner, project.id, command) == result
    assert len(service.tasks(owner, project.id)) == 1
    assert len(store.audit_events()) == before[0] + 1


def test_old_creation_receipt_cannot_bypass_revoked_project_access(board):
    _, service, owner, _, admin, project, command = board
    service.put_member(
        admin,
        project.id,
        PutNativeProjectMember(
            actor_id=admin.actor_id,
            role="owner",
            expected_version=project.version,
            idempotency_key="new-project-owner",
        ),
    )
    latest = service.get_project(admin, project.id)
    service.remove_member(
        admin,
        project.id,
        owner.actor_id,
        VersionedNativeCommand(
            expected_version=latest.version,
            idempotency_key="remove-old-owner",
        ),
    )
    with pytest.raises(NotFoundError):
        service.create_project(owner, command)
    assert service.list_projects(owner) == ()


def test_workspace_revocation_releases_claim_and_cannot_be_undone_by_replay(board):
    store, service, owner, colleague, _, project, _ = board
    task = service.create_task(
        owner,
        project.id,
        CreateNativeTask(
            title="Inspect the draft",
            idempotency_key="revocation-task",
        ),
    )
    claim = VersionedNativeCommand(expected_version=1, idempotency_key="revocation-claim")
    claimed = service.claim_task(colleague, project.id, task.id, claim)
    store.delete_membership(colleague.actor_id, colleague.workspace_id)
    released = service.get_task(owner, project.id, task.id)
    assert released.assignment.kind == "pool"
    assert released.assignment.actor_id is None
    assert released.status == "todo"
    assert released.version == claimed.version + 1
    with pytest.raises(AuthorizationError):
        service.claim_task(colleague, project.id, task.id, claim)
    store.put_membership(
        Membership(
            actor_id=colleague.actor_id,
            workspace_id=colleague.workspace_id,
            role="member",
        )
    )
    with pytest.raises(NotFoundError):
        service.get_task(colleague, project.id, task.id)
    assert service.get_task(owner, project.id, task.id) == released


def test_disabled_account_is_rechecked_inside_service_even_with_old_actor(board):
    store, service, owner, colleague, _, project, _ = board
    store.save_managed_account(
        ManagedAccount(
            actor_id=colleague.actor_id,
            workspace_id=colleague.workspace_id,
            invited_by=owner.actor_id,
            display_name="Disabled colleague",
            disabled=True,
        )
    )
    with pytest.raises(AuthorizationError):
        service.tasks(colleague, project.id)
    with pytest.raises(InvalidTransitionError, match="Assignee"):
        service.create_task(
            owner,
            project.id,
            CreateNativeTask(
                title="Private review",
                idempotency_key="disabled-assignee",
                assignment=TaskAssignment(kind="human", actor_id=colleague.actor_id),
            ),
        )


def test_stale_edit_preserves_winner_and_does_not_write_an_audit_event(board):
    store, service, owner, colleague, _, project, _ = board
    task = service.create_task(
        owner,
        project.id,
        CreateNativeTask(
            title="Original title",
            idempotency_key="concurrent-create",
        ),
    )
    winner = service.update_task(
        colleague,
        project.id,
        task.id,
        UpdateNativeTask(
            title="Reviewed title",
            description="Keep this edit",
            status="in_review",
            assignment=TaskAssignment(),
            expected_version=1,
            idempotency_key="winning-edit",
        ),
    )
    count = len(store.audit_events())
    with pytest.raises(InvalidTransitionError, match="changed"):
        service.update_task(
            owner,
            project.id,
            task.id,
            UpdateNativeTask(
                title="Stale title",
                status="done",
                assignment=TaskAssignment(),
                expected_version=1,
                idempotency_key="stale-edit",
            ),
        )
    assert service.get_task(owner, project.id, task.id) == winner
    assert len(store.audit_events()) == count


def test_members_cannot_manage_access_or_archive_and_last_owner_is_retained(board):
    _, service, owner, colleague, _, project, _ = board
    with pytest.raises(AuthorizationError):
        service.put_member(
            colleague,
            project.id,
            PutNativeProjectMember(
                actor_id=colleague.actor_id,
                role="owner",
                expected_version=project.version,
                idempotency_key="escalate-ownership",
            ),
        )
    with pytest.raises(AuthorizationError):
        service.update_project(
            colleague,
            project.id,
            UpdateNativeProject(
                name=project.name,
                objective=project.objective,
                status="archived",
                expected_version=project.version,
                idempotency_key="archive-as-member",
            ),
        )
    with pytest.raises(InvalidTransitionError, match="another project owner"):
        service.remove_member(
            owner,
            project.id,
            owner.actor_id,
            VersionedNativeCommand(
                expected_version=project.version,
                idempotency_key="remove-last-owner",
            ),
        )
    assert service.get_project(owner, project.id) == project


def test_member_removal_requires_explicit_task_reassignment(board):
    _, service, owner, colleague, _, project, _ = board
    task = service.create_task(
        owner,
        project.id,
        CreateNativeTask(
            title="Owned work",
            idempotency_key="assigned-create",
            assignment=TaskAssignment(kind="human", actor_id=colleague.actor_id),
        ),
    )
    removal = VersionedNativeCommand(
        expected_version=project.version,
        idempotency_key="assigned-remove",
    )
    with pytest.raises(InvalidTransitionError, match="Reassign"):
        service.remove_member(owner, project.id, colleague.actor_id, removal)
    service.update_task(
        owner,
        project.id,
        task.id,
        UpdateNativeTask(
            title=task.title,
            assignment=TaskAssignment(),
            status="todo",
            expected_version=1,
            idempotency_key="release-assignment",
        ),
    )
    service.remove_member(owner, project.id, colleague.actor_id, removal)
    with pytest.raises(NotFoundError):
        service.tasks(colleague, project.id)


def test_archived_project_is_readable_and_blocks_new_board_work(board):
    _, service, owner, _, _, project, _ = board
    archived = service.update_project(
        owner,
        project.id,
        UpdateNativeProject(
            name=project.name,
            objective=project.objective,
            status="archived",
            expected_version=project.version,
            idempotency_key="archive-project",
        ),
    )
    assert service.get_project(owner, project.id) == archived
    with pytest.raises(InvalidTransitionError, match="archived"):
        service.create_task(
            owner,
            project.id,
            CreateNativeTask(
                title="Work while archived",
                idempotency_key="archived-new-task",
            ),
        )
    restored = service.update_project(
        owner,
        project.id,
        UpdateNativeProject(
            name=project.name,
            objective=project.objective,
            status="active",
            expected_version=archived.version,
            idempotency_key="restore-project",
        ),
    )
    assert restored.status == "active"


def test_workspace_admin_can_recover_ownerless_project_without_legacy_access(board):
    store, service, owner, colleague, admin, project, _ = board
    store.delete_membership(owner.actor_id, owner.workspace_id)
    remaining = service.get_project(admin, project.id)
    assert remaining.created_by == project.created_by
    assert remaining.created_at == project.created_at
    assert remaining.version == project.version + 1
    service.put_member(
        admin,
        project.id,
        PutNativeProjectMember(
            actor_id=colleague.actor_id,
            role="owner",
            expected_version=remaining.version,
            idempotency_key="recover-ownership",
        ),
    )
    assert (
        next(m for m in service.members(admin, project.id) if m.actor_id == colleague.actor_id).role
        == "owner"
    )


def test_inactive_coowner_does_not_allow_removing_last_effective_owner(board):
    store, service, owner, colleague, _, project, _ = board
    service.put_member(
        owner,
        project.id,
        PutNativeProjectMember(
            actor_id=colleague.actor_id,
            role="owner",
            expected_version=project.version,
            idempotency_key="promote-coowner",
        ),
    )
    store.put_membership(
        Membership(
            actor_id=colleague.actor_id,
            workspace_id=colleague.workspace_id,
            role="guest",
        )
    )
    latest = service.get_project(owner, project.id)
    with pytest.raises(InvalidTransitionError, match="another project owner"):
        service.remove_member(
            owner,
            project.id,
            owner.actor_id,
            VersionedNativeCommand(
                expected_version=latest.version,
                idempotency_key="remove-effective-owner",
            ),
        )
    event = store.audit_events()[-1]
    assert event.payload["member_id"] == str(colleague.actor_id)
    assert event.payload["role"] == "owner"
