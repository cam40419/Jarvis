"""Decision summaries preserve uncertainty, disclosure fences and attention admission."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import httpx
import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_artifact_commands import UpdateArtifactReviewPolicy
from simon.domain.native_models import ReconcileModelUsage
from simon.services.native_delivery import NativeDeliveryService
from simon.services.native_review_inbox import NativeReviewInboxService, json_changes
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_artifacts import (
    decide,
    detail,
    key,
    review,
    setup_artifacts,
    target_command,
    upload,
)
from tests.unit.test_native_delivery import capture
from tests.unit.test_native_execution import new_task


def setup_inbox(**changes):
    h = setup_artifacts(**changes)
    h.delivery = NativeDeliveryService(h.store, h.artifacts)
    h.inbox = NativeReviewInboxService(h.delivery)
    return h


def view(h, **changes):
    return h.inbox.view(h.owner_actor, h.first.id, **changes)


def items(value):
    return [item for group in value["groups"] for item in group["items"]]


def policy(h, **changes):
    h.policy = h.artifacts.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateArtifactReviewPolicy(
            expected_version=h.policy["version"],
            reviewer_agent_id=h.reviewer.id,
            **changes,
            **key(),
        ),
    ).model_dump(mode="json")


def test_current_field_differences_checks_and_uncertainty_survive_queue_projection():
    h = setup_inbox()
    artifact, _ = upload(
        h,
        b'{"stable":{"limit":3},"price":10,"removed":null}',
        filename="spec.json",
        media_type="application/json",
    )
    upload(
        h,
        b'{"stable":{"limit":3},"price":11,"added":true}',
        artifact=artifact,
        filename="spec.json",
        media_type="application/json",
    )
    value = view(h)
    item = items(value)[0]
    assert item["status"] == "awaiting_review"
    assert item["checks"]["failed"] == 0 and item["checks"]["passed"] == 2
    assert item["coverage"]["kind"] == "unknown" and item["coverage"]["reasons"]
    assert [change["path"] for change in item["difference"]["field_changes"]] == [
        "/added",
        "/price",
        "/removed",
    ]
    assert item["difference"]["field_changes"][2]["before_present"]
    assert not item["difference"]["field_changes"][2]["after_present"]
    assert not item["difference"]["fields_truncated"]
    assert item["difference"]["text"].splitlines()[3:] == [
        '-{"stable":{"limit":3},"price":10,"removed":null}',
        "\\ No newline at end of file",
        '+{"stable":{"limit":3},"price":11,"added":true}',
        "\\ No newline at end of file",
    ]
    assert value["summary"]["scope"] == "loaded_page" and not h.calls


def test_field_summary_bounds_and_json_types_are_explicit():
    changes, cut = json_changes({"a/b~c": 1, "same": 1}, {"a/b~c": True, "same": 1.0})
    assert len(changes) == 1 and changes[0]["path"] == "/a~1b~0c" and not cut
    changes, cut = json_changes({}, {str(i): i for i in range(25)})
    assert len(changes) == 20 and cut
    changes, cut = json_changes({"a": "a" * 1000}, {"a": "b" * 1000})
    assert len(changes[0]["before"]) == 500 and cut


@pytest.mark.parametrize("content", [b'{"x":"\\u0000"}', b'{"x":1,"x":2}', b'{"x":NaN}'])
def test_unsupported_json_field_diff_is_visible_without_breaking_the_inbox(content):
    h = setup_inbox()
    artifact, _ = upload(h, b'{"x":1}', media_type="application/json")
    upload(h, content, artifact=artifact, media_type="application/json")
    difference = items(view(h))[0]["difference"]
    assert not difference["field_changes_supported"] and difference["field_changes_limitation"]
    assert difference["text"]


def test_queue_pagination_counts_only_loaded_rows_and_packets_are_not_autoaccepted():
    h = setup_inbox()
    policy(h, require_agent_review=False)
    first, _ = upload(h)
    second, _ = upload(h, task=new_task(h))
    decide(h, first)
    packet, _ = capture(h, first)
    value = view(h, limit=1)
    assert value["pagination"]["artifacts_has_more"]
    assert sum(value["summary"]["counts"].values()) == 2
    packet_item = next(i for i in items(value) if i["kind"] == "packet")
    assert packet_item["id"] == packet["packet"]["id"]
    assert packet_item["status"] == "unprofiled"
    assert packet_item["next_action"]["label"] == "Assign a delivery profile"
    remaining = view(h, artifact_offset=1, packet_offset=1, limit=1)
    assert len(items(remaining)) == 1
    assert {i["id"] for i in items(value) + items(remaining) if i["kind"] == "artifact"} == {
        first["id"],
        second["id"],
    }


def test_revoked_evidence_withholds_content_findings_checks_and_diff():
    h = setup_inbox()
    evidence = source(h)
    h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h, b"private first revision")
    artifact, _ = upload(h, b"private corrected revision", artifact=artifact)
    review(h, artifact, approved=False)
    capture(h, artifact)
    h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
    value = view(h)
    assert len(items(value)) == 2
    assert all(i["disclosure"] == "withheld" for i in items(value))
    assert all(i["checks"]["total"] == 0 and not i["difference"]["text"] for i in items(value))
    assert "private" not in json.dumps(value)
    assert "Production handoff" not in json.dumps(value)
    assert "claimed measurement" not in json.dumps(value)


def test_exhausted_repairs_are_escalated_without_new_tasks_or_implicit_policy_changes():
    h = setup_inbox()
    policy(h, max_repair_cycles=0)
    artifact, _ = upload(h)
    review(h, artifact, approved=False)
    before = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    value = view(h)
    assert value["summary"]["counts"]["blocked"] == 1
    item = items(value)[0]
    assert item["repair"] == {"used": 0, "limit": 0, "escalated": True}
    assert item["next_action"]["kind"] == "policy"
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == before


def test_attention_limit_blocks_before_review_work_or_charge_and_releases_after_decision():
    h = setup_inbox()
    policy(h, max_pending_reviews=1)
    first, _ = upload(h)
    second, _ = upload(h, task=new_task(h))
    review(h, first)
    before = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    with pytest.raises(InvalidTransitionError, match="pending review limit"):
        review(h, second)
    assert len(h.calls) == 1 and not detail(h, second)["reviews"]
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == before
    decide(h, first)
    review(h, second)
    assert len(h.calls) == 2


def test_historical_unknown_liability_blocks_attention_until_real_reconciliation(monkeypatch):
    h = setup_inbox(paid=True)
    policy(h, max_pending_reviews=1)
    first, _ = upload(h)
    second, _ = upload(h, task=new_task(h))

    def interrupt(request):
        raise httpx.ReadTimeout("private provider failure", request=request)

    h.hook = interrupt
    attempt = review(h, first)
    first, _ = upload(h, b"Replacement head", artifact=first)
    h.hook = None
    item = next(i for i in items(view(h)) if i["id"] == first["id"])
    assert item["next_action"]["kind"] == "model_usage"
    with pytest.raises(InvalidTransitionError, match="pending review limit"):
        review(h, second)
    usage = h.store.model_usage_for_operation(h.workspace, h.first.id, UUID(attempt["id"]))[0]
    moment = usage.deadline_at + timedelta(seconds=1)
    monkeypatch.setattr("simon.services.model_usage.utc_now", lambda: moment)
    h.usage.reconcile(
        h.owner_actor,
        h.first.id,
        usage.id,
        ReconcileModelUsage(
            expected_version=usage.version,
            charged_microusd=usage.charged_microusd,
            reason="Operator reconciled provider record.",
            evidence="Provider receipt confirms final usage.",
            **key(),
        ),
    )
    item = next(i for i in items(view(h)) if i["id"] == first["id"])
    assert item["next_action"]["kind"] == "artifact"
    monkeypatch.undo()
    review(h, second)
    assert len(h.calls) == 2


def test_negative_review_also_consumes_attention_until_owner_routes_repair():
    h = setup_inbox()
    policy(h, max_pending_reviews=1)
    first, _ = upload(h)
    second, _ = upload(h, task=new_task(h))
    review(h, first, approved=False)
    with pytest.raises(InvalidTransitionError, match="pending review limit"):
        review(h, second)
    decide(h, first, approved=False)
    review(h, second)
    assert len(h.calls) == 2


def test_concurrent_review_admission_observes_durable_inflight_attention():
    h = setup_inbox()
    policy(h, max_pending_reviews=1)
    first, _ = upload(h)
    second, _ = upload(h, task=new_task(h))
    second_command = target_command(h, second)
    entered, release = Event(), Event()

    def hold(_request):
        entered.set()
        assert release.wait(10)

    h.hook = hold
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(review, h, first)
        try:
            assert entered.wait(10)
            with pytest.raises(InvalidTransitionError, match="pending review limit"):
                h.artifacts.review(h.owner_actor, h.first.id, UUID(second["id"]), second_command)
            assert len(h.calls) == 1 and not detail(h, second)["reviews"]
        finally:
            release.set()
        assert future.result()["verdict"] == "approved"


def test_inbox_package_decision_uses_current_validator_and_exact_owner_acceptance():
    from tests.unit.test_native_acceptance import accept, assign, setup_package, validate

    h = setup_package()
    h.inbox = NativeReviewInboxService(h.delivery)
    assign(h)
    validated, _ = validate(h)
    packet = next(i for i in items(view(h)) if i["kind"] == "packet")
    assert packet["status"] == "profile_assigned"
    assert packet["acceptance"]["can_accept"]
    assert any(c["name"] == "edit_save_reopen_render" for c in packet["checks"]["items"])
    assert "latest_validation" not in packet["acceptance"]
    accepted, _ = accept(h, validated)
    packet = next(i for i in items(view(h)) if i["kind"] == "packet")
    assert packet["status"] == "accepted"
    assert packet["acceptance"]["acceptance_id"] == accepted["latest_acceptance"]["id"]
    policy(h, require_agent_review=False, max_pending_reviews=1)
    packet = next(i for i in items(view(h)) if i["kind"] == "packet")
    assert packet["status"] == "stale"
    assert not packet["acceptance"]["applicability"]["accepted"]


def test_inbox_failed_package_validator_remains_a_repair_with_explicit_check_evidence():
    from tests.unit.test_native_acceptance import assign, setup_package, validate

    h = setup_package(specification='{"unit_price":-1,"color":"navy"}')
    h.inbox = NativeReviewInboxService(h.delivery)
    assign(h)
    validate(h)
    packet = next(i for i in items(view(h)) if i["kind"] == "packet")
    assert packet["status"] == "validation_failed" and packet["checks"]["failed"] > 0
    assert any(
        c["name"] == "local_schema" and c["status"] == "failed" for c in packet["checks"]["items"]
    )
    assert view(h)["summary"]["counts"]["needs_repair"] == 1


@pytest.mark.parametrize("replace_head", [False, True])
def test_same_output_cannot_redispatch_unknown_review_using_a_new_key_or_head(replace_head):
    h = setup_inbox()
    artifact, _ = upload(h)

    def interrupt(request):
        raise httpx.ReadTimeout("Uncertain provider effect", request=request)

    h.hook = interrupt
    command = target_command(h, artifact)
    first = review(h, artifact, command=command)
    assert first["status"] == "unknown"
    assert review(h, artifact, command=command)["id"] == first["id"]
    if replace_head:
        artifact, _ = upload(h, b"Updated candidate", artifact=artifact)
    h.hook = None
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    with pytest.raises(InvalidTransitionError, match="unresolved review effect"):
        review(h, artifact)
    assert len(h.calls) == 1
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks
