"""Exact candidate evidence, independent review, owner decisions and bounded repair."""

import base64
import hashlib
import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_artifact_commands import (
    DecideNativeArtifact,
    ReviewNativeArtifact,
    UpdateArtifactReviewPolicy,
    UploadNativeArtifact,
)
from simon.domain.native_execution import UpdateTaskWorkflow
from simon.domain.native_models import ModelResourcePolicy
from simon.domain.native_projects import NativeProjectMember
from simon.services.identity import ROLE_SCOPES
from simon.services.native_artifacts import NativeArtifactService
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_execution import advance, claim, draft, new_task, setup_execution, start
from tests.unit.test_native_teams import create_role, issue, revision, task_update
from tests.unit.test_project_models import check, enroll, setup_models, template, update


def public(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def key():
    return {"idempotency_key": f"artifact-{uuid4()}"}


def setup_artifacts(*, paid=False):
    if paid:
        h = setup_models(paid=True)
        h.model = check(h, enroll(h))
        h.agent = create_role(h)
        h.artifacts = NativeArtifactService(h.store, h.projects, h.models)
    else:
        h = setup_execution()
        h.artifacts = h.execution.artifacts
    h.reviewer = create_role(h, key="quality")
    h.task = new_task(h)
    h.policy = public(
        h.artifacts.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateArtifactReviewPolicy(
                expected_version=0, reviewer_agent_id=h.reviewer.id, **key()
            ),
        )
    )
    h.calls.clear()
    return h


def detail(h, artifact):
    return h.artifacts.detail(h.owner_actor, h.first.id, UUID(str(artifact["id"])))


def current_revision(value):
    return next(
        revision
        for revision in value["revisions"]
        if revision["id"] == value["artifact"]["current_revision_id"]
    )


def upload(h, content=b"Evidence-backed candidate.", *, artifact=None, task=None, **changes):
    task = task or h.store.native_task(h.workspace, h.first.id, h.task.id)
    command = UploadNativeArtifact(
        **{
            "task_id": task.id,
            "expected_task_version": task.version,
            "expected_version": 0,
            "filename": "candidate.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
            **(
                {"artifact_id": artifact["id"], "expected_version": artifact["version"]}
                if artifact
                else {}
            ),
            **key(),
            **changes,
        }
    )
    result = public(h.artifacts.upload(h.owner_actor, h.first.id, command))
    artifact = result.get("artifact", result)
    return detail(h, artifact)["artifact"], command


def target_command(h, artifact, command_type=ReviewNativeArtifact, **changes):
    value = detail(h, artifact)
    revision_value = current_revision(value)
    task = h.store.native_task(h.workspace, h.first.id, UUID(value["artifact"]["task_id"]))
    return command_type(
        **{
            "expected_version": value["artifact"]["version"],
            "expected_policy_version": h.policy["version"],
            "expected_task_version": task.version,
            "revision_id": revision_value["id"],
            "sha256": revision_value["sha256"],
            **key(),
            **changes,
        }
    )


def reviewer_result(command, *, approved=True):
    return {
        "revision_id": str(command.revision_id),
        "sha256": command.sha256,
        "verdict": "approved" if approved else "changes_requested",
        "summary": "Checked the exact supplied candidate and task requirements.",
        "findings": [] if approved else ["The claimed measurement has no supporting evidence."],
    }


def review(h, artifact, *, approved=True, command=None):
    command = command or target_command(h, artifact)
    h.response = json.dumps(reviewer_result(command, approved=approved))
    return public(h.artifacts.review(h.owner_actor, h.first.id, UUID(artifact["id"]), command))


def decide(h, artifact, *, approved=True, **changes):
    command = target_command(
        h,
        artifact,
        DecideNativeArtifact,
        decision="approved" if approved else "changes_requested",
        feedback="" if approved else "Replace the unsupported measurement with cited evidence.",
        **changes,
    )
    result = h.artifacts.decide(h.owner_actor, h.first.id, UUID(artifact["id"]), command)
    value = public(result)
    return max(value["decisions"], key=lambda item: item["created_at"]), command


