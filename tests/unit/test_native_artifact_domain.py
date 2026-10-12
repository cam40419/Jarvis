"""Artifact contracts bind hashes to exact arbitrary bytes and bounded authority."""

import base64
import hashlib
from datetime import timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.models import utc_now
from simon.domain.native_artifacts import (
    MAX_ARTIFACT_BYTES,
    NativeArtifactDecision,
    NativeArtifactRevision,
    NativeReviewPolicy,
)


def artifact_revision(content=b"\x00\xff\x01image bytes", **changes):
    return NativeArtifactRevision(
        **{
            "workspace_id": uuid4(),
            "project_id": uuid4(),
            "artifact_id": uuid4(),
            "task_id": uuid4(),
            "sequence": 1,
            "filename": "generic.bin",
            "media_type": "application/octet-stream",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "created_by": uuid4(),
            **changes,
        }
    )


def test_generic_binary_and_largest_content_have_exact_roundtrip():
    for content in (b"", b"\x00\xff\x01", b"x" * MAX_ARTIFACT_BYTES):
        revision = artifact_revision(content)
        assert base64.b64decode(revision.content_base64) == content
        assert NativeArtifactRevision.model_validate_json(revision.model_dump_json()) == revision
        assert revision.content_base64 not in repr(revision) or not content
    with pytest.raises(ValidationError):
        artifact_revision(b"x" * (MAX_ARTIFACT_BYTES + 1))


@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "../escape.bin"},
        {"filename": "C:payload.bin"},
        {"filename": "payload\r\nHeader: unsafe"},
        {"filename": "\x00"},
        {"filename": ".."},
        {"media_type": "text/html; charset=utf-8"},
        {"media_type": "text/plain\r\nHeader"},
        {"sha256": "A" * 64},
        {"sha256": "a" * 64},
        {"size_bytes": 500},
        {"content_base64": "AA!="},
        {"content_base64": "AB==", "size_bytes": 1, "sha256": hashlib.sha256(b"\x00").hexdigest()},
        {"parent_revision_id": uuid4()},
        {"sequence": 2},
        {"created_by_agent_id": uuid4()},
        {"created_by": None},
    ],
)
def test_revision_rejects_unsafe_or_inconsistent_metadata(changes):
    with pytest.raises(ValidationError):
        artifact_revision(**changes)


def test_media_type_normalization_and_timestamp_utc():
    now = utc_now()
    revision = artifact_revision(media_type="APPLICATION/OCTET-STREAM", created_at=now)
    assert revision.media_type == "application/octet-stream"
    assert revision.created_at == now


def test_policy_keeps_an_acceptance_authority_and_bounded_repair_cycles():
    scope = {"workspace_id": uuid4(), "project_id": uuid4()}
    policy = NativeReviewPolicy(**scope)
    assert policy.require_human_approval and policy.require_agent_review
    for changes in (
        {"require_human_approval": False, "require_agent_review": False},
        {"max_repair_cycles": 9},
        {"version": True},
    ):
        with pytest.raises(ValidationError):
            NativeReviewPolicy(**scope, **changes)


def test_decision_records_distinguish_owner_approval_and_standing_authority():
    fields = {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "artifact_id": uuid4(),
        "revision_id": uuid4(),
        "sha256": "a" * 64,
        "policy_version": 0,
        "task_version": 1,
        "decided_by": uuid4(),
        "decision": "approved",
    }
    decision = NativeArtifactDecision(**fields)
    assert not decision.automatic
    automatic = NativeArtifactDecision(**fields, automatic=True, review_id=uuid4())
    assert automatic.automatic
    for changes in (
        {"automatic": True},
        {"repair_task_id": uuid4()},
        {"decision": "changes_requested"},
    ):
        with pytest.raises(ValidationError):
            NativeArtifactDecision(**{**fields, **changes})
    # Native UTC serialization is stable across a restored timestamp offset.
    timestamp = utc_now().astimezone(timezone(timedelta(hours=-4)))
    assert NativeArtifactDecision(
        **fields, created_at=timestamp
    ).created_at.utcoffset() == timedelta(0)
