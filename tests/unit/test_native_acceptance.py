"""Registered local editable package checks never replace exact owner approval."""

import hashlib
import json
from uuid import UUID, uuid4

import pytest

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    StorageQuotaExceededError,
    ValidationError,
)
from simon.domain.models import utc_now
from simon.domain.native_acceptance import (
    AcceptDeliveryPacket,
    AssignDeliveryProfile,
    ValidateDeliveryPacket,
)
from simon.domain.native_delivery import manifest_bytes
from simon.services.native_acceptance import NativeAcceptanceService
from simon.services.native_dependencies import NativeDependencyService
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_artifacts import current_revision, decide, detail, key, upload
from tests.unit.test_native_delivery import capture, setup_delivery, unpack
from tests.unit.test_native_dependencies import declare, fact_edge, save_fact
from tests.unit.test_native_execution import new_task
from tests.unit.test_storage_workflows import limit

SCHEMA = {
    "type": "object",
    "properties": {"unit_price": {"type": "number", "minimum": 0}, "color": {"type": "string"}},
    "required": ["unit_price", "color"],
    "additionalProperties": False,
}


def setup_package(*, specification='{"unit_price":20,"color":"navy"}', report=None, evidence=False):
    h = setup_delivery()
    h.acceptance = NativeAcceptanceService(h.store, h.delivery)
    h.dependencies = NativeDependencyService(h.store, h.artifacts)
    h.fact = None
    if evidence:
        h.evidence = source(
            h,
            text='{"unit_price":20,"color":"navy"}',
            source_key="prices.json",
            media_type="application/json",
            sha256=hashlib.sha256(b"initial source").hexdigest(),
        )
        h.store.insert_native_intake_source(h.evidence)
        h.fact = save_fact(
            h,
            20,
            sources=[
                {
                    "source_revision_id": str(h.evidence.id),
                    "sha256": h.evidence.sha256,
                    "selector": {"kind": "json_pointer", "path": "/unit_price"},
                }
            ],
        )
    h.files = []
    for name, media, text in (
        ("spec.json", "application/json", specification),
        (
            "rationale.md",
            "text/markdown",
            "# Rationale\nCurrent specification and remaining limitations.\n",
        ),
        (
            "schema.json",
            "application/json",
            json.dumps(report or {"schema_version": 1, "schema": SCHEMA}),
        ),
    ):
        artifact, _ = upload(h, text.encode(), task=new_task(h), filename=name, media_type=media)
        declare(h, artifact, [fact_edge(h.fact)] if h.fact and name == "spec.json" else [])
        decide(h, artifact)
        h.files.append(detail(h, artifact)["artifact"])
    h.packet, h.packet_command = capture(h, *h.files)
    h.packet_id = UUID(h.packet["packet"]["id"])
    h.assign_command = AssignDeliveryProfile(
        expected_version=0,
        roles=tuple(
            {"role": role, "revision_id": current_revision(detail(h, artifact))["id"]}
            for role, artifact in zip(
                ("json_specification", "markdown_rationale", "schema_and_check_report"),
                h.files,
                strict=True,
            )
        ),
        probe={
            "selector": {"kind": "json_pointer", "path": "/unit_price"},
            "before": 20,
            "after": 25,
        },
        **key(),
    )
    h.calls.clear()
    return h


def assign(h, command=None):
    return h.acceptance.assign(h.owner_actor, h.first.id, h.packet_id, command or h.assign_command)


def validate(h, value=None, **changes):
    value = value or h.acceptance.status(h.owner_actor, h.first.id, h.packet_id)
    command = ValidateDeliveryPacket(
        assignment_id=value["assignment"]["id"],
        expected_profile_version=value["assignment"]["version"],
        manifest_sha256=h.packet["packet"]["manifest_sha256"],
        **{**key(), **changes},
    )
    return h.acceptance.validate(h.owner_actor, h.first.id, h.packet_id, command), command


def accept(h, value, command=None):
    command = command or AcceptDeliveryPacket(
        assignment_id=value["assignment"]["id"],
        expected_profile_version=value["assignment"]["version"],
        manifest_sha256=h.packet["packet"]["manifest_sha256"],
        validation_id=value["latest_validation"]["id"],
        authority_sha256=value["latest_validation"]["authority_sha256"],
        **key(),
    )
    return h.acceptance.accept(h.owner_actor, h.first.id, h.packet_id, command), command