def test_completed_execution_publishes_one_exact_revision_without_acceptance():
    h = setup_artifacts()
    run = start(h, h.task)
    lease = claim(h)
    candidate = "A concrete candidate with a seeded unsupported claim."
    finished = advance(h, lease, draft(candidate))
    assert finished["status"] == "completed"
    value = h.artifacts.view(h.owner_actor, h.first.id)
    assert len(value["artifacts"]["items"]) == 1
    artifact = value["artifacts"]["items"][0]
    evidence = detail(h, artifact)
    revision_value = current_revision(evidence)
    assert revision_value["run_id"] == run["id"]
    assert revision_value["sha256"] == hashlib.sha256(candidate.encode()).hexdigest()
    assert revision_value["size_bytes"] == len(candidate.encode())
    assert revision_value["created_by_agent_id"] == str(h.agent.id)
    assert revision_value["created_by"] is None
    assert h.store.native_task(h.workspace, h.first.id, h.task.id).status == "in_review"
    assert evidence["reviews"] == [] and evidence["decisions"] == []
    assert "content_base64" not in json.dumps(value) + json.dumps(evidence)


def test_file_revisions_preserve_exact_original_and_parent_links():
    h = setup_artifacts()
    original = b"First draft with spaces.\r\n"
    artifact, _ = upload(h, original)
    first = current_revision(detail(h, artifact))
    artifact, _ = upload(h, b"Corrected revision.\n", artifact=artifact)
    second = current_revision(detail(h, artifact))
    assert first["id"] != second["id"] and second["parent_revision_id"] == first["id"]
    assert first["sequence"] == 1 and second["sequence"] == 2
    _, old_content = h.artifacts.read_content(
        h.owner_actor, h.first.id, UUID(artifact["id"]), UUID(first["id"])
    )
    assert old_content == original
    assert second["sha256"] != first["sha256"]
    assert not h.calls


def test_review_uses_separate_route_exact_content_and_fresh_role_context():
    h = setup_artifacts()
    h.templates.append(template(h.workspace, identifier="reviewer"))
    model = check(h, enroll(h, template_id="reviewer", label="Reviewer"))
    h.store.save_model_resource_policy(
        ModelResourcePolicy(
            workspace_id=h.workspace,
            project_id=h.first.id,
            version=1,
            planning_model_id=UUID(h.model["id"]),
            review_model_id=UUID(model["id"]),
        ),
        expected_version=0,
    )
    h.calls.clear()
    artifact, _ = upload(h, b"Exact candidate: seeded unsupported measurement 41%.")
    result = review(h, artifact, approved=False)
    assert result["status"] == "completed" and result["verdict"] == "changes_requested"
    assert len(h.calls) == 1 and result["model_id"] == model["id"]
    body = json.loads(h.calls[0].content)
    prompt = h.calls[0].content.decode()
    assert "Exact candidate: seeded unsupported measurement 41%." in prompt
    assert h.task.description in prompt and h.reviewer.instructions in prompt
    assert current_revision(detail(h, artifact))["sha256"] in prompt
    assert body["stream"] is False
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))
    assert len(usage) == 1 and usage[0].phase == "artifact_review"
    assert usage[0].status == "settled" and usage[0].input_tokens == 100
    assert h.store.native_task(h.workspace, h.first.id, h.task.id).status != "done"
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)


def test_owner_approval_requires_independent_passing_review_and_exact_decision():
    h = setup_artifacts()
    artifact, _ = upload(h)
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    checked = review(h, artifact)
    decision, command = decide(h, artifact)
    assert decision["decision"] == "approved" and decision["review_id"] == checked["id"]
    assert decision["revision_id"] == str(command.revision_id)
    assert decision["sha256"] == command.sha256
    assert decision["decided_by"] == str(h.owner)
    assert h.store.native_task(h.workspace, h.first.id, h.task.id).status == "done"
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "revision_id",
        "sha256",
        "expected_version",
        "expected_task_version",
        "expected_policy_version",
    ],
)
def test_review_rejects_stale_exact_target_before_dispatch(field):
    h = setup_artifacts()
    artifact, _ = upload(h)
    command = target_command(h, artifact)
    replacement = uuid4() if field == "revision_id" else "0" * 64 if field == "sha256" else 999
    command = command.model_copy(update={field: replacement})
    with pytest.raises((InvalidTransitionError, NotFoundError, ValidationError)):
        review(h, artifact, command=command)
    assert not h.calls and not detail(h, artifact)["reviews"]


def test_producer_cannot_review_own_candidate():
    h = setup_artifacts()
    start(h, h.task)
    advance(h, claim(h), draft())
    artifact = h.artifacts.view(h.owner_actor, h.first.id)["artifacts"]["items"][0]
    command = target_command(h, artifact, reviewer_agent_id=h.agent.id)
    h.calls.clear()
    with pytest.raises((AuthorizationError, ValidationError)):
        review(h, artifact, command=command)
    assert not h.calls


