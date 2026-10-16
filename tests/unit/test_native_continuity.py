"""Two human/native-worker succession retains exact checkpoints and live authority."""

import base64
import io
import json
import zipfile
from datetime import timedelta
from uuid import UUID

import pytest

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_continuity import (
    CreateContinuityHandoff,
    ResumeContinuityHandoff,
    VerifyContinuityBundle,
)
from simon.domain.native_execution import (
    EnrollExecutionRunner,
    RunnerLeaseCommand,
    UpdateExecutionPolicy,
)
from simon.domain.native_projects import NativeCommand, NativeProjectMember
from simon.domain.native_storage import StorageEntitlement
from simon.services.identity import ROLE_SCOPES
from simon.services.native_continuity import NativeContinuityService
from simon.services.native_dependencies import NativeDependencyService
from simon.services.native_execution import NativeExecutionService
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_artifact_dependencies import fact
from tests.unit.test_native_artifacts import decide, upload
from tests.unit.test_native_delivery import setup_delivery
from tests.unit.test_native_execution import (
    advance,
    claim,
    command_key,
    current,
    delegation,
    draft,
    start,
)
from tests.unit.test_native_teams import create_role
from tests.unit.test_project_models import check, enroll, setup_models


def setup_continuity(*, paid=False):
    if paid:
        h = setup_models(paid=True)
        h.model = check(h, enroll(h))
        h.agent = create_role(h)
        h.execution = NativeExecutionService(h.store, h.projects, h.models)
        h.execution.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateExecutionPolicy(
                expected_version=0, enabled=True, max_cost_microusd=1_000_000, **command_key()
            ),
        )
        first = h.execution.enroll_runner(
            h.owner_actor, h.first.id, EnrollExecutionRunner(name="First runtime", **command_key())
        )
        h.runner = h.execution.runner(first["token"])
        h.artifacts = h.execution.artifacts
        h.calls.clear()
    else:
        h = setup_delivery()
    h.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=h.workspace, project_id=h.first.id, actor_id=h.member, role="owner"
        )
    )
    h.recipient = ActorContext(
        actor_id=h.member,
        workspace_id=h.workspace,
        scopes=ROLE_SCOPES["member"],
        channel=Channel.API,
    )
    h.continuity = NativeContinuityService(h.store, h.execution)
    credential = h.execution.enroll_runner(
        h.recipient, h.first.id, EnrollExecutionRunner(name="Successor runtime", **command_key())
    )
    h.successor = h.execution.runner(credential["token"])
    h.successor_token = credential["token"]
    return h


def capture(h):
    command = CreateContinuityHandoff(
        recipient_id=h.recipient.actor_id, title="Exact takeover", **command_key()
    )
    saved = h.continuity.create(h.owner_actor, h.first.id, command)
    return saved, command


def resume_command(h, handoff, run, **changes):
    value = current(h, run)
    return ResumeContinuityHandoff(
        **{
            "manifest_sha256": handoff["handoff"]["manifest_sha256"],
            "run_id": value.id,
            "expected_run_version": value.version,
            "runner_id": h.successor.id,
            **command_key(),
            **changes,
        }
    )


def resume(h, handoff, run, command=None):
    return h.continuity.resume(
        h.recipient,
        h.first.id,
        UUID(handoff["handoff"]["id"]),
        command or resume_command(h, handoff, run),
    )


def completed_unapplied(h, monkeypatch, action=None):
    run = start(h)
    leased = claim(h)
    apply = h.execution._advance_checkpoint

    def interrupted(*args):
        raise RuntimeError("Runtime stopped after durable checkpoint")

    monkeypatch.setattr(h.execution, "_advance_checkpoint", interrupted)
    with pytest.raises(RuntimeError, match="durable checkpoint"):
        advance(h, leased, action or draft("Exact durable successor candidate."))
    monkeypatch.setattr(h.execution, "_advance_checkpoint", apply)
    value = current(h, run)
    assert value.step_number == 1 and value.applied_step == 0
    assert h.store.execution_steps(h.workspace, h.first.id, value.id)[0].status == "completed"
    h.execution._save(value, lease_until=utc_now() - timedelta(seconds=1))
    return run


def successor_step(h, lease):
    return h.execution.step(
        h.successor,
        RunnerLeaseCommand(
            run_id=lease["run_id"],
            fence=lease["fence"],
            lease_token=lease["lease_token"],
            **command_key(),
        ),
    )


