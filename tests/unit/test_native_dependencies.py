"""Registered selective-repair fixtures run against real artifact review and storage."""

import base64
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from simon.domain.errors import AuthorizationError, InvalidTransitionError, ValidationError
from simon.domain.models import utc_now
from simon.domain.native_dependencies import DependencySelector
from simon.domain.native_dependency_commands import (
    CommitNativeRepair,
    CreateImpactAssessment,
    DeclareNativeDependencies,
    PreviewNativeRepair,
    UpsertNativeFact,
)
from simon.services.native_dependencies import NativeDependencyService
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_artifacts import (
    current_revision,
    decide,
    detail,
    key,
    review,
    setup_artifacts,
    upload,
)
from tests.unit.test_native_execution import new_task

FIXTURES = json.loads(
    (Path(__file__).parents[2] / "examples/core-baseline/fixtures.json").read_text()
)


def setup():
    h = setup_artifacts()
    h.dependencies = NativeDependencyService(h.store, h.artifacts)
    return h


def save_fact(h, value, previous=None, **changes):
    values = {
        "key": "price",
        "kind": "fact",
        "value_type": "number",
        "value": value,
        "unit": "USD",
        "expected_version": 0,
        **key(),
    }
    if previous:
        values.update(
            fact_id=previous["fact"]["id"],
            expected_version=previous["fact"]["version"],
            key=previous["fact"]["key"],
            kind=previous["fact"]["kind"],
        )
    values.update(changes)
    return h.dependencies.save_fact(h.owner_actor, h.first.id, UpsertNativeFact(**values))


def fact_head(value):
    return next(
        revision
        for revision in value["revisions"]
        if revision["id"] == value["fact"]["current_revision_id"]
    )


def pointer(path):
    return {"kind": "json_pointer", "path": path}


def fact_edge(value, **changes):
    revision = fact_head(value)
    return {
        "input_kind": "fact",
        "input_id": value["fact"]["id"],
        "revision_id": revision["id"],
        "sha256": revision["sha256"],
        **changes,
    }


def artifact_edge(h, artifact, **changes):
    revision = current_revision(detail(h, artifact))
    return {
        "input_kind": "artifact",
        "input_id": artifact["id"],
        "revision_id": revision["id"],
        "sha256": revision["sha256"],
        **changes,
    }


def declare(h, artifact, dependencies=(), coverage="complete"):
    record = h.store.artifact(h.workspace, h.first.id, UUID(artifact["id"]))
    revision = h.store.artifact_revision(h.workspace, h.first.id, record.current_revision_id)
    old = h.store.dependency_declaration(h.workspace, h.first.id, revision.id)
    return h.dependencies.declare(
        h.owner_actor,
        h.first.id,
        record.id,
        DeclareNativeDependencies(
            expected_version=old.version if old else 0,
            expected_artifact_version=record.version,
            revision_id=revision.id,
            sha256=revision.sha256,
            coverage=coverage,
            dependencies=dependencies,
            **key(),
        ),
    )


def candidate(h, content, filename="candidate.txt"):
    return upload(
        h,
        content.encode(),
        task=new_task(h),
        filename=filename,
        media_type="application/json" if filename.endswith(".json") else "text/plain",
    )[0]


def assess(h, before, after):
    return h.dependencies.assess(
        h.owner_actor,
        h.first.id,
        CreateImpactAssessment(
            fact_id=before["fact"]["id"],
            from_revision_id=fact_head(before)["id"],
            to_revision_id=fact_head(after)["id"],
            **key(),
        ),
    )["assessment"]


def edit(h, artifact, before, after, selector=None):
    record = h.store.artifact(h.workspace, h.first.id, UUID(artifact["id"]))
    revision = h.store.artifact_revision(h.workspace, h.first.id, record.current_revision_id)
    return {
        "artifact_id": str(record.id),
        "expected_version": record.version,
        "revision_id": str(revision.id),
        "sha256": revision.sha256,
        "replacements": [
            {"selector": selector or {"kind": "whole_file"}, "before": before, "after": after}
        ],
    }


def preview(h, assessment, edits):
    return h.dependencies.preview_repair(
        h.owner_actor,
        h.first.id,
        PreviewNativeRepair(
            assessment_id=assessment["id"],
            edits=edits,
            rationale="Update only the declared changed input regions.",
            **key(),
        ),
    )


def commit(h, proposal, command=None):
    value = proposal["proposal"]
    return h.dependencies.commit_repair(
        h.owner_actor,
        h.first.id,
        UUID(value["id"]),
        command or CommitNativeRepair(proposal_sha256=value["sha256"], **key()),
    )


def bytes_for(h, artifact):
    record = h.store.artifact(h.workspace, h.first.id, UUID(artifact["id"]))
    revision = h.store.artifact_revision(h.workspace, h.first.id, record.current_revision_id)
    return base64.b64decode(revision.content_base64)


@pytest.mark.parametrize("fixture", FIXTURES["fixtures"], ids=lambda value: value["id"])
def test_registered_selective_repair_fixture_preserves_unrelated_bytes_and_approval(fixture):
    h = setup()
    data = fixture["fact"]
    before = save_fact(h, data["before"], key=data["id"], unit=data["unit"])
    direct_data, transitive_data = fixture["direct_output"], fixture["transitive_output"]
    direct = candidate(h, json.dumps(direct_data["before"]), direct_data["path"])
    transitive = candidate(h, transitive_data["before"], transitive_data["path"])
    unrelated = candidate(
        h,
        fixture["unrelated_accepted_output"]["content"],
        fixture["unrelated_accepted_output"]["path"],
    )
    uncertain = candidate(h, fixture["partial_coverage_output"]["content"], "notes.md")
    region = pointer(direct_data["selector"])
    declare(
        h,
        direct,
        [
            fact_edge(before, output_selector=region),
            artifact_edge(h, transitive, output_selector=region),
        ],
    )
    declare(h, transitive, [artifact_edge(h, direct, input_selector=region)])
    declare(h, unrelated)
    declare(h, uncertain, coverage="partial")
    for artifact in (direct, transitive, unrelated):
        review(h, artifact)
        decide(h, artifact)
    unrelated_before = detail(h, unrelated)
    direct_old = current_revision(detail(h, direct))
    same = save_fact(h, float(data["before"]), before, unit=data["unit"])
    assert detail(h, direct)["status"] == "accepted"
    assert all(output["status"] != "affected" for output in assess(h, before, same)["outputs"])
    after = save_fact(h, data["after"], same, unit=data["unit"])
    impact = assess(h, same, after)
    statuses = {output["artifact_id"]: output["status"] for output in impact["outputs"]}
    assert statuses == {
        direct["id"]: "affected",
        transitive["id"]: "affected",
        unrelated["id"]: "unaffected",
        uncertain["id"]: "uncertain",
    }
    assert detail(h, direct)["status"] == "stale"
    assert detail(h, transitive)["status"] == "stale"
    proposal = preview(
        h,
        impact,
        [
            edit(h, direct, data["before"], data["after"], region),
            edit(h, transitive, transitive_data["before"], transitive_data["after"]),
        ],
    )
    assert bytes_for(h, direct) == json.dumps(direct_data["before"]).encode()
    assert proposal["proposal"]["id"] in {
        item["id"] for item in h.dependencies.view(h.owner_actor, h.first.id)["repairs"]
    }
    result = commit(h, proposal)
    assert len(result["committed_revision_ids"]) == 2
    assert json.loads(bytes_for(h, direct)) == direct_data["after"]
    assert bytes_for(h, transitive) == transitive_data["after"].encode()
    assert bytes_for(h, unrelated) == fixture["unrelated_accepted_output"]["content"].encode()
    assert detail(h, unrelated)["decisions"] == unrelated_before["decisions"]
    assert detail(h, unrelated)["status"] == "accepted"
    assert current_revision(detail(h, unrelated)) == current_revision(unrelated_before)
    assert (
        h.store.artifact_revision(h.workspace, h.first.id, UUID(direct_old["id"])).sha256
        == direct_old["sha256"]
    )
    for artifact in (direct, transitive):
        assert detail(h, artifact)["status"] != "accepted"
        scope_check = next(
            item
            for item in detail(h, artifact)["checks"]
            if item["check_name"] == "declared_repair_scope"
        )
        assert scope_check["details"]["proposal_id"] == proposal["proposal"]["id"]
        review(h, artifact)
        decide(h, artifact)
        assert detail(h, artifact)["status"] == "accepted"
    # Durable proposal identity prevents duplicate publications even with a new caller key.
    h.dependencies = NativeDependencyService(h.store, h.artifacts)
    assert commit(h, proposal)["committed_revision_ids"] == result["committed_revision_ids"]


def simple_repair(fixture=None):
    h = setup()
    fixture = fixture or FIXTURES["fixtures"][0]
    data, direct, transitive = (
        fixture["fact"],
        fixture["direct_output"],
        fixture["transitive_output"],
    )
    before = save_fact(h, data["before"], key=data["id"], unit=data["unit"])
    first = candidate(h, json.dumps(direct["before"]), direct["path"])
    second = candidate(h, transitive["before"], transitive["path"])
    region = pointer(direct["selector"])
    declare(h, first, [fact_edge(before, output_selector=region)])
    declare(h, second, [artifact_edge(h, first, input_selector=region)])
    after = save_fact(h, data["after"], before, unit=data["unit"])
    impact = assess(h, before, after)
    plan = preview(
        h,
        impact,
        [
            edit(h, first, data["before"], data["after"], region),
            edit(h, second, transitive["before"], transitive["after"]),
        ],
    )
    return h, before, after, first, second, plan


@pytest.mark.parametrize("fixture", FIXTURES["fixtures"], ids=lambda value: value["id"])
def test_concurrent_human_edit_rejects_entire_batch_without_lost_work(fixture):
    h, _, _, first, second, plan = simple_repair(fixture)
    old = bytes_for(h, first)
    current = detail(h, second)["artifact"]
    upload(
        h,
        b"Human revision",
        artifact=current,
        task=h.store.native_task(h.workspace, h.first.id, UUID(current["task_id"])),
    )
    with pytest.raises(InvalidTransitionError):
        commit(h, plan)
    assert bytes_for(h, first) == old
    assert bytes_for(h, second) == b"Human revision"
    assert not h.dependencies.repair_detail(
        h.owner_actor, h.first.id, UUID(plan["proposal"]["id"])
    )["can_commit"]


def test_publication_failure_rolls_back_all_heads_tasks_receipts_and_declarations(monkeypatch):
    h, _, _, first, second, plan = simple_repair()
    originals = [bytes_for(h, item) for item in (first, second)]
    operation = h.artifacts._publish
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise InvalidTransitionError("Injected second-output persistence failure")
        return operation(*args, **kwargs)

    monkeypatch.setattr(h.artifacts, "_publish", fail_second)
    command = CommitNativeRepair(proposal_sha256=plan["proposal"]["sha256"], **key())
    with pytest.raises(InvalidTransitionError):
        commit(h, plan, command)
    assert [bytes_for(h, item) for item in (first, second)] == originals
    assert h.dependencies.repair_detail(h.owner_actor, h.first.id, UUID(plan["proposal"]["id"]))[
        "can_commit"
    ]
    monkeypatch.setattr(h.artifacts, "_publish", operation)
    assert len(commit(h, plan, command)["committed_revision_ids"]) == 2