@pytest.mark.parametrize(
    "response",
    [
        "invalid JSON",
        "{}",
        '{"verdict":"approved","summary":"Pass","findings":["Missing evidence"]}',
    ],
)
def test_malformed_or_contradictory_reviewer_result_cannot_enable_acceptance(response):
    h = setup_artifacts()
    artifact, _ = upload(h)
    h.response = response
    result = public(
        h.artifacts.review(
            h.owner_actor, h.first.id, UUID(artifact["id"]), target_command(h, artifact)
        )
    )
    assert result["status"] == "failed" and result["verdict"] is None
    assert len(h.calls) == 1
    entries = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))
    assert entries[0].status == "settled"
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)


def test_request_changes_creates_one_linked_repair_task_and_replay_does_not_duplicate():
    h = setup_artifacts()
    artifact, _ = upload(h)
    decision, command = decide(h, artifact, approved=False)
    repair = h.store.native_task(h.workspace, h.first.id, UUID(decision["repair_task_id"]))
    assert repair.status == "todo" and repair.assignment.agent_id == h.agent.id
    assert command.feedback in repair.description
    assert str(command.revision_id) in repair.description and command.sha256 in repair.description
    replay = public(h.artifacts.decide(h.owner_actor, h.first.id, UUID(artifact["id"]), command))
    assert replay["decisions"][0]["id"] == decision["id"]
    assert len(detail(h, artifact)["decisions"]) == 1
    assert not h.calls


def test_changed_revision_invalidates_prior_review_and_owner_acceptance():
    h = setup_artifacts()
    artifact, _ = upload(h)
    review(h, artifact)
    stale = target_command(h, artifact, DecideNativeArtifact, decision="approved")
    artifact, _ = upload(h, b"Different content that needs a fresh check.", artifact=artifact)
    with pytest.raises(InvalidTransitionError):
        h.artifacts.decide(h.owner_actor, h.first.id, UUID(artifact["id"]), stale)
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    assert len(h.calls) == 1 and not detail(h, artifact)["decisions"]


@pytest.mark.parametrize("change", ["task", "policy", "reviewer", "model"])
def test_changed_authority_after_review_requires_new_review_before_owner_approval(change):
    h = setup_artifacts()
    artifact, _ = upload(h)
    review(h, artifact)
    if change == "task":
        task = h.store.native_task(h.workspace, h.first.id, h.task.id)
        h.projects.update_task(
            h.owner_actor, h.first.id, task.id, task_update(task, description="New requirements.")
        )
    elif change == "policy":
        h.policy = public(
            h.artifacts.update_policy(
                h.owner_actor,
                h.first.id,
                UpdateArtifactReviewPolicy(
                    expected_version=h.policy["version"],
                    reviewer_agent_id=h.reviewer.id,
                    max_repair_cycles=2,
                    **key(),
                ),
            )
        )
    elif change == "reviewer":
        h.teams.update(
            h.owner_actor,
            h.first.id,
            h.reviewer.id,
            revision(h.reviewer, instructions="New independent checks."),
        )
    else:
        update(h, h.model, enabled=False)
    with pytest.raises((InvalidTransitionError, ValidationError)):
        decide(h, artifact)
    assert not detail(h, artifact)["decisions"]


def test_upload_idempotency_and_current_authority_before_replay():
    h = setup_artifacts()
    artifact, command = upload(h)
    replay = public(h.artifacts.upload(h.owner_actor, h.first.id, command))
    assert replay.get("artifact", replay)["id"] == artifact["id"]
    changed = command.model_copy(update={"content_base64": base64.b64encode(b"different").decode()})
    with pytest.raises(IdempotencyConflictError):
        h.artifacts.upload(h.owner_actor, h.first.id, changed)
    h.store.delete_membership(h.owner, h.workspace)
    with pytest.raises(AuthorizationError):
        h.artifacts.upload(h.owner_actor, h.first.id, command)


