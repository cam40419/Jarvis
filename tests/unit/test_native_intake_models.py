"""Model output is bounded and cannot smuggle team authority into staffing proposals."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.native_intake import (
    AnalyzeIntake,
    Citation,
    Finding,
    IntakeRun,
    IntakeSource,
    IntakeTask,
    NativeIntake,
    ProposalReview,
    Question,
    SourceUpload,
    StaffingProposal,
    StaffingRole,
    UpdateNativeIntake,
)


def role(**changes):
    return StaffingRole(
        **(
            {
                "role_key": "researcher",
                "action": "create",
                "name": "Researcher",
                "instructions": "Review the evidence",
                "success_criteria": "Cite sources",
                "rationale": "Evidence needs review",
                "need": "specialist",
                "reuse_assessment": "No existing research capability",
            }
            | changes
        )
    )


def task(**changes):
    return IntakeTask(
        **(
            {
                "key": "review-evidence",
                "title": "Review evidence",
                "description": "Compare sources",
                "acceptance": "Every claim has evidence",
                "assignment": "agent",
                "role_key": "researcher",
            }
            | changes
        )
    )


@pytest.mark.parametrize("value", [True, 1.5, "12", float("inf"), -1, 1_000_000_001])
def test_intake_budget_requires_bounded_integer_microusd(value):
    with pytest.raises(ValidationError):
        UpdateNativeIntake(budget_microusd=value, expected_version=0, idempotency_key="save-intake")


def test_answers_are_bounded_named_answers_without_invalid_unicode():
    base = {"workspace_id": uuid4(), "project_id": uuid4()}
    for answers in (
        {"not a slug": "Answer"},
        {"goal": "a" * 2001},
        {f"answer-{index}": "value" for index in range(13)},
        {"goal": "invalid\x00text"},
        {"goal": "\ud800"},
    ):
        with pytest.raises(ValidationError):
            NativeIntake(**base, answers=answers)
    assert NativeIntake(**base, answers={"goal": "Useful answer"}).answers == {
        "goal": "Useful answer"
    }


@pytest.mark.parametrize(
    "key", ["/private/file", "C:/private/file", "../file", "dir/../file", "dir//file", "dir\\file"]
)
def test_source_upload_accepts_only_relative_display_labels(key):
    with pytest.raises(ValidationError):
        SourceUpload(
            source_key=key,
            filename="file.txt",
            media_type="text/plain",
            content_base64="",
            expected_version=1,
            idempotency_key="upload-source",
        )


@pytest.mark.parametrize(
    "filename", ["../file", "dir/file", "dir\\file", "file\r\nHeader", ".", ".."]
)
def test_source_upload_filenames_cannot_inject_paths_or_headers(filename):
    with pytest.raises(ValidationError):
        SourceUpload(
            source_key="file",
            filename=filename,
            media_type="text/plain",
            content_base64="",
            expected_version=1,
            idempotency_key="upload-source",
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"can_manage_team": True},
        {"scopes": ["team:manage"]},
        {"action": "reuse"},
        {"action": "reuse", "agent_id": uuid4()},
        {"agent_id": uuid4()},
        {"need": "reuse"},
        {"reuse_assessment": ""},
    ],
)
def test_role_output_cannot_grant_authority_or_hide_reuse_decisions(changes):
    with pytest.raises(ValidationError):
        role(**changes)


def test_reuse_and_new_role_have_distinct_explicit_semantics():
    existing_id = uuid4()
    reused = role(action="reuse", agent_id=existing_id, need="reuse")
    assert reused.agent_id == existing_id
    assert role().agent_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"role_key": None},
        {"assignment": "human"},
        {"assignment": "pool"},
        {"review_role_key": "researcher"},
        {"lease_id": uuid4()},
    ],
)
def test_task_output_names_exactly_its_assignee_and_independent_reviewer(changes):
    with pytest.raises(ValidationError):
        task(**changes)


def test_proposal_keys_are_unique_and_evidence_required_for_claims():
    evidence = Citation(source_id=uuid4(), quote="Quoted evidence")
    assert Finding(kind="fact", statement="Grounded", evidence=(evidence,)).evidence == (evidence,)
    assert Finding(kind="assumption", statement="Unresolved inference").evidence == ()
    for kind in ("fact", "conflict"):
        with pytest.raises(ValidationError):
            Finding(kind=kind, statement="Unsupported claim")
    question = Question(key="audience", question="Who?", why="Needed for planning", blocking=True)
    for changes in (
        {"roles": (role(), role())},
        {"tasks": (task(), task())},
        {"questions": (question, question)},
    ):
        with pytest.raises(ValidationError):
            StaffingProposal(summary="Plan", next_milestone="Next", **changes)


def test_review_approval_and_issues_cannot_contradict_each_other():
    assert ProposalReview(approved=True).issues == ()
    assert not ProposalReview(approved=False, issues=("Unsupported claim",)).approved
    for approved, issues in ((True, ("Unsupported claim",)), (False, ()), (False, ("\x00",))):
        with pytest.raises(ValidationError):
            ProposalReview(approved=approved, issues=issues)


def test_source_and_attempt_times_are_utc_and_cannot_reverse_history():
    start = datetime(2026, 10, 7, 12, tzinfo=timezone(timedelta(hours=-4)))
    values = {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "requested_by": uuid4(),
        "idempotency_key": "plan-attempt",
        "request_digest": "a" * 64,
        "snapshot_digest": "b" * 64,
        "intake_version": 1,
        "endpoint_id": "local",
        "model": "model",
        "started_at": start,
        "deadline_at": start + timedelta(minutes=3),
    }
    assert IntakeRun(**values).started_at == start.astimezone(UTC)
    for changes in (
        {"deadline_at": start},
        {"finished_at": start - timedelta(seconds=1)},
        {"reserved_microusd": True},
        {"charged_microusd": -1},
        {"error_code": "secret body!"},
    ):
        with pytest.raises(ValidationError):
            IntakeRun(**(values | changes))
    source_values = {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "source_key": "file.txt",
        "filename": "file.txt",
        "media_type": "text/plain",
        "revision": 1,
        "sha256": "c" * 64,
        "size_bytes": 0,
        "extraction_status": "unparsed",
        "created_by": uuid4(),
        "created_at": start,
    }
    with pytest.raises(ValidationError):
        IntakeSource(**source_values, revoked_at=start - timedelta(seconds=1))
    assert IntakeSource(**source_values).created_at.tzinfo == UTC
    with pytest.raises(ValidationError):
        AnalyzeIntake(
            expected_version=1,
            idempotency_key="analyze-sources",
            source_ids=tuple(uuid4() for _ in range(13)),
        )
