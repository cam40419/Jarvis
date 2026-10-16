"""Selective authority preserves unrelated approvals without laundering revoked evidence."""

import hashlib
import json
from uuid import UUID

import pytest

from simon.domain.errors import AuthorizationError, InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_artifact_commands import DecideNativeArtifact, UpdateArtifactReviewPolicy
from simon.domain.native_dependencies import (
    DependencyBinding,
    DependencySelector,
    FactSourceBinding,
)
from simon.domain.native_dependency_commands import DeclareNativeDependencies, UpsertNativeFact
from simon.services.native_dependencies import NativeDependencyService
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_artifacts import (
    current_revision,
    decide,
    detail,
    key,
    review,
    target_command,
    upload,
)
from tests.unit.test_native_delivery import capture, setup_delivery, unpack
from tests.unit.test_native_execution import new_task


def pointer(path):
    return DependencySelector(kind="json_pointer", path=path)


def setup_dependencies():
    h = setup_delivery()
    h.dependencies = NativeDependencyService(h.store, h.artifacts)
    return h


def require_independent_review(h):
    h.policy = h.artifacts.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateArtifactReviewPolicy(
            expected_version=h.policy["version"],
            require_agent_review=True,
            reviewer_agent_id=h.reviewer.id,
            **key(),
        ),
    ).model_dump(mode="json")


def evidence(h, value, *, source_key="spec.json", revision=1):
    text = json.dumps(value, sort_keys=True)
    item = source(
        h,
        text=text,
        source_key=source_key,
        revision=revision,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        size_bytes=len(text.encode()),
        filename=source_key,
        media_type="application/json",
    )
    h.store.insert_native_intake_source(item)
    return item


def fact(h, value, *, previous=None, sources=(), name="unit-price", value_type="number"):
    saved = h.dependencies.save_fact(
        h.owner_actor,
        h.first.id,
        UpsertNativeFact(
            fact_id=previous["fact"]["id"] if previous else None,
            key=name,
            expected_version=previous["fact"]["version"] if previous else 0,
            value_type=value_type,
            value=value,
            sources=tuple(sources),
            **key(),
        ),
    )
    return saved


def fact_edge(record, *, output="/price", input_selector=None):
    revision = record["revisions"][-1]
    return DependencyBinding(
        input_kind="fact",
        input_id=record["fact"]["id"],
        revision_id=revision["id"],
        sha256=revision["sha256"],
        input_selector=input_selector or DependencySelector(),
        output_selector=pointer(output),
    )


def output_edge(h, artifact, *, input_path="/price", output_path="/price"):
    revision = current_revision(detail(h, artifact))
    return DependencyBinding(
        input_kind="artifact",
        input_id=artifact["id"],
        revision_id=revision["id"],
        sha256=revision["sha256"],
        input_selector=pointer(input_path),
        output_selector=pointer(output_path),
    )


def declare(h, artifact, *edges, coverage="complete"):
    revision = current_revision(detail(h, artifact))
    old = h.store.dependency_declaration(h.workspace, h.first.id, UUID(revision["id"]))
    h.dependencies.declare(
        h.owner_actor,
        h.first.id,
        UUID(artifact["id"]),
        DeclareNativeDependencies(
            expected_version=old.version if old else 0,
            expected_artifact_version=artifact["version"],
            revision_id=revision["id"],
            sha256=revision["sha256"],
            coverage=coverage,
            dependencies=tuple(edges),
            **key(),
        ),
    )


def json_output(h, value):
    return upload(
        h,
        json.dumps(value).encode(),
        task=new_task(h),
        filename="output.json",
        media_type="application/json",
    )[0]


def approvals(h, artifact):
    revision = current_revision(detail(h, artifact))
    return h.store.artifact_decisions(h.workspace, h.first.id, UUID(revision["id"]))


