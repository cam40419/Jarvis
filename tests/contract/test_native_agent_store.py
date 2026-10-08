"""Scoped role identity, delegation provenance and revocable credential persistence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_agents import NativeAgent, NativeAgentCredential, NativeTeamPolicy
from simon.domain.native_projects import NativeTask, TaskAssignment
from tests.contract.test_native_project_store import seed_native_projects


def role(h, **changes):
    values = {
        "workspace_id": h.workspace,
        "project_id": h.first.id,
        "name": "Researcher",
        "role_key": "researcher",
        "instructions": "Find evidence and record unresolved questions.",
        "success_criteria": "Claims link to their sources.",
        "rationale": "The project needs source review.",
        "created_by": h.owner,
    }
    return NativeAgent(**(values | changes))


def credential(h, agent, **changes):
    now = utc_now()
    values = {
        "workspace_id": h.workspace,
        "project_id": h.first.id,
        "agent_id": agent.id,
        "agent_version": agent.version,
        "token_hash": uuid4().hex + uuid4().hex,
        "scopes": frozenset({"board:read", "board:write"}),
        "issued_by": h.owner,
        "created_at": now,
        "expires_at": now + timedelta(minutes=15),
    }
    return NativeAgentCredential(**(values | changes))


@pytest.fixture
def agents(store):
    h = seed_native_projects(store)
    h.agent = role(h)
    store.insert_native_agent(h.agent)
    return h


def test_roles_are_project_scoped_and_count_only_active_agents(agents):
    h = agents
    paused = role(
        h,
        role_key="reviewer",
        status="paused",
        created_at=h.agent.created_at + timedelta(seconds=1),
    )
    other_project = role(h, project_id=h.second.id)
    for agent in (paused, other_project):
        h.store.insert_native_agent(agent)
    assert h.store.native_agent(h.workspace, h.first.id, h.agent.id) == h.agent
    assert h.store.native_agent(h.foreign_workspace, h.first.id, h.agent.id) is None
    assert h.store.native_agent(h.workspace, h.second.id, h.agent.id) is None
    assert h.store.native_agents(h.workspace, h.first.id, 0, 1) == (h.agent,)
    assert h.store.native_agents(h.workspace, h.first.id, 1, 1) == (paused,)
    assert h.store.native_agents(h.foreign_workspace, h.first.id, 0, 10) == ()
    assert h.store.native_active_agent_count(h.workspace, h.first.id) == 1
    assert h.store.native_active_agent_count(h.workspace, h.second.id) == 1
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_agent(role(h))
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_agent(role(h, role_key="missing", project_id=h.foreign.id))


def test_delegated_creators_and_task_assignments_cannot_cross_projects(agents):
    h = agents
    child = role(h, role_key="designer", created_by=None, created_by_agent_id=h.agent.id)
    h.store.insert_native_agent(child)
    assert h.store.native_agent(h.workspace, h.first.id, child.id) == child
    invalid_child = child.model_copy(update={"id": uuid4(), "project_id": h.second.id})
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_agent(invalid_child)
    task = NativeTask(
        workspace_id=h.workspace,
        project_id=h.first.id,
        title="Review the sources",
        created_by_agent_id=child.id,
        assignment=TaskAssignment(kind="agent", agent_id=h.agent.id),
    )
    h.store.insert_native_task(task)
    assert h.store.native_task(h.workspace, h.first.id, task.id) == task
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_task(
            task.model_copy(update={"id": uuid4(), "project_id": h.second.id})
        )
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_task(
            task.model_copy(update={"version": 2, "created_by_agent_id": h.agent.id}), 1
        )
    updated = task.model_copy(update={"version": 2, "description": "Keep the original creator."})
    h.store.update_native_task(updated, 1)
    assert h.store.native_task(h.workspace, h.first.id, task.id) == updated


def test_agent_updates_preserve_role_key_and_creator_and_reject_stale_versions(agents):
    h = agents
    for changes in (
        {"role_key": "new-role"},
        {"created_by": h.member},
        {"created_at": utc_now() + timedelta(days=1)},
        {"project_id": h.second.id},
        {"workspace_id": h.foreign_workspace},
        {"version": 3},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_native_agent(h.agent.model_copy(update={"version": 2, **changes}), 1)
    retired = h.agent.model_copy(update={"version": 2, "status": "retired"})
    h.store.update_native_agent(retired, 1)
    assert h.store.native_agent(h.workspace, h.first.id, h.agent.id) == retired
    assert h.store.native_active_agent_count(h.workspace, h.first.id) == 0
    with pytest.raises(InvalidTransitionError):
        h.store.update_native_agent(retired, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_agent(role(h))


def test_team_policy_compare_and_swap_and_project_isolation(agents):
    h = agents
    assert h.store.native_team_policy(h.workspace, h.first.id) is None
    policy = NativeTeamPolicy(workspace_id=h.workspace, project_id=h.first.id, version=1)
    h.store.save_native_team_policy(policy, 0)
    assert h.store.native_team_policy(h.workspace, h.first.id) == policy
    assert h.store.native_team_policy(h.foreign_workspace, h.first.id) is None
    with pytest.raises(InvalidTransitionError):
        h.store.save_native_team_policy(policy, 0)
    with pytest.raises(InvalidTransitionError):
        h.store.save_native_team_policy(policy.model_copy(update={"project_id": h.foreign.id}), 0)

    def update(ceiling):
        changed = policy.model_copy(update={"version": 2, "max_active_agents": ceiling})
        try:
            h.store.save_native_team_policy(changed, 1)
        except InvalidTransitionError:
            return None
        return changed

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [value for value in executor.map(update, (5, 10)) if value is not None]
    assert len(winners) == 1
    assert h.store.native_team_policy(h.workspace, h.first.id) == winners[0]


def test_credentials_bind_current_role_revision_and_do_not_rewrite_old_authority(agents):
    h = agents
    issued = credential(h, h.agent)
    h.store.insert_native_agent_credential(issued)
    assert h.store.native_agent_credential(issued.id) == issued
    assert h.store.native_agent_credentials(h.workspace, h.first.id, h.agent.id) == (issued,)
    assert h.store.native_agent_credentials(h.foreign_workspace, h.first.id, h.agent.id) == ()
    assert h.store.native_agent_credentials(h.workspace, h.second.id, h.agent.id) == ()
    for invalid in (
        issued,
        issued.model_copy(update={"id": uuid4()}),
        credential(h, h.agent, project_id=h.second.id),
        credential(h, h.agent, issued_by=h.outsider),
        credential(h, h.agent, agent_version=2),
        credential(h, h.agent, expires_at=utc_now() - timedelta(days=1)),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_native_agent_credential(invalid)
    changed = h.agent.model_copy(update={"version": 2, "instructions": "Review new evidence."})
    h.store.update_native_agent(changed, 1)
    assert h.store.native_agent_credential(issued.id) == issued
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_agent_credential(credential(h, h.agent))
    replacement = credential(h, changed)
    h.store.insert_native_agent_credential(replacement)
    h.store.update_native_agent(changed.model_copy(update={"version": 3, "status": "paused"}), 2)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_native_agent_credential(credential(h, changed, agent_version=3))


def test_credential_revocation_is_scoped_monotonic_and_replayable(agents):
    h = agents
    issued = credential(h, h.agent)
    h.store.insert_native_agent_credential(issued)
    now = utc_now()
    assert not h.store.revoke_native_agent_credential(
        h.foreign_workspace, h.first.id, h.agent.id, issued.id, now
    )
    assert not h.store.revoke_native_agent_credential(
        h.workspace, h.second.id, h.agent.id, issued.id, now
    )
    with pytest.raises(InvalidTransitionError):
        h.store.revoke_native_agent_credential(
            h.workspace, h.first.id, h.agent.id, issued.id, issued.created_at - timedelta(seconds=1)
        )
    assert h.store.revoke_native_agent_credential(
        h.workspace, h.first.id, h.agent.id, issued.id, now
    )
    assert not h.store.revoke_native_agent_credential(
        h.workspace, h.first.id, h.agent.id, issued.id, now + timedelta(seconds=1)
    )
    assert h.store.native_agent_credential(issued.id).revoked_at == now


def test_agent_pause_releases_only_nonterminal_tasks_and_rolls_back_together(agents):
    h = agents
    tasks = []
    for status in ("todo", "in_progress", "in_review", "blocked", "done", "cancelled"):
        task = NativeTask(
            workspace_id=h.workspace,
            project_id=h.first.id,
            title=f"Task {status}",
            status=status,
            created_by_agent_id=h.agent.id,
            assignment=TaskAssignment(kind="agent", agent_id=h.agent.id),
        )
        h.store.insert_native_task(task)
        tasks.append(task)
    paused = h.agent.model_copy(update={"version": 2, "status": "paused"})
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.update_native_agent(paused, 1)
        h.store.native_release_agent_tasks(h.workspace, h.first.id, h.agent.id)
        raise RuntimeError("Rollback paused role and its released work")
    assert h.store.native_agent(h.workspace, h.first.id, h.agent.id) == h.agent
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tuple(
        sorted(tasks, key=lambda task: (task.created_at, task.id))
    )
    assert h.store.native_release_agent_tasks(h.workspace, h.second.id, h.agent.id) == ()
    with h.store.transaction(h.workspace):
        h.store.update_native_agent(paused, 1)
        released = h.store.native_release_agent_tasks(h.workspace, h.first.id, h.agent.id)
    assert {task.id for task in released} == {task.id for task in tasks[:4]}
    for original in tasks:
        saved = h.store.native_task(h.workspace, h.first.id, original.id)
        assert saved.created_by_agent_id == h.agent.id
        if original.status in {"done", "cancelled"}:
            assert saved == original
        else:
            assert saved.assignment == TaskAssignment() and saved.version == 2
            assert saved.status == ("todo" if original.status == "in_progress" else original.status)
    assert h.store.native_release_agent_tasks(h.workspace, h.first.id, h.agent.id) == ()


def test_agent_policy_and_credential_creation_rollback(agents):
    h = agents
    child = role(h, role_key="temporary")
    policy = NativeTeamPolicy(workspace_id=h.workspace, project_id=h.first.id, version=1)
    issued = credential(h, child)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.insert_native_agent(child)
        h.store.save_native_team_policy(policy, 0)
        h.store.insert_native_agent_credential(issued)
        raise RuntimeError("Rollback new team state")
    assert h.store.native_agent(h.workspace, h.first.id, child.id) is None
    assert h.store.native_team_policy(h.workspace, h.first.id) is None
    assert h.store.native_agent_credential(issued.id) is None


@pytest.mark.postgres
def test_database_agent_references_reject_cross_project_authority(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url, pool_size=2)
    try:
        h = seed_native_projects(store)
        agent = role(h)
        store.insert_native_agent(agent)
        issued = credential(h, agent)
        store.insert_native_agent_credential(issued)
        for statement, parameters in (
            (
                "UPDATE native_agent_credentials SET project_id=%s WHERE id=%s",
                (h.second.id, issued.id),
            ),
            (
                "UPDATE native_agents SET created_by=NULL,created_by_agent_id=%s WHERE id=%s",
                (uuid4(), agent.id),
            ),
        ):
            with pytest.raises(psycopg.errors.ForeignKeyViolation), store.transaction():
                store.connection.execute(statement, parameters)
        with pytest.raises(psycopg.errors.CheckViolation), store.transaction():
            store.connection.execute(
                "UPDATE native_agents SET created_by=NULL WHERE id=%s", (agent.id,)
            )
        store.close()
        assert store.native_agent(h.workspace, h.first.id, agent.id) == agent
        assert store.native_agent_credential(issued.id) == issued
    finally:
        store.close()
