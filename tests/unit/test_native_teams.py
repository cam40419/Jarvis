"""Real machine principals, bounded staffing and revocation before command replay."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.domain.accounts import ManagedAccount
from simon.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.identity import Membership
from simon.domain.models import ActorContext, Channel
from simon.domain.native_agents import (
    CreateNativeAgent,
    IssueNativeAgentCredential,
    UpdateNativeAgent,
    UpdateNativeTeamPolicy,
)
from simon.domain.native_projects import (
    CreateNativeTask,
    NativeProjectMember,
    PutNativeProjectMember,
    TaskAssignment,
    UpdateNativeProject,
    UpdateNativeTask,
    VersionedNativeCommand,
)
from simon.services.canonical import digest
from simon.services.identity import ROLE_SCOPES
from simon.services.native_projects import NativeProjectService
from simon.services.native_teams import NativeTeamService
from simon.services.scoped_agents import credential_hash
from tests.contract.test_native_project_store import seed_native_projects


def setup_team(store):
    h = seed_native_projects(store)
    h.owner_actor = ActorContext(
        actor_id=h.owner, workspace_id=h.workspace, scopes=ROLE_SCOPES["owner"], channel=Channel.API
    )
    h.projects = NativeProjectService(store)
    h.teams = NativeTeamService(store, h.projects)
    h.authority = h.projects.agent_authority
    return h


@pytest.fixture
def team():
    return setup_team(InMemoryStore())


def create_role(h, *, actor=None, key="research", manager=False):
    return h.teams.create(
        actor or h.owner_actor,
        h.first.id,
        CreateNativeAgent(
            name=key.title(),
            role_key=key,
            instructions="Collect evidence and prepare reviewable work.",
            success_criteria="Sources and review notes accompany the result.",
            rationale="This project needs this responsibility covered.",
            can_manage_team=manager,
            idempotency_key=f"create-{key}",
        ),
    )


def issue(h, agent, *, scopes=None, key=None):
    command = IssueNativeAgentCredential(
        expected_version=agent.version,
        idempotency_key=key or f"issue-{agent.id}-{agent.version}",
        **({"scopes": scopes} if scopes else {}),
    )
    issued = h.teams.issue_credential(h.owner_actor, h.first.id, agent.id, command)
    assert issued.token
    return h.authority.resolve(issued.token), issued, command


def revision(agent, **changes):
    values = agent.model_dump(
        include={
            "name",
            "instructions",
            "success_criteria",
            "rationale",
            "can_manage_team",
            "status",
        }
    )
    return UpdateNativeAgent(
        **{
            **values,
            "expected_version": agent.version,
            "idempotency_key": f"revise-{uuid4()}",
            **changes,
        }
    )


def task_update(task, **changes):
    return UpdateNativeTask(
        **{
            **task.model_dump(include={"title", "description", "status", "assignment"}),
            "expected_version": task.version,
            "idempotency_key": f"task-{uuid4()}",
            **changes,
        }
    )


def test_agent_task_creator_and_human_agent_claim_race(store):
    h = setup_team(store)
    agent = create_role(h)
    machine, _, _ = issue(h, agent)
    task = h.projects.create_task(
        machine,
        h.first.id,
        CreateNativeTask(
            title="Inspect brand source documents", idempotency_key="machine-created-task"
        ),
    )
    assert task.created_by is None
    assert task.created_by_agent_id == agent.id
    assert store.memberships(agent.id) == ()
    assert store.native_project_member(h.workspace, h.first.id, agent.id) is None
    barrier = Barrier(2)
    before = len(store.audit_events(h.workspace))

    def claim(actor):
        barrier.wait(timeout=10)
        try:
            return h.projects.claim_task(
                actor,
                h.first.id,
                task.id,
                VersionedNativeCommand(
                    expected_version=1, idempotency_key=f"claim-{actor.actor_id}"
                ),
            )
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [machine, h.owner_actor]))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].version == 2
    assert winners[0].assignment in (
        TaskAssignment(kind="agent", agent_id=agent.id),
        TaskAssignment(kind="human", actor_id=h.owner),
    )
    assert len(store.audit_events(h.workspace)) == before + 1
    creation = next(
        e for e in store.audit_events(h.workspace) if e.event_type == "native.task.created"
    )
    assert creation.actor_id == agent.id
    assert creation.payload["actor_kind"] == "agent"
    assert creation.payload["credential_id"] == str(machine.credential_id)


def test_concurrent_staffing_respects_default_cap_and_policy_cas(store):
    h = setup_team(store)
    manager = create_role(h, key="coordinator", manager=True)
    machine, _, _ = issue(h, manager, scopes={"board:read", "board:write", "team:manage"})
    for index in range(6):
        create_role(h, actor=machine, key=f"specialist-{index}")
    barrier = Barrier(2)

    def hire(index):
        barrier.wait(timeout=10)
        try:
            return create_role(h, actor=machine, key=f"candidate-{index}")
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(hire, range(2)))
    assert sum(result is not None for result in results) == 1
    assert store.native_active_agent_count(h.workspace, h.first.id) == 8
    winner = next(result for result in results if result is not None)
    assert winner.created_by is None and winner.created_by_agent_id == manager.id
    barrier = Barrier(2)

    def policy(limit):
        barrier.wait(timeout=10)
        try:
            return h.teams.update_policy(
                h.owner_actor,
                h.first.id,
                UpdateNativeTeamPolicy(
                    max_active_agents=limit,
                    agents_can_manage_team=True,
                    expected_version=0,
                    idempotency_key=f"policy-{limit}",
                ),
            )
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        policies = list(pool.map(policy, [9, 10]))
    assert sum(result is not None for result in policies) == 1
    current = store.native_team_policy(h.workspace, h.first.id)
    assert current.version == 1 and current.max_active_agents in {9, 10}
    with pytest.raises(InvalidTransitionError):
        h.teams.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateNativeTeamPolicy(
                max_active_agents=7,
                agents_can_manage_team=True,
                expected_version=1,
                idempotency_key="below-staffing",
            ),
        )


@pytest.mark.parametrize("status", ["paused", "retired"])
def test_pause_retires_authority_releases_open_work_and_preserves_history(team, status):
    h = team
    agent = create_role(h)
    machine, _, _ = issue(h, agent)
    assignment = TaskAssignment(kind="agent", agent_id=agent.id)
    active = h.projects.create_task(
        machine,
        h.first.id,
        CreateNativeTask(title="Active work", idempotency_key="active-task", assignment=assignment),
    )
    active = h.projects.update_task(
        machine, h.first.id, active.id, task_update(active, status="in_progress")
    )
    done = h.projects.create_task(
        machine,
        h.first.id,
        CreateNativeTask(title="Reviewed work", idempotency_key="done-task", assignment=assignment),
    )
    done = h.projects.update_task(machine, h.first.id, done.id, task_update(done, status="done"))
    h.teams.update(h.owner_actor, h.first.id, agent.id, revision(agent, status=status))
    released = h.projects.get_task(h.owner_actor, h.first.id, active.id)
    assert released.assignment.kind == "pool" and released.status == "todo"
    assert released.version == active.version + 1
    assert released.created_by_agent_id == agent.id
    assert h.projects.get_task(h.owner_actor, h.first.id, done.id) == done
    with pytest.raises(AuthenticationError):
        h.projects.update_task(machine, h.first.id, active.id, task_update(active, status="done"))
    with pytest.raises(InvalidTransitionError):
        h.projects.update_task(
            h.owner_actor,
            h.first.id,
            active.id,
            task_update(active, assignment=TaskAssignment(), status="done"),
        )
    assert not h.teams.credentials(h.owner_actor, h.first.id, agent.id)[0].valid


@pytest.mark.parametrize(
    "invalidate",
    [
        "expire",
        "revoke",
        "revise",
        "pause",
        "issuer-demote",
        "issuer-remove",
        "issuer-disable",
        "archive",
    ],
)
def test_current_authority_fences_previously_successful_command_receipts(
    team, monkeypatch, invalidate
):
    h = team
    agent = create_role(h)
    machine, issued, _ = issue(h, agent)
    command = CreateNativeTask(title="Draft a review package", idempotency_key="worker-replay")
    task = h.projects.create_task(machine, h.first.id, command)
    if invalidate == "expire":
        monkeypatch.setattr(
            "simon.services.scoped_agents.utc_now",
            lambda: issued.credential.expires_at + timedelta(seconds=1),
        )
    elif invalidate == "revoke":
        h.teams.revoke_credential(
            h.owner_actor,
            h.first.id,
            agent.id,
            issued.credential.id,
            VersionedNativeCommand(expected_version=1, idempotency_key="revoke-token"),
        )
    elif invalidate in {"revise", "pause"}:
        h.teams.update(
            h.owner_actor,
            h.first.id,
            agent.id,
            revision(
                agent,
                status="paused" if invalidate == "pause" else "active",
                instructions="New reviewed instructions.",
            ),
        )
    elif invalidate == "issuer-demote":
        h.store.put_membership(
            Membership(actor_id=h.owner, workspace_id=h.workspace, role="member")
        )
        h.store.put_native_project_member(
            NativeProjectMember(
                workspace_id=h.workspace, project_id=h.first.id, actor_id=h.owner, role="member"
            )
        )
    elif invalidate == "issuer-remove":
        h.store.delete_membership(h.owner, h.workspace)
    elif invalidate == "issuer-disable":
        h.store.save_managed_account(
            ManagedAccount(
                actor_id=h.owner,
                workspace_id=h.workspace,
                invited_by=h.owner,
                display_name="Disabled issuing owner",
                disabled=True,
            )
        )
    else:
        h.projects.update_project(
            h.owner_actor,
            h.first.id,
            UpdateNativeProject(
                name=h.first.name,
                objective=h.first.objective,
                status="archived",
                expected_version=1,
                idempotency_key="archive-project",
            ),
        )
    events = len(h.store.audit_events(h.workspace))
    with pytest.raises(AuthenticationError):
        h.projects.create_task(machine, h.first.id, command)
    with pytest.raises(AuthenticationError):
        h.authority.resolve(issued.token)
    assert len(h.store.audit_events(h.workspace)) == events
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == (task,)


@pytest.mark.parametrize("offset,limit", [(-1, 50), (0, 0), (0, 101), (1_000_001, 1)])
def test_team_service_bounds_reads_for_internal_controllers(team, offset, limit):
    # A trusted controller calls services directly, without FastAPI query validation.
    h = team
    before = h.store.audit_events()
    with pytest.raises(ValidationError, match="page bounds"):
        h.teams.view(h.owner_actor, h.first.id, offset, limit)
    assert h.store.audit_events() == before


def test_delegated_manager_cannot_expand_authority_and_policy_is_live(team):
    h = team
    manager = create_role(h, key="coordinator", manager=True)
    machine, _, _ = issue(h, manager, scopes={"board:read", "board:write", "team:manage"})
    child = create_role(h, actor=machine, key="research")
    with pytest.raises(AuthorizationError):
        create_role(h, actor=machine, key="nested-manager", manager=True)
    for target, change in [(manager, {}), (child, {"can_manage_team": True})]:
        with pytest.raises(AuthorizationError):
            h.teams.update(machine, h.first.id, target.id, revision(target, **change))
    with pytest.raises(AuthorizationError):
        h.teams.update_policy(
            machine,
            h.first.id,
            UpdateNativeTeamPolicy(
                max_active_agents=100,
                agents_can_manage_team=True,
                expected_version=0,
                idempotency_key="expand-policy",
            ),
        )
    with pytest.raises(AuthorizationError):
        h.teams.issue_credential(
            machine,
            h.first.id,
            child.id,
            IssueNativeAgentCredential(expected_version=1, idempotency_key="mint-child"),
        )
    with pytest.raises(AuthorizationError):
        h.projects.put_member(
            machine,
            h.first.id,
            PutNativeProjectMember(
                actor_id=h.member, role="owner", expected_version=1, idempotency_key="promote-human"
            ),
        )
    revised = h.teams.update(
        machine,
        h.first.id,
        child.id,
        revision(child, instructions="Collect linked source documents."),
    )
    assert revised.version == 2
    h.teams.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateNativeTeamPolicy(
            max_active_agents=8,
            agents_can_manage_team=False,
            expected_version=0,
            idempotency_key="disable-delegation",
        ),
    )
    with pytest.raises(AuthorizationError):
        create_role(
            h, actor=machine, key="research"
        )  # Even a successful creation receipt is fenced.
    assert h.projects.get_project(machine, h.first.id) == h.first
    assert not h.teams.view(machine, h.first.id).can_manage


def test_read_only_credentials_and_scope_tampering_and_other_projects_are_denied(team):
    h = team
    agent = create_role(h)
    reader, issued, _ = issue(h, agent, scopes={"board:read"})
    assert h.projects.list_projects(reader) == (h.first,)
    assert h.teams.view(reader, h.first.id).agents == (agent,)
    with pytest.raises(AuthorizationError):
        h.projects.create_task(
            reader, h.first.id, CreateNativeTask(title="Unauthorized", idempotency_key="read-write")
        )
    with pytest.raises(AuthenticationError):
        h.projects.create_task(
            reader.model_copy(update={"scopes": frozenset({"board:read", "board:write"})}),
            h.first.id,
            CreateNativeTask(title="Forged authority", idempotency_key="forged-task"),
        )
    for other in [h.second, h.foreign]:
        with pytest.raises(NotFoundError):
            h.projects.get_project(reader, other.id)
        with pytest.raises(NotFoundError):
            h.teams.view(reader, other.id)
    for token in [
        "",
        issued.token[:-1] + ("A" if issued.token[-1] != "A" else "B"),
        f"sagent.{uuid4()}." + "A" * 43,
    ]:
        with pytest.raises(AuthenticationError):
            h.authority.resolve(token)


def test_duplicate_role_keys_require_reuse_even_after_retirement(team):
    h = team
    agent = create_role(h)
    h.teams.update(h.owner_actor, h.first.id, agent.id, revision(agent, status="retired"))
    command = CreateNativeAgent(
        name="Different employee",
        role_key=agent.role_key,
        instructions=agent.instructions,
        success_criteria=agent.success_criteria,
        rationale=agent.rationale,
        idempotency_key="duplicate-responsibility",
    )
    with pytest.raises(InvalidTransitionError):
        h.teams.create(h.owner_actor, h.first.id, command)
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100)[0].status == "retired"


def test_secret_is_once_only_and_absent_from_receipts_audit_and_outbox(store):
    h = setup_team(store)
    agent = create_role(h)
    _, issued, command = issue(h, agent)
    replayed = h.teams.issue_credential(h.owner_actor, h.first.id, agent.id, command)
    assert replayed.token is None and replayed.replayed
    assert replayed.credential == issued.credential
    stored = store.native_agent_credential(issued.credential.id)
    assert stored.token_hash == credential_hash(issued.token)
    metadata = h.teams.credentials(h.owner_actor, h.first.id, agent.id)
    assert "token_hash" not in metadata[0].model_dump()
    receipt, replay = store.execute_once(
        f"native:{h.workspace}:human:{h.owner}:agent:{agent.id}:credential",
        command.idempotency_key,
        digest(command.model_dump(mode="json", exclude={"idempotency_key"})),
        lambda: pytest.fail("Receipt should already exist"),
    )
    assert replay and receipt == {"credential_id": str(issued.credential.id)}
    persisted = json.dumps(
        [
            receipt,
            stored.model_dump(mode="json"),
            [event.model_dump(mode="json") for event in store.audit_events()],
            [event.model_dump(mode="json") for event in store.outbox_events()],
        ]
    )
    assert issued.token not in persisted
    assert issued.token.split(".")[-1] not in persisted


def test_credential_retry_digest_survives_process_hash_seed_changes():
    program = """