def test_material_fact_change_preserves_unrelated_exact_approval_and_packet_bytes():
    h = setup_dependencies()
    price = fact(h, 40)
    affected = json_output(h, {"price": 40})
    unrelated = json_output(h, {"wordmark": "Independent"})
    declare(h, affected, fact_edge(price))
    declare(h, unrelated)
    decide(h, affected)
    decide(h, unrelated)
    retained = approvals(h, unrelated)
    retained_artifact = detail(h, unrelated)["artifact"]
    packet, _ = capture(h, unrelated)
    _, original_zip, original_files = unpack(h, packet)

    fact(h, 50, previous=price)
    assert detail(h, affected)["status"] == "stale"
    assert detail(h, unrelated)["status"] == "accepted"
    assert approvals(h, unrelated) == retained
    assert detail(h, unrelated)["artifact"] == retained_artifact
    current = h.delivery.detail(h.owner_actor, h.first.id, UUID(packet["packet"]["id"]))
    assert current["applicability"]["current"]
    _, current_zip, current_files = unpack(h, packet)
    assert current_zip == original_zip and current_files == original_files
    assert not h.calls


def test_same_value_source_and_fact_rebase_preserves_original_approval_identity():
    h = setup_dependencies()
    original = evidence(h, {"price": 40, "note": "First wording"})
    binding = FactSourceBinding(
        source_revision_id=original.id, sha256=original.sha256, selector=pointer("/price")
    )
    price = fact(h, 40, sources=(binding,))
    artifact = json_output(h, {"price": 40})
    declare(h, artifact, fact_edge(price))
    decide(h, artifact)
    retained = approvals(h, artifact)
    packet, _ = capture(h, artifact)
    _, original_zip, _ = unpack(h, packet)

    changed = evidence(h, {"price": 40.0, "note": "Irrelevant paraphrase"}, revision=2)
    assert detail(h, artifact)["status"] == "accepted"
    fact(
        h,
        40,
        previous=price,
        sources=(
            FactSourceBinding(
                source_revision_id=changed.id, sha256=changed.sha256, selector=pointer("/price")
            ),
        ),
    )
    assert detail(h, artifact)["status"] == "accepted"
    assert approvals(h, artifact) == retained
    current = h.delivery.detail(h.owner_actor, h.first.id, UUID(packet["packet"]["id"]))
    assert current["applicability"]["current"]
    assert unpack(h, packet)[1] == original_zip


def test_unknown_and_partial_coverage_remain_conservative_without_blocking_initial_acceptance():
    h = setup_dependencies()
    price = fact(h, 40)
    unknown = json_output(h, {"note": "Unknown dependency coverage"})
    partial = json_output(h, {"price": 40})
    unrelated = json_output(h, {"wordmark": "Independent"})
    declare(h, partial, fact_edge(price), coverage="partial")
    declare(h, unrelated)
    for artifact in (unknown, partial, unrelated):
        decide(h, artifact)
        assert detail(h, artifact)["status"] == "accepted"
    packet, _ = capture(h, unknown, partial, unrelated)
    retained = approvals(h, unrelated)

    fact(h, 50, previous=price)
    assert detail(h, unknown)["status"] == "stale"
    assert detail(h, partial)["status"] == "stale"
    assert detail(h, unrelated)["status"] == "accepted"
    assert approvals(h, unrelated) == retained
    current = h.delivery.detail(h.owner_actor, h.first.id, UUID(packet["packet"]["id"]))
    assert not current["applicability"]["current"]
    assert not current["applicability"]["components_accepted"]


def test_material_fact_change_reaches_transitive_cycle_and_preserves_unrelated_acceptance():
    h = setup_dependencies()
    price = fact(h, 40)
    first, second, third, unrelated = [json_output(h, {"price": 40}) for _ in range(4)]
    declare(h, first, fact_edge(price), output_edge(h, second))
    declare(h, second, output_edge(h, first))
    declare(h, third, output_edge(h, second))
    declare(h, unrelated)
    for artifact in (first, second, third, unrelated):
        decide(h, artifact)
    retained = approvals(h, unrelated)

    fact(h, 50, previous=price)
    assert all(detail(h, artifact)["status"] == "stale" for artifact in (first, second, third))
    assert detail(h, unrelated)["status"] == "accepted"
    assert approvals(h, unrelated) == retained


def test_redeclaration_never_reuses_an_existing_accepted_decision():
    h = setup_dependencies()
    price = fact(h, 40)
    artifact = json_output(h, {"price": 40})
    declare(h, artifact, fact_edge(price))
    decide(h, artifact)
    retained = approvals(h, artifact)
    declare(h, artifact, fact_edge(price))
    assert detail(h, artifact)["status"] == "stale"
    decide(h, artifact)
    assert approvals(h, artifact)[0] == retained[0]
    assert len(approvals(h, artifact)) == 2


