"""Artifact commands reject caller-controlled authority and ambiguous review targets."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.native_artifact_commands import (
    DecideNativeArtifact,
    ReviewNativeArtifact,
    UpdateArtifactReviewPolicy,
    UploadNativeArtifact,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_version": True},
        {"expected_policy_version": True},
        {"expected_task_version": False},
        {"sha256": "g" * 64},
        {"sha256": "a" * 63},
        {"workspace_id": str(uuid4())},
        {"requested_by": str(uuid4())},
        {"model_id": str(uuid4())},
        {"verdict": "approved"},
    ],
)
def test_review_commands_reject_ambiguous_or_caller_supplied_authority(changes):
    with pytest.raises(ValidationError):
        ReviewNativeArtifact(
            **{
                "expected_version": 1,
                "expected_policy_version": 0,
                "expected_task_version": 1,
                "revision_id": uuid4(),
                "sha256": "a" * 64,
                "idempotency_key": "artifact-exact-command",
                **changes,
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"require_human_approval": False, "require_agent_review": False},
        {"max_repair_cycles": -1},
        {"max_repair_cycles": 9},
        {"max_repair_cycles": True},
        {"expected_version": True},
        {"decided_by": str(uuid4())},
        {"workspace_id": str(uuid4())},
    ],
)
def test_review_policy_requires_bounded_repairs_and_independent_automatic_review(changes):
    with pytest.raises(ValidationError):
        UpdateArtifactReviewPolicy(
            **{"expected_version": 0, "idempotency_key": "artifact-policy-command", **changes}
        )


def test_change_request_requires_feedback_and_upload_errors_do_not_echo_private_content():
    with pytest.raises(ValidationError):
        DecideNativeArtifact(
            expected_version=1,
            expected_policy_version=0,
            expected_task_version=1,
            revision_id=uuid4(),
            sha256="a" * 64,
            decision="changes_requested",
            feedback=" \n ",
            idempotency_key="artifact-feedback-command",
        )
    with pytest.raises(ValidationError) as error:
        UploadNativeArtifact(
            task_id=uuid4(),
            expected_version=0,
            expected_task_version=1,
            filename="private-source-content\r\n.txt",
            content_base64="private-artifact-content",
            idempotency_key="artifact-upload-command",
        )
    assert "private-source-content" not in str(error.value)
    assert "private-artifact-content" not in str(error.value)
