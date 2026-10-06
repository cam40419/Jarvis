from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import ActorContext, Channel
from simon.domain.project_work import ProjectActivityDraft
from simon.domain.project_workspace import (
    ProjectRecordSource,
    SaveProjectDraft,
    UpdateProjectRecord,
)
from simon.services.project_work import ProjectWorkService
from simon.services.project_workspace import ProjectWorkspaceService


@pytest.fixture
def workspace(store):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    project_id, other_id = uuid4(), uuid4()
    access = {"allowed": True}

    def resolve(current, identifier):
        if (
            not access["allowed"]
            or identifier not in {project_id, other_id}
            or (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id)
        ):
            raise NotFoundError("Project not found")
        return SimpleNamespace(id=identifier)

    work = ProjectWorkService(store, project_resolver=resolve)
    return SimpleNamespace(
        actor=actor,
        project_id=project_id,
        other_id=other_id,
        access=access,
        work=work,
        service=ProjectWorkspaceService(work),
        store=store,
    )


def draft(h, text="Plan next collection", version=0, key="save-draft-first"):
    return h.service.save_draft(
        h.actor,
        h.project_id,
        "composer",
        SaveProjectDraft(
            text=text,
            expected_version=version,
            idempotency_key=key,
        ),
    )


def test_draft_durable_receipts_conflicts_and_revision_retention(workspace):
    h = workspace
    assert h.service.draft(h.actor, h.project_id, "composer").version == 0
    first = draft(h)
    second = draft(h, text="Research suppliers", version=1, key="draft-second")
    assert second.version == 2
    assert draft(h) == first  # Lost response replay must preserve the original receipt.
    assert ProjectWorkspaceService(h.work).draft(h.actor, h.project_id, "composer") == second
    with pytest.raises(InvalidTransitionError):
        draft(h, text="Stale tab", key="draft-stale-key")
    with pytest.raises(IdempotencyConflictError):
        draft(h, text="Changed same request")
    revisions = h.store.jobs(
        h.actor.workspace_id,
        h.actor.actor_id,
        h.service.kind(h.project_id, "draft_revision"),
        0,
        20,
    )
    assert {row.input["initial_state"]["text"] for row in revisions} == {
        "Plan next collection",
        "Research suppliers",
    }
    assert h.work.get(h.actor, h.project_id).activity_count == 0
    assert h.service.draft(h.actor, h.other_id, "composer").text == ""
    h.access["allowed"] = False
    with pytest.raises(NotFoundError):
        draft(h)


def test_two_tabs_cannot_overwrite_the_same_draft_version(workspace):
    h = workspace
    draft(h)

    def write(text):
        try:
            return draft(h, text=text, version=1, key="draft-" + text)
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(write, ("first-tab", "second-tab")))
    assert sum(value is not None for value in receipts) == 1
    assert h.service.draft(h.actor, h.project_id, "composer").version == 2


def test_records_keep_revisions_sources_and_agent_attribution(workspace):
    h = workspace
    record_id, run_id, plan_id = uuid4(), uuid4(), uuid4()
    body = UpdateProjectRecord(
        expected_version=0,
        idempotency_key="supplier-first",
        kind="supplier",
        title="Sample manufacturer",
        summary="Offers small sample runs.",
        fields={"minimum": "50 units"},
        confidence="supported",
        checked_at=datetime.now(UTC) - timedelta(days=2),
        review_after=datetime.now(UTC) - timedelta(days=1),
        sources=(ProjectRecordSource(label="Supplier catalog", url="https://example.com/catalog"),),
    )
    first = h.service.save_record(
        h.actor,
        h.project_id,
        record_id,
        body,
        run_id=run_id,
        plan_id=plan_id,
        agent_id="researcher",
    )
    assert (first.run_id, first.plan_id, first.agent_id) == (run_id, plan_id, "researcher")
    second = h.service.save_record(
        h.actor,
        h.project_id,
        record_id,
        body.model_copy(
            update={
                "expected_version": 1,
                "idempotency_key": "supplier-second",
                "fields": {"minimum": "75 units"},
            }
        ),
    )
    assert second.created_at == first.created_at and second.version == 2
    revisions = h.service.revisions(h.actor, h.project_id, record_id, limit=1)
    assert revisions["items"][0]["version"] == 2
    earlier = h.service.revisions(
        h.actor, h.project_id, record_id, before_version=revisions["next_before_version"]
    )
    assert earlier["items"][0]["fields"] == {"minimum": "50 units"}
    page = h.service.records(h.actor, h.project_id, query="75 units", kind="supplier")
    assert len(page["items"]) == 1 and page["items"][0]["needs_review"]
    assert ProjectWorkspaceService(h.work).record(h.actor, h.project_id, record_id) == second
    assert len(h.service.knowledge.history(h.actor, h.project_id).items) == 2
    with pytest.raises(NotFoundError):
        h.service.record(h.actor, h.other_id, record_id)