def test_members_can_read_and_download_but_cannot_spend_or_approve():
    h = setup_artifacts()
    artifact, command = upload(h)
    h.store.put_native_project_member(
        NativeProjectMember(workspace_id=h.workspace, project_id=h.first.id, actor_id=h.member)
    )
    member = ActorContext(
        actor_id=h.member,
        workspace_id=h.workspace,
        scopes=ROLE_SCOPES["member"],
        channel=Channel.API,
    )
    value = h.artifacts.detail(member, h.first.id, UUID(artifact["id"]))
    assert value["artifact"]["id"] == artifact["id"]
    assert h.artifacts.view(member, h.first.id)["can_manage"] is False
    with pytest.raises(AuthorizationError):
        h.artifacts.upload(member, h.first.id, command)
    with pytest.raises(AuthorizationError):
        h.artifacts.review(member, h.first.id, UUID(artifact["id"]), target_command(h, artifact))
    with pytest.raises(AuthorizationError):
        h.artifacts.decide(
            member,
            h.first.id,
            UUID(artifact["id"]),
            target_command(h, artifact, DecideNativeArtifact, decision="approved"),
        )
    worker, _, _ = issue(h, h.agent)
    with pytest.raises(AuthorizationError):
        h.artifacts.view(worker, h.first.id)
    with pytest.raises(NotFoundError):
        h.artifacts.detail(h.owner_actor, h.second.id, UUID(artifact["id"]))


@pytest.mark.parametrize(
    "content,media_type",
    [(b"\x00\xff\x00", "application/octet-stream"), (b"x" * 100000, "text/plain")],
    ids=["binary", "oversized_text"],
)
def test_unsupported_review_content_stays_downloadable_and_cannot_dispatch(content, media_type):
    h = setup_artifacts()
    artifact, _ = upload(h, content, media_type=media_type)
    with pytest.raises((ValidationError, InvalidTransitionError)):
        review(h, artifact)
    assert not h.calls and not detail(h, artifact)["reviews"]


def test_review_replay_returns_same_durable_attempt_without_another_provider_call():
    h = setup_artifacts()
    artifact, _ = upload(h)
    command = target_command(h, artifact)
    first = review(h, artifact, command=command)
    second = review(h, artifact, command=command)
    assert first["id"] == second["id"] and len(h.calls) == 1
    assert len(detail(h, artifact)["reviews"]) == 1


def test_paid_review_records_known_usage_in_project_and_workspace_totals():
    h = setup_artifacts(paid=True)
    artifact, _ = upload(h)
    before = h.store.model_usage_totals(h.workspace, h.first.id, h.task.created_at)
    result = review(h, artifact)
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))[0]
    assert usage.status == "settled" and usage.held_microusd == 0
    assert usage.charged_microusd == 120
    project = h.store.model_usage_totals(h.workspace, h.first.id, h.task.created_at)
    workspace = h.store.model_usage_totals(h.workspace, None, h.task.created_at)
    assert project.charged_lifetime - before.charged_lifetime == usage.charged_microusd
    assert workspace.charged_lifetime == project.charged_lifetime
    assert len(h.calls) == 1 and not detail(h, artifact)["decisions"]


def test_unknown_review_holds_money_and_call_slot_and_replay_never_resends():
    h = setup_artifacts(paid=True)
    for project_id in (None, h.first.id):
        policy = h.usage.policy(h.workspace, project_id)
        h.store.save_model_resource_policy(
            policy.model_copy(update={"version": policy.version + 1, "max_concurrent_calls": 1}),
            expected_version=policy.version,
        )
    artifact, _ = upload(h)
    command = target_command(h, artifact)

    def interrupt(request):
        raise httpx.ReadTimeout("private-provider-timeout", request=request)

    h.hook = interrupt
    first = review(h, artifact, command=command)
    assert first["status"] == "unknown" and first["verdict"] is None
    entry = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(first["id"]))[0]
    assert entry.status == "unknown" and entry.held_microusd == entry.reserved_microusd > 0
    assert h.store.model_usage_totals(h.workspace, h.first.id, h.task.created_at).active_calls == 1
    assert review(h, artifact, command=command)["id"] == first["id"]
    h.hook = None
    with pytest.raises(InvalidTransitionError, match="concurrent"):
        review(h, artifact)
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    assert len(h.calls) == 1 and "private-provider-timeout" not in json.dumps(first)