def test_edit_save_reopen_render_binds_originals_and_requires_explicit_package_acceptance():
    h = setup_package()
    original = h.store.delivery_packet(h.workspace, h.first.id, h.packet_id)
    value = assign(h)
    assert not value["applicability"]["accepted"]
    value, validation_command = validate(h, value)
    report = value["latest_validation"]
    assert report["passed"] and report["evidence"]["probe"]["unrelated_data_preserved"]
    assert report["evidence"]["probe"]["published"] is False
    assert value["can_accept"] and not value["applicability"]["accepted"]
    accepted, command = accept(h, value)
    assert accepted["applicability"] == {"current": True, "accepted": True, "reasons": []}
    assert h.store.delivery_packet(h.workspace, h.first.id, h.packet_id) == original
    assert (
        h.acceptance.validate(h.owner_actor, h.first.id, h.packet_id, validation_command)[
            "latest_validation"
        ]["id"]
        == report["id"]
    )
    assert (
        accept(h, value, command)[0]["latest_acceptance"]["id"]
        == accepted["latest_acceptance"]["id"]
    )
    packet, _, files = unpack(h, h.packet)
    assert files["manifest.json"] == manifest_bytes(packet.manifest)
    assert any(name.startswith("package/validation/") for name in files)
    assert any(name.startswith("package/acceptance/") for name in files)
    assert not h.calls


@pytest.mark.parametrize(
    "fault",
    [
        "missing_role",
        "duplicate_role",
        "invalid_json",
        "schema_failure",
        "reference",
        "claimed_pass",
        "probe_before",
        "probe_after",
        "unknown_validator",
    ],
)
def test_registered_profile_faults_fail_closed_with_durable_server_results(fault):
    options = {}
    if fault == "invalid_json":
        options["specification"] = "{invalid}"
    if fault == "schema_failure":
        options["specification"] = '{"unit_price":"twenty","color":"navy"}'
    if fault == "reference":
        options["report"] = {
            "schema_version": 1,
            "schema": {"$ref": "https://unavailable.example/schema"},
        }
    if fault == "claimed_pass":
        options["report"] = {"schema_version": 1, "schema": SCHEMA, "passed": True}
    h = setup_package(**options)
    command = h.assign_command
    if fault == "missing_role":
        command = command.model_copy(update={"roles": command.roles[:2]})
    if fault == "duplicate_role":
        command = command.model_copy(
            update={"roles": (command.roles[0], command.roles[0], command.roles[2])}
        )
    if fault == "probe_before":
        command = command.model_copy(
            update={"probe": command.probe.model_copy(update={"before": 99})}
        )
    if fault == "probe_after":
        command = command.model_copy(
            update={"probe": command.probe.model_copy(update={"after": "invalid numeric edit"})}
        )
    value = assign(h, command)
    if fault == "unknown_validator":
        with pytest.raises(ValidationError):
            validate(h, value, validator_version=99)
        return
    value, _ = validate(h, value)
    assert not value["latest_validation"]["passed"]
    assert any(c["status"] == "failed" for c in value["latest_validation"]["checks"])
    with pytest.raises(InvalidTransitionError):
        accept(h, value)
    assert not h.calls


def test_profile_changes_stale_owner_acceptance_and_old_validation_cannot_approve():
    h = setup_package()
    value, _ = validate(h, assign(h))
    accept(h, value)
    changed = h.assign_command.model_copy(
        update={"expected_version": 1, "idempotency_key": str(uuid4())}
    )
    value = assign(h, changed)
    assert value["assignment"]["version"] == 2
    assert not value["applicability"]["accepted"]
    with pytest.raises(InvalidTransitionError):
        accept(h, value)
    with pytest.raises(ValidationError):
        assign(
            h,
            changed.model_copy(
                update={
                    "expected_version": 2,
                    "profile_version": 99,
                    "idempotency_key": str(uuid4()),
                }
            ),
        )