def test_records_reject_foreign_sources_and_false_owner_confirmation(workspace):
    h = workspace
    h.work.record_activity(
        h.actor,
        h.other_id,
        ProjectActivityDraft(kind="finding", text="Private"),
        idempotency_key="foreign-finding",
    )
    foreign = h.service.knowledge.history(h.actor, h.other_id).items[0]
    record_id = uuid4()
    body = UpdateProjectRecord(
        expected_version=0,
        idempotency_key="record-new-item",
        title="Fact",
        sources=(ProjectRecordSource(label="Source", activity_id=foreign.id),),
    )
    with pytest.raises(NotFoundError):
        h.service.save_record(h.actor, h.project_id, record_id, body)
    assert h.service.records(h.actor, h.project_id)["items"] == []
    body = body.model_copy(update={"sources": (), "confidence": "owner_confirmed"})
    with pytest.raises(AuthorizationError):
        h.service.save_record(h.actor, h.project_id, record_id, body, agent_id="researcher")
    owner_saved = h.service.save_record(h.actor, h.project_id, record_id, body)
    assert owner_saved.confidence == "owner_confirmed"


def test_procedures_and_readonly_context_leave_active_work_unchanged(workspace):
    h = workspace
    body = UpdateProjectRecord(
        expected_version=0,
        idempotency_key="procedure-save",
        kind="procedure",
        title="Supplier review",
        steps=("Check saved supplier records.", "Read current supplier terms."),
        success_checks=("All quotes have a source and checked date.",),
    )
    h.service.save_record(h.actor, h.project_id, uuid4(), body)
    state = h.work.get(h.actor, h.project_id)
    briefing = h.service.briefing(h.actor, h.project_id)
    assert briefing["records"]["items"][0]["steps"] == list(body.steps)
    assert "Supplier review" in h.service.context(h.actor, h.project_id)
    assert h.work.get(h.actor, h.project_id) == state
    read_actor = h.actor.model_copy(update={"scopes": frozenset({"jobs:read"})})
    assert h.service.briefing(read_actor, h.project_id) == briefing
    with pytest.raises(AuthorizationError):
        h.service.save_draft(
            read_actor,
            h.project_id,
            "composer",
            SaveProjectDraft(
                expected_version=0,
                idempotency_key="read-only-write",
                text="Unauthorized",
            ),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": "supported"},
        {"kind": "procedure"},
        {"steps": ("Do work",)},
        {"summary": "invalid\x00text"},
        {"fields": {"bad\x00key": "text"}},
    ],
)
def test_invalid_record_contracts(change):
    with pytest.raises(ValidationError):
        UpdateProjectRecord(
            expected_version=0, idempotency_key="invalid-record", title="Record", **change
        )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//evil.test",
        "https://user:pass@example.com",
        "https://example.com\n/path",
        "https://example.com\\other",
    ],
)
def test_record_source_links_cannot_embed_credentials_or_script(url):
    with pytest.raises(ValidationError):
        ProjectRecordSource(label="Source", url=url)