@pytest.mark.parametrize("change", ["policy", "reviewer", "task", "revision"])
def test_known_review_charge_survives_fenced_in_flight_result(change):
    h = setup_artifacts(paid=True)
    artifact, _ = upload(h)

    def changed_authority(_request):
        h.hook = None
        if change == "policy":
            h.policy = public(
                h.artifacts.update_policy(
                    h.owner_actor,
                    h.first.id,
                    UpdateArtifactReviewPolicy(
                        expected_version=h.policy["version"],
                        reviewer_agent_id=h.reviewer.id,
                        max_repair_cycles=2,
                        **key(),
                    ),
                )
            )
        elif change == "reviewer":
            h.teams.update(
                h.owner_actor, h.first.id, h.reviewer.id, revision(h.reviewer, status="paused")
            )
        elif change == "task":
            task = h.store.native_task(h.workspace, h.first.id, h.task.id)
            h.projects.update_task(
                h.owner_actor,
                h.first.id,
                task.id,
                task_update(task, description="Changed during provider work."),
            )
        else:
            upload(h, b"Concurrent revised bytes.", artifact=artifact)

    h.hook = changed_authority
    result = review(h, artifact)
    assert result["status"] == "stale" and result["verdict"] is None
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))[0]
    assert usage.status == "settled" and usage.charged_microusd == 120
    assert usage.held_microusd == 0 and len(h.calls) == 1
    assert not detail(h, artifact)["decisions"]


def test_revoked_requesting_owner_cannot_apply_result_but_provider_charge_is_durable():
    h = setup_artifacts(paid=True)
    artifact, _ = upload(h)
    revision_value = current_revision(detail(h, artifact))

    def revoke(_request):
        h.hook = None
        h.store.delete_membership(h.owner, h.workspace)

    h.hook = revoke
    with pytest.raises(AuthorizationError):
        review(h, artifact)
    attempts = h.store.artifact_reviews(h.workspace, h.first.id, UUID(revision_value["id"]))
    assert len(attempts) == 1 and attempts[0].status == "stale"
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, attempts[0].id)[0]
    assert usage.status == "settled" and usage.charged_microusd == 120
    assert not h.store.artifact_decisions(h.workspace, h.first.id, UUID(revision_value["id"]))


def test_explicit_automatic_policy_accepts_only_after_independent_review():
    h = setup_artifacts()
    h.policy = public(
        h.artifacts.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateArtifactReviewPolicy(
                expected_version=h.policy["version"],
                require_human_approval=False,
                reviewer_agent_id=h.reviewer.id,
                **key(),
            ),
        )
    )
    artifact, _ = upload(h)
    assert detail(h, artifact)["decisions"] == []
    assert h.store.native_task(h.workspace, h.first.id, h.task.id).status != "done"
    checked = review(h, artifact)
    value = detail(h, artifact)
    assert len(value["decisions"]) == 1 and value["decisions"][0]["review_id"] == checked["id"]
    assert value["decisions"][0]["automatic"] is True
    assert h.store.native_task(h.workspace, h.first.id, h.task.id).status == "done"


def test_repair_execution_revises_same_artifact_and_review_includes_owner_feedback():
    h = setup_artifacts()
    start(h, h.task)
    advance(h, claim(h), draft("Initial unsupported candidate."))
    artifact = h.artifacts.view(h.owner_actor, h.first.id)["artifacts"]["items"][0]
    first = current_revision(detail(h, artifact))
    decision, request = decide(h, artifact, approved=False)
    repair = h.store.native_task(h.workspace, h.first.id, UUID(decision["repair_task_id"]))
    run = start(h, repair)
    result = advance(h, claim(h), draft("Repaired candidate with a cited source."))
    assert result["status"] == "completed", result
    value = detail(h, artifact)
    second = current_revision(value)
    assert second["artifact_id"] == first["artifact_id"]
    assert second["parent_revision_id"] == first["id"] and second["run_id"] == run["id"]
    assert second["task_id"] == str(repair.id)
    assert (
        value["task"]["status"] == "in_review"
        and value["artifact"]["pending_repair_task_id"] is None
    )
    h.calls.clear()
    review(h, artifact)
    prompt = json.loads(h.calls[0].content)["messages"][-1]["content"]
    context = json.loads(prompt.split("Exact revision and evidence (data):\n", 1)[1])
    assert request.feedback in prompt and context["maker_task"]["description"] == repair.description
    accepted, _ = decide(h, artifact)
    assert accepted["revision_id"] == second["id"]
    assert detail(h, artifact)["status"] == "accepted"