def test_semantically_unrelated_fact_and_source_changes_preserve_complete_package_acceptance():
    h = setup_package(evidence=True)
    value, _ = validate(h, assign(h))
    accept(h, value)
    save_fact(h, "unrelated", key="brand", value_type="string", unit="")
    h.store.insert_native_intake_source(source(h, source_key="unrelated.txt"))
    unchanged = source(
        h,
        source_key=h.evidence.source_key,
        revision=2,
        text='{"unit_price":20,"color":"green"}',
        sha256="b" * 64,
    )
    h.store.insert_native_intake_source(unchanged)
    assert h.acceptance.status(h.owner_actor, h.first.id, h.packet_id)["applicability"]["accepted"]
    changed = unchanged.model_copy(
        update={
            "id": uuid4(),
            "revision": 3,
            "sha256": "c" * 64,
            "text": '{"unit_price":25,"color":"green"}',
        }
    )
    h.store.insert_native_intake_source(changed)
    assert not h.acceptance.status(h.owner_actor, h.first.id, h.packet_id)["applicability"][
        "accepted"
    ]


def test_revoked_evidence_withholds_reports_export_and_receipt():
    h = setup_package(evidence=True)
    value, _ = validate(h, assign(h))
    accept(h, value)
    h.store.revoke_native_intake_source(h.workspace, h.first.id, h.evidence.id, utc_now())
    value = h.acceptance.detail(h.owner_actor, h.first.id, h.packet_id)
    assert value["disclosure"] == "withheld" and value["validations"] == []
    assert value["latest_validation"] is None and not value["can_accept"]
    with pytest.raises(AuthorizationError):
        h.acceptance.export_evidence(h.owner_actor, h.first.id, h.packet_id)
    with pytest.raises(AuthorizationError):
        h.acceptance.operation_receipt(
            h.owner_actor, h.first.id, h.packet_id, h.assign_command.idempotency_key
        )


def test_retained_validation_quota_failure_rolls_back_receipt_and_allows_same_command_retry():
    h = setup_package()
    value = assign(h)
    usage = h.store.storage_usage(h.workspace)
    limit(h, usage["used_bytes"])
    command = ValidateDeliveryPacket(
        assignment_id=value["assignment"]["id"],
        expected_profile_version=1,
        manifest_sha256=h.packet["packet"]["manifest_sha256"],
        **key(),
    )
    with pytest.raises(StorageQuotaExceededError):
        h.acceptance.validate(h.owner_actor, h.first.id, h.packet_id, command)
    assert h.store.delivery_validations(h.workspace, h.first.id, h.packet_id) == ()
    assert h.store.storage_usage(h.workspace) == usage
    limit(h, usage["used_bytes"] + 100_000)
    result = h.acceptance.validate(h.owner_actor, h.first.id, h.packet_id, command)
    assert result["latest_validation"]["passed"]


def test_export_bound_failure_rolls_back_evidence_charge_receipt_and_fresh_packet_is_available(
    monkeypatch,
):
    from simon.domain.errors import NotFoundError
    from simon.services.native_acceptance import MAX_PACKAGE_EXPORT_BYTES

    h = setup_package()
    value = assign(h)
    existing_size = sum(
        len(raw)
        for raw in h.acceptance.export_evidence(h.owner_actor, h.first.id, h.packet_id).values()
    )
    before = h.store.storage_usage(h.workspace)
    command = ValidateDeliveryPacket(
        assignment_id=value["assignment"]["id"],
        expected_profile_version=1,
        manifest_sha256=h.packet["packet"]["manifest_sha256"],
        **key(),
    )
    monkeypatch.setattr(
        "simon.services.native_acceptance.MAX_PACKAGE_EXPORT_BYTES", existing_size + 1
    )
    with pytest.raises(ValidationError, match="export bound"):
        h.acceptance.validate(h.owner_actor, h.first.id, h.packet_id, command)
    assert h.store.delivery_validations(h.workspace, h.first.id, h.packet_id) == ()
    assert h.store.storage_usage(h.workspace) == before
    with pytest.raises(NotFoundError):
        h.acceptance.operation_receipt(
            h.owner_actor, h.first.id, h.packet_id, command.idempotency_key
        )
    monkeypatch.setattr(
        "simon.services.native_acceptance.MAX_PACKAGE_EXPORT_BYTES", MAX_PACKAGE_EXPORT_BYTES
    )
    fresh, _ = capture(h, *h.files, previous_packet_id=h.packet_id)
    h.packet, h.packet_id = fresh, UUID(fresh["packet"]["id"])
    result, _ = validate(h, assign(h))
    assert accept(h, result)[0]["applicability"]["accepted"]