@pytest.mark.parametrize("fixture", FIXTURES["fixtures"], ids=lambda value: value["id"])
def test_selected_source_revision_same_value_is_current_but_revoke_withholds_all_derived_views(
    fixture,
):
    h = setup()
    data, transitive = fixture["fact"], fixture["transitive_output"]

    def evidence(number, revision, color):
        text = json.dumps({"price": number, "color": color})
        value = source(
            h, revision=revision, text=text, sha256=hashlib.sha256(text.encode()).hexdigest()
        )
        h.store.insert_native_intake_source(value)
        return value

    first_source = evidence(data["before"], 1, "navy")
    binding = {
        "source_revision_id": str(first_source.id),
        "sha256": first_source.sha256,
        "selector": pointer("/price"),
    }
    before = save_fact(h, data["before"], key=data["id"], unit=data["unit"], sources=[binding])
    artifact = candidate(h, transitive["before"], transitive["path"])
    declare(h, artifact, [fact_edge(before)])
    review(h, artifact)
    original, _ = decide(h, artifact)
    evidence(float(data["before"]), 2, "green")
    assert detail(h, artifact)["status"] == "accepted"
    assert detail(h, artifact)["decisions"][0]["id"] == original["id"]
    changed_source = evidence(data["after"], 3, "green")
    assert detail(h, artifact)["status"] == "stale"
    after = save_fact(
        h,
        data["after"],
        before,
        unit=data["unit"],
        sources=[
            {
                **binding,
                "source_revision_id": str(changed_source.id),
                "sha256": changed_source.sha256,
            }
        ],
    )
    impact = assess(h, before, after)
    plan = preview(h, impact, [edit(h, artifact, transitive["before"], transitive["after"])])
    h.store.revoke_native_intake_source(h.workspace, h.first.id, first_source.id, utc_now())
    with pytest.raises(AuthorizationError):
        commit(h, plan)
    assert (
        h.dependencies.repair_detail(h.owner_actor, h.first.id, UUID(plan["proposal"]["id"]))[
            "disclosure"
        ]
        == "withheld"
    )
    assert (
        h.dependencies.assessment_detail(h.owner_actor, h.first.id, UUID(impact["id"]))[
            "disclosure"
        ]
        == "withheld"
    )
    assert h.dependencies.view(h.owner_actor, h.first.id)["outputs"][0]["disclosure"] == "withheld"


def test_conflicting_source_selection_rejected_and_unrelated_fields_cannot_be_patched():
    h, before, after, first, _, _ = simple_repair()
    evidence = source(h, text='{"price":99}')
    h.store.insert_native_intake_source(evidence)
    with pytest.raises(ValidationError, match="conflicts"):
        save_fact(
            h,
            25,
            after,
            sources=[
                {
                    "source_revision_id": evidence.id,
                    "sha256": evidence.sha256,
                    "selector": pointer("/price"),
                }
            ],
        )
    impact = assess(h, before, after)
    with pytest.raises(ValidationError, match="affected region"):
        preview(h, impact, [edit(h, first, "navy", "green", pointer("/color"))])


@pytest.mark.parametrize("path", ["/a~2b", "missing-prefix"])
def test_unsupported_pointer_syntax_is_rejected(path):
    with pytest.raises(ValueError):
        DependencySelector(kind="json_pointer", path=path)


@pytest.mark.parametrize("fixture", FIXTURES["fixtures"], ids=lambda value: value["id"])
def test_unknown_coverage_cannot_be_claimed_independent_or_selectively_repaired(fixture):
    h = setup()
    data, transitive = fixture["fact"], fixture["transitive_output"]
    before = save_fact(h, data["before"], key=data["id"], unit=data["unit"])
    artifact = candidate(h, transitive["before"], transitive["path"])
    after = save_fact(h, data["after"], before, unit=data["unit"])
    impact = assess(h, before, after)
    assert impact["outputs"][0]["status"] == "uncertain"
    with pytest.raises(ValidationError, match="unknown coverage"):
        preview(h, impact, [edit(h, artifact, transitive["before"], transitive["after"])])


def test_quota_denial_on_second_output_rolls_back_entire_repair_and_can_retry():
    from simon.domain.errors import StorageQuotaExceededError
    from tests.unit.test_storage_workflows import limit

    h, _, _, first, second, plan = simple_repair()
    usage = h.store.storage_usage(h.workspace)["retained_bytes"]
    first_bytes = len(plan["proposal"]["plan"]["outputs"][0]["after_text"].encode())
    originals = [bytes_for(h, item) for item in (first, second)]
    limit(h, usage + first_bytes)
    with pytest.raises(StorageQuotaExceededError):
        commit(h, plan)
    assert [bytes_for(h, item) for item in (first, second)] == originals
    assert h.store.storage_usage(h.workspace)["retained_bytes"] == usage
    assert h.dependencies.repair_detail(h.owner_actor, h.first.id, UUID(plan["proposal"]["id"]))[
        "can_commit"
    ]
    limit(h, usage + 1000)
    assert len(commit(h, plan)["committed_revision_ids"]) == 2