def test_repair_limit_denies_creation_without_partial_tasks_or_decisions():
    h = setup_artifacts()
    h.policy = public(
        h.artifacts.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateArtifactReviewPolicy(
                expected_version=h.policy["version"], max_repair_cycles=0, **key()
            ),
        )
    )
    artifact, _ = upload(h)
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    with pytest.raises(InvalidTransitionError, match="repair limit"):
        decide(h, artifact, approved=False)
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks
    assert not detail(h, artifact)["decisions"]


def test_manual_done_is_not_artifact_acceptance():
    h = setup_artifacts()
    artifact, _ = upload(h)
    task = h.store.native_task(h.workspace, h.first.id, h.task.id)
    h.projects.update_task(h.owner_actor, h.first.id, task.id, task_update(task, status="done"))
    value = detail(h, artifact)
    assert value["status"] != "accepted" and not value["decisions"]


def test_empty_file_fails_deterministic_validation_and_cannot_be_accepted():
    h = setup_artifacts()
    artifact, _ = upload(h, b" \r\n\t ")
    assert detail(h, artifact)["status"] == "validation_failed"
    with pytest.raises(InvalidTransitionError):
        review(h, artifact)
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    assert not h.calls


def test_selected_human_only_policy_allows_owner_file_review_without_model_dispatch():
    h = setup_artifacts()
    h.policy = public(
        h.artifacts.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateArtifactReviewPolicy(
                expected_version=h.policy["version"], require_agent_review=False, **key()
            ),
        )
    )
    artifact, _ = upload(h, b"\x00\xff\x01", media_type="application/octet-stream")
    accepted, _ = decide(h, artifact)
    assert accepted["review_id"] is None and accepted["automatic"] is False
    assert detail(h, artifact)["status"] == "accepted" and not h.calls


def test_review_has_latest_source_excerpt_and_changed_source_invalidates_approval():
    h = setup_artifacts()
    initial = source(h, text="Original measurement: 30 percent.")
    replacement = source(h, revision=2, text="Corrected measurement: 20 percent.")
    for evidence in (initial, replacement):
        h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h)
    review(h, artifact)
    prompt = h.calls[0].content.decode()
    assert replacement.text in prompt and initial.text not in prompt
    assert str(replacement.id) in prompt and replacement.sha256 in prompt
    h.store.insert_native_intake_source(source(h, revision=3, text="Measurement withdrawn."))
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    assert not detail(h, artifact)["decisions"]


def test_interrupted_review_recovers_unknown_without_redispatch_or_releasing_liability(monkeypatch):
    h = setup_artifacts(paid=True)
    artifact, _ = upload(h)
    command = target_command(h, artifact)

    def process_stop(_request):
        raise SystemExit("Simulated worker process stop after durable dispatch.")

    h.hook = process_stop
    with pytest.raises(SystemExit):
        review(h, artifact, command=command)
    revision_value = current_revision(detail(h, artifact))
    attempt = h.store.artifact_reviews(h.workspace, h.first.id, UUID(revision_value["id"]))[0]
    assert attempt.status == "running"
    moment = utc_now() + timedelta(seconds=301)
    monkeypatch.setattr("simon.services.native_artifacts.utc_now", lambda: moment)
    monkeypatch.setattr("simon.services.model_usage.utc_now", lambda: moment)
    h.hook = None
    recovered = detail(h, artifact)["reviews"][0]
    assert recovered["status"] == "unknown" and recovered["verdict"] is None
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, attempt.id)[0]
    assert usage.status == "unknown" and usage.held_microusd == usage.reserved_microusd > 0
    assert h.store.model_usage_totals(h.workspace, h.first.id, moment).active_calls == 1
    replay = review(h, artifact, command=command)
    assert replay["id"] == recovered["id"] and replay["status"] == "unknown"
    assert len(h.calls) == 1


@pytest.mark.parametrize("field", ["revision_id", "sha256"])
def test_provider_cannot_approve_different_revision_or_hash(field):
    h = setup_artifacts()
    artifact, _ = upload(h)
    command = target_command(h, artifact)
    response = reviewer_result(command)
    response[field] = str(uuid4()) if field == "revision_id" else "0" * 64
    h.response = json.dumps(response)
    result = h.artifacts.review(h.owner_actor, h.first.id, UUID(artifact["id"]), command)
    assert result["status"] == "failed" and result["verdict"] is None
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))[0]
    assert usage.status == "settled" and usage.held_microusd == 0
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)


def dependent_run(h):
    task = new_task(h, title="Prepare work using the current deliverable")
    h.execution.update_workflow(
        h.owner_actor,
        h.first.id,
        task.id,
        UpdateTaskWorkflow(expected_version=0, dependency_ids=(h.task.id,), **key()),
    )
    return start(h, task)