def test_two_humans_two_workers_resume_exact_completed_checkpoint_without_redispatch(monkeypatch):
    h = setup_continuity()
    accepted, _ = upload(h, b"Previously accepted exact file.")
    decide(h, accepted)
    run = completed_unapplied(h, monkeypatch)
    assert len(h.calls) == 1
    handoff, creation = capture(h)
    manifest = handoff["handoff"]["manifest"]
    assert manifest["snapshot"]["artifacts"][0]["status"] == "accepted"
    assert manifest["snapshot"]["runs"][0]["checkpoints"][0]["status"] == "completed"
    assert "lease_token" not in json.dumps(manifest) and "token_hash" not in json.dumps(manifest)
    assert h.continuity.create(h.owner_actor, h.first.id, creation)["handoff"] == handoff["handoff"]
    command = resume_command(h, handoff, run)
    result = resume(h, handoff, run, command)
    assert result["status"] == "authorized" and not result["redispatched"]
    assert resume(h, handoff, run, command)["replayed"]
    assert h.execution.claim(h.runner, NativeCommand(**command_key())) is None
    lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
    assert lease["run_id"] == run["id"]
    assert successor_step(h, lease)["status"] == "completed"
    assert len(h.calls) == 1
    final = resume(h, handoff, run)
    assert final["status"] == "already_completed" and len(h.calls) == 1
    assert (
        h.store.continuity_handoff(
            h.workspace, h.first.id, UUID(handoff["handoff"]["id"])
        ).manifest.model_dump(mode="json")
        == manifest
    )


def test_revoked_source_withholds_all_snapshot_and_export_and_resume(monkeypatch):
    h = setup_continuity()
    evidence = source(h, text="Private source measurement", source_key="private.txt")
    h.store.insert_native_intake_source(evidence)
    run = completed_unapplied(h, monkeypatch)
    handoff, _ = capture(h)
    handoff_id = UUID(handoff["handoff"]["id"])
    h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
    withheld = h.continuity.detail(h.recipient, h.first.id, handoff_id)
    assert not withheld["disclosure_allowed"] and "manifest" not in withheld["handoff"]
    with pytest.raises(AuthorizationError, match="revoked"):
        h.continuity.download(h.recipient, h.first.id, handoff_id)
    with pytest.raises(AuthorizationError, match="revoked"):
        resume(h, handoff, run)
    assert len(h.calls) == 1


def test_source_revocation_after_resume_blocks_worker_checkpoint_publication(monkeypatch):
    h = setup_continuity()
    evidence = source(h)
    h.store.insert_native_intake_source(evidence)
    run = completed_unapplied(h, monkeypatch)
    handoff, _ = capture(h)
    resume(h, handoff, run)
    lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
    h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
    with pytest.raises(AuthorizationError, match="revoked"):
        successor_step(h, lease)
    assert current(h, run).applied_step == 0 and len(h.calls) == 1


def test_recipient_revocation_mid_resume_and_wrong_worker_cannot_publish(monkeypatch):
    h = setup_continuity()
    run = completed_unapplied(h, monkeypatch)
    handoff, _ = capture(h)
    resume(h, handoff, run)
    lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
    with pytest.raises(InvalidTransitionError, match="lease"):
        h.execution.step(
            h.runner,
            RunnerLeaseCommand(
                run_id=lease["run_id"],
                fence=lease["fence"],
                lease_token=lease["lease_token"],
                **command_key(),
            ),
        )
    h.store.delete_native_project_member(h.workspace, h.first.id, h.recipient.actor_id)
    with pytest.raises((AuthorizationError, NotFoundError)) as failure:
        successor_step(h, lease)
    assert "access" in str(failure.value).lower() or "not found" in str(failure.value).lower()
    assert current(h, run).applied_step == 0 and len(h.calls) == 1


def test_expired_successor_credential_and_foreign_runtime_are_blocked(monkeypatch):
    h = setup_continuity()
    run = start(h)
    handoff, _ = capture(h)
    moment = h.successor.expires_at + timedelta(seconds=1)
    monkeypatch.setattr("simon.services.native_workflows.utc_now", lambda: moment)
    with pytest.raises(AuthorizationError, match="expired"):
        resume(h, handoff, run)
    with pytest.raises(ValueError):
        resume_command(h, handoff, run, runtime="foreign")
    assert not h.calls