def test_changed_prior_maker_task_blocks_repair_of_current_revision():
    from tests.unit.test_native_teams import task_update

    h = setup()
    before = save_fact(h, 20)
    artifact = candidate(h, "Price: 20")
    decision, _ = decide(h, artifact, approved=False)
    maker = h.store.native_task(h.workspace, h.first.id, UUID(decision["repair_task_id"]))
    artifact, _ = upload(
        h, b"Price: 20, confirmed", artifact=detail(h, artifact)["artifact"], task=maker
    )
    declare(h, artifact, [fact_edge(before)])
    after = save_fact(h, 25, before)
    proposal = preview(
        h,
        assess(h, before, after),
        [edit(h, artifact, "Price: 20, confirmed", "Price: 25, confirmed")],
    )
    maker = h.store.native_task(h.workspace, h.first.id, maker.id)
    h.projects.update_task(
        h.owner_actor,
        h.first.id,
        maker.id,
        task_update(maker, description="Changed source interpretation."),
    )
    with pytest.raises(InvalidTransitionError):
        commit(h, proposal)
    assert bytes_for(h, artifact) == b"Price: 20, confirmed"


def test_repair_of_one_fact_does_not_rebase_other_stale_fact_bindings():
    h = setup()
    price = save_fact(h, 20)
    color = save_fact(h, "navy", key="color", value_type="string", unit="")
    artifact = candidate(h, '{"price":20,"color":"navy"}', "spec.json")
    declare(
        h,
        artifact,
        [
            fact_edge(price, output_selector=pointer("/price")),
            fact_edge(color, output_selector=pointer("/color")),
        ],
    )
    changed_price = save_fact(h, 25, price)
    save_fact(h, "green", color, value_type="string", unit="")
    proposal = preview(
        h, assess(h, price, changed_price), [edit(h, artifact, 20, 25, pointer("/price"))]
    )
    commit(h, proposal)
    assert json.loads(bytes_for(h, artifact)) == {"price": 25, "color": "navy"}
    assert detail(h, artifact)["status"] == "stale"
    with pytest.raises(InvalidTransitionError):
        review(h, artifact)


def test_upstream_unknown_coverage_propagates_uncertainty_without_false_independence():
    h = setup()
    before = save_fact(h, 20)
    upstream = candidate(h, "Undeclared relationship")
    downstream = candidate(h, "Uses upstream")
    declare(h, downstream, [artifact_edge(h, upstream)])
    after = save_fact(h, 25, before)
    impact = assess(h, before, after)
    assert {output["status"] for output in impact["outputs"]} == {"uncertain"}


@pytest.mark.parametrize("change", ["unit", "requirement"])
def test_units_and_requirements_are_semantic_authority(change):
    h = setup()
    before = save_fact(h, 20, kind="requirement" if change == "requirement" else "fact")
    artifact = candidate(h, "Constraint 20")
    declare(h, artifact, [fact_edge(before)])
    review(h, artifact)
    decide(h, artifact)
    after = save_fact(
        h, 21 if change == "requirement" else 20, before, unit="EUR" if change == "unit" else "USD"
    )
    assert detail(h, artifact)["status"] == "stale"
    assert assess(h, before, after)["outputs"][0]["status"] == "affected"


@pytest.mark.parametrize("text", ['{"price":20,"price":25}', '{"price":NaN}', '{"price":1e999}'])
def test_ambiguous_and_nonfinite_json_sources_are_rejected(text):
    h = setup()
    evidence = source(h, text=text)
    h.store.insert_native_intake_source(evidence)
    with pytest.raises(ValidationError):
        save_fact(
            h,
            20,
            sources=[
                {
                    "source_revision_id": evidence.id,
                    "sha256": evidence.sha256,
                    "selector": pointer("/price"),
                }
            ],
        )