def test_dependency_waits_for_repair_then_uses_current_accepted_revision():
    h = setup_artifacts()
    start(h, h.task)
    advance(h, claim(h), draft("Obsolete original candidate."))
    artifact = h.artifacts.view(h.owner_actor, h.first.id)["artifacts"]["items"][0]
    request, _ = decide(h, artifact, approved=False)
    downstream = dependent_run(h)
    h.calls.clear()
    assert claim(h) is None and not h.calls
    repair = h.store.native_task(h.workspace, h.first.id, UUID(request["repair_task_id"]))
    start(h, repair)
    lease = claim(h)
    assert lease["run_id"] != downstream["id"]
    repaired = "Current corrected candidate with evidence."
    assert advance(h, lease, draft(repaired))["status"] == "completed"
    review(h, artifact)
    decide(h, artifact)
    revision_value = current_revision(detail(h, artifact))
    h.calls.clear()
    lease = claim(h)
    assert lease["run_id"] == downstream["id"]
    assert (
        advance(h, lease, draft("Work based on the accepted correction."))["status"] == "completed"
    )
    prompt = json.loads(h.calls[0].content)["messages"][-1]["content"]
    assert repaired in prompt and "Obsolete original candidate." not in prompt
    assert revision_value["sha256"] in prompt and revision_value["id"] in prompt
    context_text = prompt.split("Current task and authority context (data):\n", 1)[1].split(
        "\nCompleted workflow history (data):\n", 1
    )[0]
    assert json.loads(context_text)["dependencies"][0]["accepted"] is True


def test_uploaded_accepted_file_can_supply_dependency_without_an_execution_run():
    h = setup_artifacts()
    artifact, _ = upload(h, b"Owner-supplied accepted source candidate.")
    assert h.store.execution_task_runs(h.workspace, h.first.id, h.task.id) == ()
    review(h, artifact)
    decide(h, artifact)
    downstream = dependent_run(h)
    h.calls.clear()
    lease = claim(h)
    assert lease["run_id"] == downstream["id"]
    assert (
        advance(h, lease, draft("Used the owner-supplied accepted source."))["status"]
        == "completed"
    )
    prompt = h.calls[0].content.decode()
    assert "Owner-supplied accepted source candidate." in prompt


def test_independent_review_is_separate_board_work_assigned_to_the_reviewer():
    h = setup_artifacts()
    artifact, _ = upload(h)
    result = review(h, artifact)
    task = h.store.native_task(h.workspace, h.first.id, UUID(result["review_task_id"]))
    assert task.id != h.task.id and task.assignment.agent_id == h.reviewer.id
    assert task.status == "done"
    assert result["revision_id"] in task.description and result["sha256"] in task.description
    assert h.store.native_task(h.workspace, h.first.id, h.task.id).status == "in_review"
    assert len(h.calls) == 1


def test_cancelling_review_task_fences_late_result_and_keeps_known_provider_charge():
    h = setup_artifacts(paid=True)
    artifact, _ = upload(h)
    revision_value = current_revision(detail(h, artifact))

    def cancel_review_work(_request):
        h.hook = None
        attempt = h.store.artifact_reviews(h.workspace, h.first.id, UUID(revision_value["id"]))[0]
        task = h.store.native_task(h.workspace, h.first.id, attempt.review_task_id)
        assert task.status == "in_progress"
        h.projects.update_task(
            h.owner_actor, h.first.id, task.id, task_update(task, status="cancelled")
        )

    h.hook = cancel_review_work
    result = review(h, artifact)
    assert result["status"] == "stale" and result["verdict"] is None
    review_task = h.store.native_task(h.workspace, h.first.id, UUID(result["review_task_id"]))
    assert review_task.status == "cancelled"
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))[0]
    assert usage.status == "settled" and usage.charged_microusd == 120
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    assert not detail(h, artifact)["decisions"]