def test_unknown_provider_effect_and_held_liability_are_retained_and_block_resume():
    h = setup_continuity(paid=True)
    run = start(h)
    h.hook = lambda _: (_ for _ in ()).throw(TimeoutError("Unknown provider result"))
    assert advance(h, claim(h), draft())["status"] == "unknown"
    handoff, _ = capture(h)
    costs = handoff["handoff"]["manifest"]["snapshot"]["costs"]
    assert costs["held_microusd"] > 0 and costs["charged_microusd"] > 0
    assert any(entry["status"] == "unknown" for entry in costs["entries"])
    before = h.store.model_usage_entries(h.workspace, h.first.id)
    with pytest.raises(InvalidTransitionError, match="unknown provider effects"):
        resume(h, handoff, run)
    assert h.store.model_usage_entries(h.workspace, h.first.id) == before
    assert len(h.calls) == 1


def test_storage_downgrade_retains_handoff_and_checkpoint_and_does_not_repeat_model(monkeypatch):
    h = setup_continuity()
    run = completed_unapplied(h, monkeypatch)
    handoff, _ = capture(h)
    record_id = UUID(handoff["handoff"]["id"])
    charge = h.store.storage_charge(h.workspace, "continuity", record_id)
    h.store.save_storage_entitlement(
        StorageEntitlement(
            workspace_id=h.workspace,
            version=1,
            plan_key="restricted",
            limit_bytes=0,
            reason="Synthetic downgrade",
            issued_by=h.owner,
        ),
        0,
    )
    assert h.continuity.download(h.recipient, h.first.id, record_id)[1]
    resume(h, handoff, run)
    lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
    result = successor_step(h, lease)
    assert result["status"] == "failed" and result["error_code"] == "storage_quota_exceeded"
    assert h.store.storage_charge(h.workspace, "continuity", record_id) == charge
    assert (
        h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))[0].status == "completed"
    )
    assert len(h.calls) == 1


def test_export_verifies_exact_files_and_rejects_altered_file():
    h = setup_continuity()
    artifact, _ = upload(h, b"Exact original editable content.")
    decide(h, artifact)
    handoff, _ = capture(h)
    record_id = UUID(handoff["handoff"]["id"])
    _, raw = h.continuity.download(h.recipient, h.first.id, record_id)
    assert h.continuity.verify(
        h.recipient,
        h.first.id,
        record_id,
        VerifyContinuityBundle(content_base64=base64.b64encode(raw).decode()),
    )["valid"]
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(stream, "w") as altered:
        for name in original.namelist():
            altered.writestr(name, b"Altered" if name.startswith("files/") else original.read(name))
    with pytest.raises(ValidationError, match="changed"):
        h.continuity.verify(
            h.recipient,
            h.first.id,
            record_id,
            VerifyContinuityBundle(content_base64=base64.b64encode(stream.getvalue()).decode()),
        )


def test_delegated_child_inherits_parent_successor_worker_fence(monkeypatch):
    h = setup_continuity()
    run = completed_unapplied(h, monkeypatch, delegation(h, h.agent.id))
    handoff, _ = capture(h)
    resume(h, handoff, run)
    lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
    assert successor_step(h, lease)["status"] == "waiting"
    assert len(h.calls) == 1
    assert h.execution.claim(h.runner, NativeCommand(**command_key())) is None
    child = h.execution.claim(h.successor, NativeCommand(**command_key()))
    assert child["run_id"] != run["id"]
    h.response = json.dumps(draft("Successor-owned child evidence"))
    assert successor_step(h, child)["status"] == "completed"
    assert len(h.calls) == 2


