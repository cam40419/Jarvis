from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.domain.models import ActorContext, Channel
from simon.domain.project_knowledge import PinnedProjectDecision, UpdateProjectKnowledge
from simon.domain.project_work import ProjectActivityDraft
from simon.services.project_knowledge import REVISION_KIND, ProjectKnowledgeService
from simon.services.project_work import ProjectWorkService


@pytest.fixture
def knowledge(store):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    project_id, other_id = uuid4(), uuid4()
    access = {"allowed": True}

    def resolve(current, identifier):
        if (
            not access["allowed"]
            or (current.actor_id, current.household_id)
            != (
                actor.actor_id,
                actor.household_id,
            )
            or identifier not in {project_id, other_id}
        ):
            raise NotFoundError("Project not found")
        return SimpleNamespace(id=identifier)

    work = ProjectWorkService(store, project_resolver=resolve)
    return SimpleNamespace(
        actor=actor,
        project_id=project_id,
        other_id=other_id,
        work=work,
        service=ProjectKnowledgeService(work),
        store=store,
        access=access,
    )


def save(h, *, version=0, brief="Launch a linen collection.", key="save-knowledge", pins=()):
    return h.service.update(
        h.actor,
        h.project_id,
        UpdateProjectKnowledge(
            expected_version=version,
            idempotency_key=key,
            brief=brief,
            pinned_decisions=pins,
        ),
    )


def record(h, text, *, kind="finding", project_id=None, run_id=None):
    identifier = project_id or h.project_id
    h.work.record_activity(
        h.actor,
        identifier,
        ProjectActivityDraft(
            kind=kind,
            text=text,
            run_id=run_id,
        ),
        idempotency_key="entry-" + uuid4().hex,
    )
    return h.service.history(h.actor, identifier, limit=1).items[0]


def test_memory_revision_and_audit_survive_service_reconstruction(knowledge):
    h = knowledge
    source = record(h, "Use linen for durability.", run_id=uuid4())
    first = save(
        h,
        pins=(
            PinnedProjectDecision(
                id="fabric",
                title="Fabric choice",
                text="Choose linen.",
                source_activity_id=source.id,
            ),
        ),
    )
    assert first.version == 1
    second = save(h, version=1, key="second-knowledge", brief="Prepare linen samples.")
    restored = ProjectKnowledgeService(h.work)
    assert restored.get(h.actor, h.project_id) == second
    assert second.version == 2 and second.pinned_decisions == ()
    revisions = h.store.jobs(h.actor.household_id, h.actor.actor_id, REVISION_KIND, 0, 10)
    assert len(revisions) == 2
    assert any(job.input["initial_state"]["pinned_decisions"] for job in revisions)
    assert restored.activity(h.actor, h.project_id, source.id).run_id == source.run_id
    assert len(restored.history(h.actor, h.project_id, kind="configuration").items) == 2


def test_response_loss_retry_stale_update_and_payload_conflict(knowledge):
    h = knowledge
    first = save(h)
    assert save(h) == first
    with pytest.raises(IdempotencyConflictError):
        save(h, brief="Changed using the same key")
    with pytest.raises(InvalidTransitionError):
        save(h, key="different-stale-key")
    assert h.service.get(h.actor, h.project_id) == first
    assert len(h.service.history(h.actor, h.project_id).items) == 1
    # Task/activity edits do not invalidate the independently versioned brief.
    record(h, "Unrelated worker progress", kind="progress")
    assert save(h, version=1, key="save-after-progress").version == 2


def test_concurrent_edit_has_one_winner_without_lost_update(knowledge):
    h = knowledge
    save(h)

    def attempt(index):
        try:
            return save(h, version=1, brief=f"Edit {index}", key=f"concurrent-edit-{index}")
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, range(2)))
    assert sum(item is not None for item in results) == 1
    assert h.service.get(h.actor, h.project_id).version == 2