def test_review_route_change_during_dispatch_fences_result_and_keeps_known_charge():
    h = setup_artifacts(paid=True)
    h.templates.append(template(h.workspace, identifier="alternate", paid=True))
    alternate = check(h, enroll(h, template_id="alternate", label="Alternate reviewer"))
    policy = h.usage.policy(h.workspace, h.first.id)
    policy = policy.model_copy(
        update={"version": policy.version + 1, "review_model_id": UUID(h.model["id"])}
    )
    h.store.save_model_resource_policy(policy, expected_version=policy.version - 1)
    artifact, _ = upload(h)
    h.calls.clear()

    def change_route(_request):
        h.hook = None
        h.store.save_model_resource_policy(
            policy.model_copy(
                update={"version": policy.version + 1, "review_model_id": UUID(alternate["id"])}
            ),
            expected_version=policy.version,
        )

    h.hook = change_route
    result = review(h, artifact)
    assert result["status"] == "stale" and result["verdict"] is None
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))[0]
    assert usage.model_id == UUID(h.model["id"])
    assert usage.status == "settled" and usage.charged_microusd == 120
    assert usage.held_microusd == 0 and len(h.calls) == 1
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    assert not detail(h, artifact)["decisions"]


@pytest.mark.parametrize("workspace_policy", [False, True], ids=["project", "workspace"])
def test_resource_pause_during_review_fences_result_and_preserves_known_cost(workspace_policy):
    h = setup_artifacts(paid=True)
    artifact, _ = upload(h)
    scope = None if workspace_policy else h.first.id

    def pause_resource_policy(_request):
        h.hook = None
        policy = h.usage.policy(h.workspace, scope)
        h.store.save_model_resource_policy(
            policy.model_copy(update={"version": policy.version + 1, "paused": True}),
            expected_version=policy.version,
        )

    h.hook = pause_resource_policy
    result = review(h, artifact)
    assert result["status"] == "stale" and result["verdict"] is None
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(result["id"]))[0]
    assert usage.status == "settled" and usage.charged_microusd == 120
    assert usage.held_microusd == 0 and len(h.calls) == 1
    with pytest.raises(InvalidTransitionError):
        decide(h, artifact)
    assert not detail(h, artifact)["decisions"]


@pytest.mark.parametrize("operation", ["review", "repair"])
def test_full_length_command_keys_with_same_prefix_create_distinct_linked_work(operation):
    h = setup_artifacts()
    first, _ = upload(h, b"First deliverable.")
    other_task = new_task(h, title="Other independent deliverable")
    second, _ = upload(h, b"Second deliverable.", task=other_task)
    prefix = "key-" + "x" * 195
    identifiers = []
    for artifact, suffix in ((first, "a"), (second, "b")):
        command_key = prefix + suffix
        assert len(command_key) == 200
        if operation == "review":
            result = review(
                h, artifact, command=target_command(h, artifact, idempotency_key=command_key)
            )
            assert result["status"] == "completed"
            task_id = UUID(result["review_task_id"])
        else:
            result, _ = decide(h, artifact, approved=False, idempotency_key=command_key)
            task_id = UUID(result["repair_task_id"])
        task = h.store.native_task(h.workspace, h.first.id, task_id)
        assert artifact["id"] in task.description
        identifiers.append(task.id)
    assert identifiers[0] != identifiers[1]
    assert len(h.calls) == (2 if operation == "review" else 0)


@pytest.mark.parametrize("limit", ["complete_context", "model_capacity"])
def test_review_admission_limits_fail_safely_without_tasks_receipts_usage_or_dispatch(limit):
    h = setup_artifacts()
    if limit == "complete_context":
        h.projects.update_task(
            h.owner_actor, h.first.id, h.task.id, task_update(h.task, description="T" * 8000)
        )
        h.teams.update(
            h.owner_actor,
            h.first.id,
            h.reviewer.id,
            revision(h.reviewer, instructions="R" * 8000),
        )
        content = (b"private-review-evidence-" * 2200)[:48000]
        message = "review context limit"
    else:
        h.templates[0] = template(h.workspace, context_window_tokens=4096, max_output_tokens=1024)
        h.model = check(h, h.model)
        content = b"private-review-evidence-" * 200
        message = "enough context capacity"
    artifact, _ = upload(h, content)
    command = target_command(h, artifact)
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    usages = h.store.model_usage_entries(h.workspace, h.first.id)
    h.calls.clear()
    with pytest.raises(ValidationError, match=message):
        h.artifacts.review(h.owner_actor, h.first.id, UUID(artifact["id"]), command)
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks
    assert h.store.model_usage_entries(h.workspace, h.first.id) == usages
    assert not detail(h, artifact)["reviews"] and not h.calls
    with pytest.raises(NotFoundError):
        h.artifacts.operation_receipt(h.owner_actor, h.first.id, command.idempotency_key)