def test_changed_fact_requires_recapture_and_new_binding_supersedes_stale_handoff(monkeypatch):
    h = setup_continuity()
    h.dependencies = NativeDependencyService(h.store, h.artifacts)
    original_fact = fact(h, 40)
    run = completed_unapplied(h, monkeypatch)
    original, _ = capture(h)
    resume(h, original, run)
    fact(h, 50, previous=original_fact)
    stale = h.continuity.detail(h.recipient, h.first.id, UUID(original["handoff"]["id"]))
    assert not stale["current"]
    assert any("Fact context changed" in reason for reason in stale["reasons"])
    # Ordinary execution reads/recovery must leave this cached checkpoint
    # available for explicit fresh context, rather than mark its core grant stale.
    h.execution.view(h.recipient, h.first.id)
    assert current(h, run).status == "queued"
    with pytest.raises(InvalidTransitionError, match="Fact context changed"):
        resume(h, original, run)
    replacement, _ = capture(h)
    assert replacement["handoff"]["manifest"]["snapshot"]["facts"][0]["revision"]["value"] == 50
    assert resume(h, replacement, run)["status"] == "authorized"
    lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
    assert successor_step(h, lease)["status"] == "completed"
    assert len(h.calls) == 1


def test_unknown_usage_blocks_resume_even_when_provider_checkpoint_is_completed():
    h = setup_continuity()
    run = start(h)
    h.hook = lambda _: (_ for _ in ()).throw(TimeoutError("Unknown provider result"))
    advance(h, claim(h), draft())
    step = h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))[0]
    h.execution._save(step, status="completed", result=draft(), error_code=None)
    handoff, _ = capture(h)
    before = h.store.model_usage_entries(h.workspace, h.first.id)
    with pytest.raises(InvalidTransitionError, match="unknown provider effects"):
        resume(h, handoff, run)
    assert h.store.model_usage_entries(h.workspace, h.first.id) == before


def test_unrelated_previously_revoked_source_is_omitted_without_historical_run_context():
    h = setup_continuity()
    historical = source(h)
    h.store.insert_native_intake_source(historical)
    h.store.revoke_native_intake_source(h.workspace, h.first.id, historical.id, utc_now())
    handoff, _ = capture(h)
    assert not handoff["handoff"]["manifest"]["source_revision_ids"]
    assert handoff["handoff"]["manifest"]["snapshot"]["omitted_sources"][0][
        "source_revision_id"
    ] == str(historical.id)


def test_archive_limit_counts_zip_headers_and_status_bytes(monkeypatch):
    h = setup_continuity()
    handoff, _ = capture(h)
    identifier = UUID(handoff["handoff"]["id"])
    _, raw = h.continuity.download(h.recipient, h.first.id, identifier)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        file_bytes = sum(item.file_size for item in archive.infolist())
    assert len(raw) > file_bytes
    # A withheld export flag adds one JSON byte; retain a small margin that
    # still falls below ZIP framing so the final archive bound is exercised.
    monkeypatch.setattr(
        "simon.services.native_continuity.MAX_CONTINUITY_EXPORT_BYTES", file_bytes + 10
    )
    with pytest.raises(ValidationError, match="archive exceeds"):
        h.continuity.download(h.recipient, h.first.id, identifier)


def test_edited_current_status_is_explicitly_untrusted_while_originals_verify():
    h = setup_continuity()
    handoff, _ = capture(h)
    identifier = UUID(handoff["handoff"]["id"])
    _, raw = h.continuity.download(h.recipient, h.first.id, identifier)
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(stream, "w") as changed:
        for name in original.namelist():
            changed.writestr(
                name, b'{"current":true}' if name == "current-status.json" else original.read(name)
            )
    result = h.continuity.verify(
        h.recipient,
        h.first.id,
        identifier,
        VerifyContinuityBundle(content_base64=base64.b64encode(stream.getvalue()).decode()),
    )
    assert result["valid"] and not result["current_status_trusted"]
    assert result["verification_scope"] == "immutable_manifest_and_originals"


def test_failed_core_grant_check_rolls_back_staged_binding_and_receipt(monkeypatch):
    h = setup_continuity()
    run = completed_unapplied(h, monkeypatch)
    handoff, _ = capture(h)
    policy = h.execution._policy(h.workspace, h.first.id)
    h.execution.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateExecutionPolicy(expected_version=policy.version, enabled=False, **command_key()),
    )
    command = resume_command(h, handoff, run)
    before = h.store.execution_events(h.workspace, h.first.id, UUID(run["id"]), 0, 1000)
    with pytest.raises(InvalidTransitionError, match="grant changed"):
        resume(h, handoff, run, command)
    assert h.store.execution_events(h.workspace, h.first.id, UUID(run["id"]), 0, 1000) == before
    assert (
        h.store.command_receipt(
            h.continuity._namespace(h.recipient, h.first.id), command.idempotency_key
        )
        is None
    )


