"""Package commands cannot submit verdicts; immutable evidence validates its checksums."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.native_acceptance import (
    NativeDeliveryValidation,
    PackageEditProbe,
    ValidateDeliveryPacket,
)
from simon.domain.native_dependencies import snapshot_hash


def validation(**changes):
    authority = {"project_version": 1, "source": "metadata only"}
    return NativeDeliveryValidation(
        **{
            "workspace_id": uuid4(),
            "project_id": uuid4(),
            "packet_id": uuid4(),
            "manifest_sha256": "a" * 64,
            "created_by": uuid4(),
            "assignment_id": uuid4(),
            "profile_version": 1,
            "profile_sha256": "b" * 64,
            "validator_key": "simon.editable-text-json",
            "validator_version": 1,
            "validator_sha256": snapshot_hash({"key": "simon.editable-text-json", "version": 1}),
            "validator_snapshot": {"key": "simon.editable-text-json", "version": 1},
            "passed": True,
            "checks": ({"name": "server_check", "status": "passed"},),
            "evidence": {},
            "authority": authority,
            "authority_sha256": snapshot_hash(authority),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": False},
        {"passed": 1},
        {"authority_sha256": "f" * 64},
        {
            "checks": (
                {"name": "duplicate", "status": "passed"},
                {"name": "duplicate", "status": "passed"},
            )
        },
        {"evidence": {"oversized": "😀" * 70_000}},
    ],
)
def test_validation_verdict_check_identity_hash_and_utf8_byte_bounds(changes):
    with pytest.raises(ValidationError):
        validation(**changes)


def test_request_cannot_supply_passed_check_results():
    with pytest.raises(ValidationError):
        ValidateDeliveryPacket(
            assignment_id=uuid4(),
            expected_profile_version=1,
            manifest_sha256="a" * 64,
            idempotency_key="caller-claim",
            passed=True,
        )


@pytest.mark.parametrize("selector", [{"kind": "whole_file"}, {"kind": "json_pointer", "path": ""}])
def test_edit_probe_requires_existing_field_instead_of_entire_document(selector):
    with pytest.raises(ValidationError):
        PackageEditProbe(selector=selector, before={}, after={})


def test_edit_probe_preserves_significant_pointer_whitespace_and_owned_values():
    value = {"owned": [1]}
    probe = PackageEditProbe(
        selector={"kind": "json_pointer", "path": "/label "}, before=value, after={}
    )
    value["owned"].clear()
    assert probe.selector.path == "/label " and probe.before == {"owned": [1]}