def test_authorization_rechecked_for_cached_save_and_all_reads(knowledge):
    h = knowledge
    entry = record(h, "Private decision")
    save(h)
    other = h.actor.model_copy(update={"actor_id": uuid4()})
    outsider = h.actor.model_copy(update={"household_id": uuid4()})
    for actor in (other, outsider):
        for read in (
            lambda actor=actor: h.service.get(actor, h.project_id),
            lambda actor=actor: h.service.history(actor, h.project_id),
            lambda actor=actor: h.service.activity(actor, h.project_id, entry.id),
        ):
            with pytest.raises(NotFoundError):
                read()
    read_only = h.actor.model_copy(update={"scopes": frozenset({"jobs:read"})})
    with pytest.raises(AuthorizationError):
        h.service.update(
            read_only,
            h.project_id,
            UpdateProjectKnowledge(
                expected_version=1,
                idempotency_key="readonly-save",
            ),
        )
    no_read = h.actor.model_copy(update={"scopes": frozenset()})
    with pytest.raises(AuthorizationError):
        h.service.history(no_read, h.project_id)
    h.access["allowed"] = False
    with pytest.raises(NotFoundError):
        save(h)


def test_cross_project_source_rejected_and_save_rolled_back(knowledge):
    h = knowledge
    foreign = record(h, "Other project secret", project_id=h.other_id)
    with pytest.raises(NotFoundError):
        save(
            h,
            pins=(
                PinnedProjectDecision(
                    id="foreign", title="Bad reference", text="No", source_activity_id=foreign.id
                ),
            ),
        )
    assert h.service.get(h.actor, h.project_id).version == 0
    assert not h.service.history(h.actor, h.project_id).items
    with pytest.raises(NotFoundError):
        h.service.activity(h.actor, h.project_id, foreign.id)


def test_searches_entire_history_literal_substrings_and_kind(knowledge):
    h = knowledge
    earliest = record(h, "Linen supplier offers 20%_discount.")
    record(h, "Linen supplier progress", kind="progress")
    for index in range(35):
        record(h, f"Recent unrelated work {index}", kind="progress")
    record(h, "Private linen note", project_id=h.other_id)
    page = h.service.history(h.actor, h.project_id, query="LINEN", kind="finding")
    assert [item.id for item in page.items] == [earliest.id]
    assert h.service.history(h.actor, h.project_id, query="%_").items == (earliest,)
    assert not h.service.history(h.actor, h.project_id, query="x%_").items


def test_keyset_pages_remain_stable_and_cursor_binds_filters(knowledge):
    h = knowledge
    original = [record(h, f"Finding {index}") for index in range(6)]
    page = h.service.history(h.actor, h.project_id, query="Finding", kind="finding", limit=2)
    assert page.items == tuple(reversed(original[-2:]))
    assert page.next_cursor
    record(h, "Finding inserted after page one")
    rest = []
    cursor = page.next_cursor
    while cursor:
        current = h.service.history(
            h.actor, h.project_id, query="Finding", kind="finding", limit=2, cursor=cursor
        )
        rest.extend(current.items)
        cursor = current.next_cursor
    assert rest == list(reversed(original[:-2]))
    for overrides in ({"query": "Other"}, {"kind": "progress"}, {"project_id": h.other_id}):
        arguments = {
            "project_id": h.project_id,
            "query": "Finding",
            "kind": "finding",
            "cursor": page.next_cursor,
            **overrides,
        }
        with pytest.raises(ValidationError, match="cursor"):
            h.service.history(h.actor, **arguments)
    with pytest.raises(ValidationError):
        h.service.history(h.actor, h.project_id, cursor="not-json")


def test_isolation_even_when_project_resolver_permits_multiple_owners(knowledge):
    h = knowledge
    record(h, "Only this owner may see it")
    save(h)
    broad = ProjectKnowledgeService(
        ProjectWorkService(
            h.store,
            project_resolver=lambda *_: SimpleNamespace(id=h.project_id),
        )
    )
    for actor in (
        h.actor.model_copy(update={"actor_id": uuid4()}),
        h.actor.model_copy(update={"household_id": uuid4()}),
    ):
        assert broad.get(actor, h.project_id).version == 0
        assert broad.history(actor, h.project_id).items == ()
