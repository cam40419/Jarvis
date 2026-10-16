"""Typed facts, scoped dependencies and repair evidence persist atomically."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_artifacts import NativeArtifact
from simon.domain.native_dependencies import (
    DependencyBinding,
    FactSourceBinding,
    NativeDependencyDeclaration,
    NativeFact,
    NativeFactRevision,
    NativeImpactAssessment,
    NativeRepairProposal,
    semantic_hash,
    snapshot_hash,
)
from tests.contract.test_native_artifact_store import revision_record
from tests.contract.test_native_execution_store import seed_execution
from tests.contract.test_native_intake_store import source


@pytest.fixture
def dependencies(store):
    h = seed_execution(store)
    h.artifact = NativeArtifact(
        workspace_id=h.workspace,
        project_id=h.first.id,
        task_id=h.task.id,
        name="Output",
        created_by_agent_id=h.agent.id,
    )
    store.insert_artifact(h.artifact)
    h.revision = revision_record(h)
    store.insert_artifact_revision(h.revision)
    h.artifact = h.artifact.model_copy(update={"version": 2, "current_revision_id": h.revision.id})
    store.update_artifact(h.artifact, 1)
    h.source = source(h)
    store.insert_native_intake_source(h.source)
    h.fact = NativeFact(
        workspace_id=h.workspace,
        project_id=h.first.id,
        key="price",
        created_by=h.owner,
    )
    store.insert_fact(h.fact)
    h.initial = fact_revision(h)
    store.insert_fact_revision(h.initial)
    h.fact = h.fact.model_copy(update={"version": 2, "current_revision_id": h.initial.id})
    store.update_fact(h.fact, 1)
    return h


def fact_revision(h, **changes):
    values = {
        "workspace_id": h.workspace,
        "project_id": h.first.id,
        "fact_id": h.fact.id,
        "version": 1,
        "value_type": "number",
        "value": 100,
        "unit": "USD",
        "sources": (FactSourceBinding(source_revision_id=h.source.id, sha256=h.source.sha256),),
        "created_by": h.owner,
        **changes,
    }
    values["semantic_sha256"] = semantic_hash(values["value_type"], values["value"], values["unit"])
    values["sha256"] = snapshot_hash(
        {
            "value_type": values["value_type"],
            "value": values["value"],
            "unit": values["unit"],
            "sources": [item.model_dump(mode="json") for item in values["sources"]],
        }
    )
    return NativeFactRevision(**values)


def advance_fact(h):
    h.changed = fact_revision(
        h, version=2, parent_revision_id=h.initial.id, value=120, sha256="b" * 64
    )
    h.store.insert_fact_revision(h.changed)
    h.store.update_fact(
        h.fact.model_copy(update={"version": 3, "current_revision_id": h.changed.id}), 2
    )


def declaration(h, **changes):
    return NativeDependencyDeclaration(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "artifact_id": h.artifact.id,
            "revision_id": h.revision.id,
            "sha256": h.revision.sha256,
            "version": 1,
            "coverage": "complete",
            "dependencies": (
                DependencyBinding(
                    input_kind="fact",
                    input_id=h.fact.id,
                    revision_id=h.initial.id,
                    sha256=h.initial.sha256,
                ),
            ),
            "created_by": h.owner,
            **changes,
        }
    )


def assessment(h, **changes):
    return NativeImpactAssessment(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "fact_id": h.fact.id,
            "from_revision_id": h.initial.id,
            "to_revision_id": h.changed.id,
            "graph_fingerprint": "c" * 64,
            "source_fingerprint": "d" * 64,
            "outputs": (
                {
                    "artifact_id": str(h.artifact.id),
                    "revision_id": str(h.revision.id),
                    "sha256": h.revision.sha256,
                    "status": "stale",
                },
            ),
            "created_by": h.owner,
            **changes,
        }
    )


def test_fact_scope_history_head_cas_and_exact_ancestry(dependencies):
    h = dependencies
    advance_fact(h)
    assert h.store.fact_revisions(h.workspace, h.first.id, h.fact.id) == (h.initial, h.changed)
    assert h.store.fact_revisions(h.workspace, h.first.id, h.fact.id, 1, 1) == (h.changed,)
    for ws, pid in ((h.workspace, h.second.id), (h.foreign_workspace, h.first.id)):
        assert h.store.fact(ws, pid, h.fact.id) is None
        assert h.store.fact_revision(ws, pid, h.initial.id) is None
        assert h.store.facts(ws, pid) == ()
        assert h.store.fact_revisions(ws, pid, h.fact.id) == ()
    with pytest.raises(InvalidTransitionError):
        h.store.update_fact(h.fact.model_copy(update={"version": 3}), 2)
    for record in (
        h.changed,
        fact_revision(h, version=2, parent_revision_id=h.initial.id),
        fact_revision(h, project_id=h.second.id),
        fact_revision(h, fact_id=uuid4()),
        fact_revision(
            h, sources=(FactSourceBinding(source_revision_id=h.source.id, sha256="f" * 64),)
        ),
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_fact_revision(record)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_fact(
            h.fact.model_copy(update={"id": uuid4(), "version": 1, "current_revision_id": None})
        )


def test_fact_head_identity_and_revoked_evidence_cannot_publish(dependencies):
    h = dependencies
    next_revision = fact_revision(h, version=2, parent_revision_id=h.initial.id)
    h.store.insert_fact_revision(next_revision)
    for changes in ({"key": "changed"}, {"kind": "requirement"}, {"created_by": h.member}):
        with pytest.raises(InvalidTransitionError):
            h.store.update_fact(
                h.fact.model_copy(
                    update={"version": 3, "current_revision_id": next_revision.id, **changes}
                ),
                2,
            )
    h.store.revoke_native_intake_source(h.workspace, h.first.id, h.source.id, utc_now())
    with pytest.raises(InvalidTransitionError):
        h.store.update_fact(
            h.fact.model_copy(update={"version": 3, "current_revision_id": next_revision.id}), 2
        )


def test_declarations_cas_exact_input_binding_and_one_concurrent_winner(dependencies):
    h = dependencies
    first = declaration(h)
    h.store.insert_dependency_declaration(first, 0)

    def append(notes):
        record = declaration(h, version=2, notes=notes)
        try:
            h.store.insert_dependency_declaration(record, 1)
            return record
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(append, ("first", "second")))
    winner = next(r for r in results if r is not None)
    assert sum(r is not None for r in results) == 1
    assert h.store.dependency_declaration(h.workspace, h.first.id, h.revision.id) == winner
    assert h.store.dependency_declarations(h.workspace, h.first.id, h.revision.id) == (
        first,
        winner,
    )
    assert h.store.dependency_declaration(h.workspace, h.second.id, h.revision.id) is None
    for changes in (
        {"sha256": "f" * 64},
        {"project_id": h.second.id},
        {"dependencies": (first.dependencies[0].model_copy(update={"sha256": "f" * 64}),)},
        {"dependencies": (first.dependencies[0].model_copy(update={"input_id": uuid4()}),)},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_dependency_declaration(declaration(h, version=3, **changes), 2)


def test_impact_proposal_scope_immutable_copies_and_transaction_rollback(dependencies):
    h = dependencies
    advance_fact(h)
    impact = assessment(h)
    h.store.insert_impact_assessment(impact)
    plan = {
        "assessment_id": str(impact.id),
        "graph_fingerprint": impact.graph_fingerprint,
        "source_fingerprint": impact.source_fingerprint,
        "outputs": impact.outputs,
    }
    proposal = NativeRepairProposal(
        workspace_id=h.workspace,
        project_id=h.first.id,
        assessment_id=impact.id,
        sha256=snapshot_hash(plan),
        plan=plan,
        created_by=h.owner,
    )
    h.store.insert_repair_proposal(proposal)
    assert h.store.repair_proposal(h.workspace, h.first.id, proposal.id) == proposal
    assert h.store.impact_assessments(h.workspace, h.first.id) == (impact,)
    assert h.store.repair_proposals(h.workspace, h.first.id) == (proposal,)
    proposal.plan["outputs"][0]["status"] = "caller mutation"
    assert (
        h.store.repair_proposal(h.workspace, h.first.id, proposal.id).plan["outputs"][0]["status"]
        == "stale"
    )
    for value, method in (
        (impact, h.store.insert_impact_assessment),
        (proposal, h.store.insert_repair_proposal),
    ):
        with pytest.raises(InvalidTransitionError):
            method(value)
        with pytest.raises(InvalidTransitionError):
            method(value.model_copy(update={"id": uuid4(), "project_id": h.second.id}))
    assert h.store.impact_assessment(h.workspace, h.second.id, impact.id) is None
    assert h.store.repair_proposal(h.foreign_workspace, h.first.id, proposal.id) is None
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.insert_impact_assessment(assessment(h))
        raise RuntimeError("rollback all evidence")
    assert h.store.impact_assessments(h.workspace, h.first.id) == (impact,)


def test_impact_rejects_other_fact_revisions_and_foreign_output(dependencies):
    h = dependencies
    advance_fact(h)
    for changes in (
        {"from_revision_id": h.changed.id, "to_revision_id": h.initial.id},
        {"fact_id": uuid4()},
        {"outputs": ({"artifact_id": str(uuid4()), "revision_id": str(h.revision.id)},)},
        {
            "outputs": (
                {
                    "artifact_id": str(h.artifact.id),
                    "revision_id": str(h.revision.id),
                    "sha256": "f" * 64,
                },
            )
        },
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_impact_assessment(assessment(h, **changes))


def test_artifact_task_conflict_includes_original_pending_and_current_revision_tasks(dependencies):
    h = dependencies
    assert not h.store.artifact_task_conflict(h.workspace, h.first.id, h.artifact.id, (h.task.id,))
    assert not h.store.artifact_task_conflict(h.workspace, h.first.id, h.artifact.id, ())
    h.artifact = h.artifact.model_copy(update={"version": 3, "pending_repair_task_id": h.other.id})
    h.store.update_artifact(h.artifact, 2)
    assert h.store.artifact_task_conflict(h.workspace, h.first.id, uuid4(), (h.task.id,))
    assert h.store.artifact_task_conflict(h.workspace, h.first.id, uuid4(), (h.other.id,))
    repaired = revision_record(h, sequence=2, parent_revision_id=h.revision.id, task_id=h.other.id)
    h.store.insert_artifact_revision(repaired)
    h.artifact = h.artifact.model_copy(
        update={
            "version": 4,
            "current_revision_id": repaired.id,
            "pending_repair_task_id": None,
        }
    )
    h.store.update_artifact(h.artifact, 3)
    assert h.store.artifact_task_conflict(h.workspace, h.first.id, uuid4(), (h.other.id,))
    assert not h.store.artifact_task_conflict(h.workspace, h.second.id, uuid4(), (h.other.id,))
    assert not h.store.artifact_task_conflict(
        h.foreign_workspace, h.first.id, uuid4(), (h.other.id,)
    )
    other = h.artifact.model_copy(
        update={
            "id": uuid4(),
            "task_id": h.other.id,
            "version": 1,
            "current_revision_id": None,
        }
    )
    h.store.insert_artifact(other)
    assert h.store.artifact_task_conflict(h.workspace, h.first.id, other.id, (h.other.id,))


def test_repair_proposal_can_capture_pending_repair_task_but_rejects_unrelated_task(dependencies):
    h = dependencies
    advance_fact(h)
    pending = h.artifact.model_copy(update={"version": 3, "pending_repair_task_id": h.other.id})
    h.store.update_artifact(pending, 2)
    impact = assessment(h)
    h.store.insert_impact_assessment(impact)
    plan = {
        "assessment_id": str(impact.id),
        "outputs": [
            {
                "artifact_id": str(h.artifact.id),
                "revision_id": str(h.revision.id),
                "before_sha256": h.revision.sha256,
                "task_id": str(h.other.id),
            }
        ],
    }
    proposal = NativeRepairProposal(
        workspace_id=h.workspace,
        project_id=h.first.id,
        assessment_id=impact.id,
        sha256=snapshot_hash(plan),
        plan=plan,
        created_by=h.owner,
    )
    h.store.insert_repair_proposal(proposal)
    assert h.store.repair_proposal(h.workspace, h.first.id, proposal.id) == proposal
    plan["outputs"][0]["task_id"] = str(uuid4())
    forged = proposal.model_copy(
        update={"id": uuid4(), "plan": plan, "sha256": snapshot_hash(plan)}
    )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_repair_proposal(forged)


@pytest.mark.postgres
def test_sql_dependency_histories_reject_update_delete_and_scoped_reference_forgery(postgres_url):
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb

    from simon.adapters.postgres import PostgresStore
    from simon.domain.native_dependencies import canonical_bytes

    store = PostgresStore(postgres_url)
    try:
        h = dependencies.__wrapped__(store)
        advance_fact(h)
        first = declaration(h)
        store.insert_dependency_declaration(first, 0)
        impact = assessment(h)
        store.insert_impact_assessment(impact)
        proposal = NativeRepairProposal(
            workspace_id=h.workspace,
            project_id=h.first.id,
            assessment_id=impact.id,
            sha256=snapshot_hash({}),
            plan={},
            created_by=h.owner,
        )
        store.insert_repair_proposal(proposal)
        records = (
            ("native_fact_revisions", h.initial),
            ("native_dependency_declarations", first),
            ("native_impact_assessments", impact),
            ("native_repair_proposals", proposal),
        )
        for table, record in records:
            for statement in (
                "UPDATE {} SET snapshot=snapshot WHERE id=%s",
                "DELETE FROM {} WHERE id=%s",
            ):
                with (
                    pytest.raises(psycopg.IntegrityError),
                    psycopg.connect(postgres_url) as connection,
                ):
                    connection.execute(
                        sql.SQL(statement).format(sql.Identifier(table)), (record.id,)
                    )
        with pytest.raises(psycopg.IntegrityError), psycopg.connect(postgres_url) as connection:
            connection.execute(
                "UPDATE native_facts SET key='forged',"
                "snapshot=jsonb_set(snapshot,'{key}','\"forged\"') WHERE id=%s",
                (h.fact.id,),
            )
        forged = assessment(h, project_id=h.second.id)
        with pytest.raises(psycopg.IntegrityError), psycopg.connect(postgres_url) as connection:
            connection.execute(
                "INSERT INTO native_impact_assessments (id,workspace_id,project_id,fact_id,"
                "from_revision_id,to_revision_id,graph_fingerprint,source_fingerprint,outputs_bytes,"
                "created_by,created_at,snapshot) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    forged.id,
                    forged.workspace_id,
                    forged.project_id,
                    forged.fact_id,
                    forged.from_revision_id,
                    forged.to_revision_id,
                    forged.graph_fingerprint,
                    forged.source_fingerprint,
                    canonical_bytes(forged.outputs),
                    forged.created_by,
                    forged.created_at,
                    Jsonb(forged.model_dump(mode="json")),
                ),
            )
    finally:
        store.close()


@pytest.mark.postgres
def test_pg_repair_second_output_quota_is_atomic_and_receipt_survives_restart(
    postgres_url, monkeypatch
):
    from uuid import UUID

    from simon.adapters.postgres import PostgresStore
    from simon.domain.errors import StorageQuotaExceededError
    from simon.services.native_dependencies import NativeDependencyService
    from tests.unit.test_native_dependencies import bytes_for, commit, simple_repair
    from tests.unit.test_storage_workflows import limit

    store = PostgresStore(postgres_url, pool_size=4)
    restarted = None
    try:
        monkeypatch.setattr("tests.unit.test_project_models.InMemoryStore", lambda: store)
        h, _, _, first, second, plan = simple_repair()
        identifiers = tuple(UUID(item["id"]) for item in (first, second))
        before_heads = tuple(store.artifact(h.workspace, h.first.id, id) for id in identifiers)
        before_tasks = tuple(
            store.native_task(h.workspace, h.first.id, item.task_id) for item in before_heads
        )
        before_declarations = tuple(
            store.dependency_declaration(h.workspace, h.first.id, item.current_revision_id)
            for item in before_heads
        )
        originals = [bytes_for(h, item) for item in (first, second)]
        usage = store.storage_usage(h.workspace)["retained_bytes"]
        first_bytes = len(plan["proposal"]["plan"]["outputs"][0]["after_text"].encode())
        limit(h, usage + first_bytes)
        with pytest.raises(StorageQuotaExceededError):
            commit(h, plan)
        assert [bytes_for(h, item) for item in (first, second)] == originals
        assert store.storage_usage(h.workspace)["retained_bytes"] == usage
        assert (
            tuple(store.artifact(h.workspace, h.first.id, id) for id in identifiers) == before_heads
        )
        assert (
            tuple(store.native_task(h.workspace, h.first.id, item.task_id) for item in before_heads)
            == before_tasks
        )
        assert (
            tuple(
                store.dependency_declaration(h.workspace, h.first.id, item.current_revision_id)
                for item in before_heads
            )
            == before_declarations
        )
        limit(h, usage + 1000)
        result = commit(h, plan)
        assert len(result["committed_revision_ids"]) == 2
        after = store.storage_usage(h.workspace)
        counts = tuple(
            len(store.artifact_revisions(h.workspace, h.first.id, id)) for id in identifiers
        )
        # A fresh adapter and service must retrieve the durable proposal receipt,
        # even though the proposal's original graph no longer matches new heads.
        restarted = PostgresStore(postgres_url, pool_size=4)
        h.dependencies = NativeDependencyService(restarted, h.artifacts)
        assert commit(h, plan)["committed_revision_ids"] == result["committed_revision_ids"]
        assert restarted.storage_usage(h.workspace) == after
        assert (
            tuple(
                len(restarted.artifact_revisions(h.workspace, h.first.id, id)) for id in identifiers
            )
            == counts
        )
        assert all(count == 2 for count in counts)
    finally:
        if restarted is not None:
            restarted.close()
        store.close()
