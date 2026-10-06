from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticError

from simon.domain.context import CreateMemory, ExplicitMemory
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.project_details import UpdateProjectDetails
from simon.services.project_details import REVISION_KIND, ProjectDetailsService
from simon.services.project_work import ProjectWorkService
from tests.contract.test_project_files import setup_project


@pytest.fixture
def details(store):
    connected, actor, files, project_id = setup_project(store)
    work = ProjectWorkService(store, project_resolver=files.project)
    return SimpleNamespace(
        connected=connected,
        actor=actor,
        project_id=project_id,
        files=files,
        work=work,
        service=ProjectDetailsService(work),
        store=store,
    )


def update(h, *, version=0, key="edit-project-details", actor=None, **fields):
    return h.service.update(
        actor or h.actor,
        h.project_id,
        UpdateProjectDetails(
            expected_version=version,
            idempotency_key=key,
            **(fields or {"name": "Linen studio", "description": "Prepare durable linen basics."}),
        ),
    )


def test_stable_project_edit_persists_without_changing_files_or_ownership(details):
    h = details
    original = h.files.project(h.actor, h.project_id)
    binding = h.files.binding(h.actor, h.project_id)
    calls = h.files.api.creates
    assert h.service.get(h.actor, h.project_id).version == 0
    saved = update(h)
    current = h.files.project(h.actor, h.project_id)
    assert saved.version == 1 and saved.project_id == original.id
    assert current == original.model_copy(
        update={"subject": "Linen studio", "content": "Prepare durable linen basics."}
    )
    assert h.files.binding(h.actor, h.project_id) == binding
    assert h.files.api.creates == calls
    assert any(item.subject == saved.name for item in h.connected.memories.list(h.actor))
    restored = ProjectDetailsService(h.work)
    assert restored.get(h.actor, h.project_id) == saved
    cleared = update(h, version=1, key="clear-description", description="")
    assert cleared.name == saved.name and cleared.description == "" and cleared.version == 2
    revisions = h.store.jobs(h.actor.workspace_id, h.actor.actor_id, REVISION_KIND, 0, 10)
    assert len(revisions) == 2
    assert any(row.input["before"]["name"] == original.subject for row in revisions)
    events = [e for e in h.store.audit_events() if e.event_type == "project.details_updated"]
    assert len(events) == 2 and events[0].payload["fields"] == ["name", "description"]
    assert len(h.work.list_activity(h.actor, h.project_id)["items"]) == 2


def test_duplicate_conflict_stale_and_noop_edits(details):
    h = details
    first = update(h)
    assert update(h) == first
    with pytest.raises(IdempotencyConflictError):
        update(h, name="Different request")
    with pytest.raises(InvalidTransitionError):
        update(h, key="stale-new-request", description="No")
    noop = update(h, version=1, key="already-saved-name", name=first.name)
    assert noop == first
    assert len(h.store.jobs(h.actor.workspace_id, h.actor.actor_id, REVISION_KIND, 0, 10)) == 1


def test_concurrent_edit_has_one_winner(details):
    h = details

    def attempt(index):
        try:
            return update(h, key=f"concurrent-details-{index}", name=f"Edit {index}")
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1
    assert h.service.get(h.actor, h.project_id).version == 1


def test_failed_audit_rolls_back_memory_version_and_receipt(details, monkeypatch):
    h = details
    original = h.service.get(h.actor, h.project_id)
    record = h.service.audit.record

    def fail(**kwargs):
        record(**kwargs)
        raise RuntimeError("Injected audit failure")

    monkeypatch.setattr(h.service.audit, "record", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        update(h)
    assert h.service.get(h.actor, h.project_id) == original
    assert not h.store.jobs(h.actor.workspace_id, h.actor.actor_id, REVISION_KIND, 0, 10)
    monkeypatch.setattr(h.service.audit, "record", record)
    assert update(h).version == 1


def test_project_access_permissions_and_replays_are_checked(details):
    h = details
    update(h)
    for change in ({"actor_id": uuid4()}, {"workspace_id": uuid4()}):
        foreign = h.actor.model_copy(update=change)
        with pytest.raises(NotFoundError):
            h.service.get(foreign, h.project_id)
        with pytest.raises(NotFoundError):
            update(h, actor=foreign)
    for permission in ("jobs:read", "jobs:write", "memories:read", "memories:write"):
        revoked = h.actor.model_copy(update={"scopes": h.actor.scopes - {permission}})
        with pytest.raises(AuthorizationError):
            update(h, actor=revoked)
    h.connected.memories.retract(h.actor, h.project_id)
    with pytest.raises(NotFoundError):
        update(h)


def test_shared_project_read_does_not_grant_other_member_write(details):
    h = details
    other_id = uuid4()
    shared = ExplicitMemory(
        workspace_id=h.actor.workspace_id,
        created_by=other_id,
        subject="Shared planning",
        content="Shared context",
        category="project",
        scope="workspace",
    )
    h.store.insert_memory(shared)
    member = h.actor.model_copy(update={"scopes": h.actor.scopes - {"memories:manage"}})
    assert h.service.get(member, shared.id).name == "Shared planning"
    with pytest.raises(AuthorizationError, match="creator"):
        h.service.update(
            member,
            shared.id,
            UpdateProjectDetails(
                expected_version=0, idempotency_key="foreign-shared-edit", name="No"
            ),
        )


def test_nonproject_memory_and_store_compare_and_swap_reject(details):
    h = details
    fact = h.connected.memories.create(
        h.actor,
        CreateMemory(subject="Fact", content="A memory", idempotency_key="metadata-is-not-fact"),
    )
    with pytest.raises(NotFoundError):
        h.service.get(h.actor, fact.id)
    previous = h.files.project(h.actor, h.project_id)
    update(h)
    with pytest.raises(InvalidTransitionError):
        h.store.update_project_memory(previous, "Stale overwrite", "No")
    with pytest.raises(InvalidTransitionError):
        h.store.update_project_memory(fact, "Mutate fact", "No")


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"name": " "},
        {"name": "x" * 201},
        {"description": "x" * 1001},
        {"description": "bad\x00text"},
        {"name": "\ud800"},
        {"name": "Name", "scope": "workspace"},
    ],
)
def test_project_details_boundaries_reject_unrelated_or_unsafe_fields(fields):
    with pytest.raises(PydanticError):
        UpdateProjectDetails(expected_version=0, idempotency_key="bounded-project-edit", **fields)