import json
from simon.domain.native_agents import IssueNativeAgentCredential
from simon.services.canonical import digest
command = IssueNativeAgentCredential(
    expected_version=1,
    idempotency_key="restart-credential-retry",
    scopes={"board:read", "board:write", "team:manage"},
)
body = command.model_dump(mode="json", exclude={"idempotency_key"})
print(json.dumps({"digest": digest(body), "scopes": body["scopes"]}))
"""
    results = []
    for seed in (1, 2, 3):
        result = subprocess.run(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        results.append(json.loads(result.stdout))
    assert all(result == results[0] for result in results)
    assert set(results[0]["scopes"]) == {"board:read", "board:write", "team:manage"}


@pytest.mark.parametrize("operation", ["create", "issue", "pause", "policy"])
def test_audit_failure_rolls_back_roles_tokens_policy_tasks_and_receipts(
    store, monkeypatch, operation
):
    h = setup_team(store)
    agent = create_role(h)
    machine, _, _ = issue(h, agent)
    task = h.projects.create_task(
        machine,
        h.first.id,
        CreateNativeTask(
            title="Assigned work",
            assignment=TaskAssignment(kind="agent", agent_id=agent.id),
            idempotency_key="rollback-task",
        ),
    )
    old_record = h.projects.audit.record
    before = (
        h.store.native_agents(h.workspace, h.first.id, 0, 100),
        h.store.native_agent_credentials(h.workspace, h.first.id, agent.id),
        h.store.native_team_policy(h.workspace, h.first.id),
        h.store.native_task(h.workspace, h.first.id, task.id),
        h.store.audit_events(),
        h.store.outbox_events(),
    )
    pause = revision(agent, status="paused")

    def change():
        if operation == "create":
            return create_role(h, key="new-role")
        if operation == "issue":
            return h.teams.issue_credential(
                h.owner_actor,
                h.first.id,
                agent.id,
                IssueNativeAgentCredential(expected_version=1, idempotency_key="new-token"),
            )
        if operation == "pause":
            return h.teams.update(h.owner_actor, h.first.id, agent.id, pause)
        return h.teams.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateNativeTeamPolicy(
                expected_version=0,
                max_active_agents=5,
                agents_can_manage_team=False,
                idempotency_key="rollback-policy",
            ),
        )

    def fail_after_write(**kwargs):
        old_record(**kwargs)
        raise RuntimeError("Injected failure after audit and outbox")

    monkeypatch.setattr(h.projects.audit, "record", fail_after_write)
    with pytest.raises(RuntimeError, match="Injected failure"):
        change()
    after = (
        h.store.native_agents(h.workspace, h.first.id, 0, 100),
        h.store.native_agent_credentials(h.workspace, h.first.id, agent.id),
        h.store.native_team_policy(h.workspace, h.first.id),
        h.store.native_task(h.workspace, h.first.id, task.id),
        h.store.audit_events(),
        h.store.outbox_events(),
    )
    assert after == before
    monkeypatch.setattr(h.projects.audit, "record", old_record)
    result = change()
    assert result is not None
    if operation == "issue":
        assert result.token and not result.replayed