def test_unexportable_capture_rolls_back_record_charge_audit_and_receipt(monkeypatch):
    h = setup_continuity()
    command = CreateContinuityHandoff(recipient_id=h.recipient.actor_id, **command_key())
    usage = h.store.storage_usage(h.workspace)
    audit = h.store.audit_events(h.workspace)
    monkeypatch.setattr("simon.services.native_continuity.MAX_CONTINUITY_EXPORT_BYTES", 500)
    with pytest.raises(ValidationError, match="export bound"):
        h.continuity.create(h.owner_actor, h.first.id, command)
    assert not h.store.continuity_handoffs(h.workspace, h.first.id)
    assert h.store.storage_usage(h.workspace) == usage
    assert h.store.audit_events(h.workspace) == audit
    assert (
        h.store.command_receipt(
            h.continuity._namespace(h.owner_actor, h.first.id), command.idempotency_key
        )
        is None
    )


@pytest.mark.parametrize("unknown", [False, True])
def test_context_change_during_provider_call_preserves_cost_and_fences_publication(unknown):
    h = setup_continuity(paid=True)
    h.dependencies = NativeDependencyService(h.store, h.artifacts)
    original_fact = fact(h, 40)
    run = start(h)
    handoff, _ = capture(h)
    resume(h, handoff, run)
    lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
    h.response = json.dumps(draft("Durable pre-change checkpoint candidate"))

    def change_during_call(_):
        fact(h, 50, previous=original_fact)
        if unknown:
            raise TimeoutError("Provider acknowledgement lost after context changed")

    h.hook = change_during_call
    result = successor_step(h, lease)
    assert result["status"] == "running" and result["applied_step"] == 0
    assert not h.store.artifacts(h.workspace, h.first.id)
    step = h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))[0]
    usage = h.store.model_usage(h.workspace, h.first.id, step.usage_id)
    assert len(h.calls) == 1
    assert step.status == ("unknown" if unknown else "completed")
    assert usage.status == ("unknown" if unknown else "settled")
    assert usage.held_microusd > 0 if unknown else usage.charged_microusd > 0
    h.execution.view(h.recipient, h.first.id)
    assert current(h, run).status == "running"
    h.execution._save(current(h, run), lease_until=utc_now() - timedelta(seconds=1))
    replacement, _ = capture(h)
    if unknown:
        with pytest.raises(InvalidTransitionError, match="unknown provider effects"):
            resume(h, replacement, run)
        assert h.store.model_usage(h.workspace, h.first.id, step.usage_id) == usage
    else:
        resume(h, replacement, run)
        next_lease = h.execution.claim(h.successor, NativeCommand(**command_key()))
        assert successor_step(h, next_lease)["status"] == "completed"
        assert h.store.model_usage(h.workspace, h.first.id, step.usage_id) == usage
    assert len(h.calls) == 1


def test_entitlement_notes_are_redacted_for_member_in_detail_and_export():
    h = setup_continuity()
    entitlement = StorageEntitlement(
        workspace_id=h.workspace,
        version=1,
        plan_key="review",
        limit_bytes=1_000_000,
        reason="Private administrator entitlement rationale",
        issued_by=h.owner,
    )
    h.store.save_storage_entitlement(entitlement, 0)
    handoff, _ = capture(h)
    identifier = UUID(handoff["handoff"]["id"])
    owner = h.continuity.detail(h.owner_actor, h.first.id, identifier)
    member = h.continuity.detail(h.recipient, h.first.id, identifier)
    assert owner["current_resources"]["storage_entitlement"]["reason"] == entitlement.reason
    assert owner["current_resources"]["storage_entitlement"]["issued_by"] == str(h.owner)
    assert member["current_resources"]["storage_entitlement"]["reason"] == ""
    assert member["current_resources"]["storage_entitlement"]["issued_by"] is None
    assert (
        member["current_resources"]["storage_entitlement"]["limit_bytes"] == entitlement.limit_bytes
    )
    _, raw = h.continuity.download(h.recipient, h.first.id, identifier)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        report = json.loads(archive.read("current-status.json"))
    assert report["current_resources"]["storage_entitlement"]["reason"] == ""
    assert report["current_resources"]["storage_entitlement"]["issued_by"] is None
    assert entitlement.reason.encode() not in raw