def test_revoked_historical_declaration_cannot_be_laundered_before_first_review():
    h = setup_dependencies()
    original = evidence(h, {"price": 40})
    price = fact(
        h,
        40,
        sources=(FactSourceBinding(source_revision_id=original.id, sha256=original.sha256),),
    )
    artifact = json_output(h, {"price": 40})
    declare(h, artifact, fact_edge(price))
    declare(h, artifact)
    h.store.revoke_native_intake_source(h.workspace, h.first.id, original.id, utc_now())
    revision = current_revision(detail(h, artifact))
    with pytest.raises(AuthorizationError):
        h.artifacts.read_content(
            h.owner_actor, h.first.id, UUID(artifact["id"]), UUID(revision["id"])
        )
    assert not detail(h, artifact)["disclosure_allowed"]
    with pytest.raises((AuthorizationError, InvalidTransitionError)):
        decide(h, artifact)


def test_revoked_typed_values_are_removed_from_public_decision_metadata_and_packets():
    h = setup_dependencies()
    private = "Private measurement 41"
    original = evidence(h, {"measurement": private})
    measurement = fact(
        h,
        private,
        value_type="string",
        sources=(
            FactSourceBinding(
                source_revision_id=original.id,
                sha256=original.sha256,
                selector=pointer("/measurement"),
            ),
        ),
    )
    artifact = json_output(h, {"measurement": private})
    declare(h, artifact, fact_edge(measurement, output="/measurement"))
    h.artifacts.decide(
        h.owner_actor,
        h.first.id,
        UUID(artifact["id"]),
        target_command(h, artifact, DecideNativeArtifact, decision="approved", feedback=private),
    )
    retained = approvals(h, artifact)
    packet, _ = capture(h, artifact)

    h.store.revoke_native_intake_source(h.workspace, h.first.id, original.id, utc_now())
    value = detail(h, artifact)
    assert not value["disclosure_allowed"]
    assert private not in json.dumps(value)
    assert "dependency_snapshot" not in value["decisions"][0]["authority_snapshot"]
    assert approvals(h, artifact) == retained
    with pytest.raises(AuthorizationError):
        unpack(h, packet)


def test_complete_review_prompt_contains_only_declared_typed_fact_values():
    h = setup_dependencies()
    require_independent_review(h)
    evidence(h, {"unrelated": "Unrelated private quotation"}, source_key="other.json")
    price = fact(h, 40)
    artifact = json_output(h, {"price": 40})
    declare(h, artifact, fact_edge(price))
    reviewed = review(h, artifact)
    saved = h.store.artifact_review(h.workspace, h.first.id, UUID(reviewed["id"]))
    assert saved.context["sources"] == []
    assert saved.context["review_facts"][0]["value"] == 40
    assert "Unrelated private quotation" not in h.calls[-1].content.decode()
    decide(h, artifact)
    assert detail(h, artifact)["status"] == "accepted"


def test_json_fact_selectors_scope_review_values_and_preserve_unselected_field_edits():
    h = setup_dependencies()
    require_independent_review(h)
    selected = {"price": 40, "lead_days": 5, "private_note": "Unselected private field"}
    configuration = fact(h, selected, value_type="json")
    artifact = json_output(h, {"price": 40, "lead_days": 5})
    declare(
        h,
        artifact,
        fact_edge(configuration, input_selector=pointer("/price")),
        fact_edge(configuration, input_selector=pointer("/lead_days"), output="/lead_days"),
    )
    reviewed = review(h, artifact)
    saved = h.store.artifact_review(h.workspace, h.first.id, UUID(reviewed["id"]))
    assert {item["selector"]["path"]: item["value"] for item in saved.context["review_facts"]} == {
        "/price": 40,
        "/lead_days": 5,
    }
    assert "Unselected private field" not in h.calls[-1].content.decode()
    decide(h, artifact)
    retained = approvals(h, artifact)

    fact(
        h,
        {**selected, "private_note": "Only this unselected field changed"},
        previous=configuration,
        value_type="json",
    )
    assert detail(h, artifact)["status"] == "accepted"
    assert approvals(h, artifact) == retained
    assert len(h.calls) == 1