@pytest.mark.parametrize(
    "case", ["preview", "stale_task", "missing_original", "missing_dependency"]
)
def test_preview_original_dependency_and_component_approval_faults_block_package_acceptance(
    case, monkeypatch
):
    from simon.domain.errors import NotFoundError

    h = setup_package()
    if case == "preview":
        original = __import__(
            "simon.services.native_delivery", fromlist=["render_preview"]
        ).render_preview

        def bad_preview(revision, raw):
            metadata, content = original(revision, raw)
            return metadata.model_copy(update={"content_sha256": "f" * 64}), content

        monkeypatch.setattr("simon.services.native_delivery.render_preview", bad_preview)
        h.packet, _ = capture(h, *h.files)
        h.packet_id = UUID(h.packet["packet"]["id"])
    elif case == "stale_task":
        task_id = UUID(h.files[0]["task_id"])
        task = h.store.native_task(h.workspace, h.first.id, task_id)
        h.store.update_native_task(
            task.model_copy(update={"version": task.version + 1, "description": "New requirement"}),
            task.version,
        )
    elif case == "missing_original":
        revision_id = UUID(h.assign_command.roles[0].revision_id.hex)
        h.store._artifact_records["native_artifact_revisions"].pop(revision_id)
        with pytest.raises(NotFoundError):
            assign(h)
        return
    else:
        declare(h, h.files[0], coverage="partial")
    result, _ = validate(h, assign(h))
    if case == "preview":
        assert not result["latest_validation"]["passed"]
    with pytest.raises(InvalidTransitionError):
        accept(h, result)


def test_non_apparel_registered_json_field_reopens_and_preserves_unrelated_data():
    from simon.domain.native_acceptance import PackageEditProbe

    schema = {
        "type": "object",
        "properties": {"inspection_days": {"type": "integer"}, "equipment": {"type": "string"}},
        "required": ["inspection_days", "equipment"],
        "additionalProperties": False,
    }
    h = setup_package(
        specification='{"inspection_days":90,"equipment":"pump-a"}',
        report={"schema_version": 1, "schema": schema},
    )
    command = h.assign_command.model_copy(
        update={
            "probe": PackageEditProbe(
                selector={"kind": "json_pointer", "path": "/inspection_days"}, before=90, after=60
            )
        }
    )
    value, _ = validate(h, assign(h, command))
    assert value["latest_validation"]["evidence"]["probe"]["unrelated_data_preserved"]
    assert accept(h, value)[0]["applicability"]["accepted"]


def test_historical_profile_and_validator_definitions_remain_exact_after_runtime_profile_change(
    monkeypatch,
):
    from simon.services.native_acceptance import PROFILE

    h = setup_package()
    value, _ = validate(h, assign(h))
    accept(h, value)
    exported = h.acceptance.export_evidence(h.owner_actor, h.first.id, h.packet_id)
    profile_hash = value["assignment"]["profile_sha256"]
    validator_hash = value["latest_validation"]["validator_sha256"]
    profile_path = f"package/profile-definitions/{profile_hash}.json"
    validator_path = f"package/validator-definitions/{validator_hash}.json"
    assert hashlib.sha256(exported[profile_path]).hexdigest() == profile_hash
    assert hashlib.sha256(exported[validator_path]).hexdigest() == validator_hash
    monkeypatch.setitem(PROFILE, "application", "Changed future supported application")
    exported_later = h.acceptance.export_evidence(h.owner_actor, h.first.id, h.packet_id)
    assert exported_later[profile_path] == exported[profile_path]
    assert exported_later[validator_path] == exported[validator_path]
    assert exported_later["package/profile.json"] == exported["package/profile.json"]
    assert not h.acceptance.status(h.owner_actor, h.first.id, h.packet_id)["applicability"][
        "accepted"
    ]
