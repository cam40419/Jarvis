import hashlib
import json
from uuid import uuid4

import pytest

from simon.domain.agent_runs import TaskExecution
from simon.domain.artifacts import ArtifactError
from simon.domain.models import utc_now
from simon.services.artifacts import ArtifactStore
from simon.services.dependency_receipts import MAX_RECEIPT_CHARS, dependency_receipt_context
from tests.unit.test_agent_worker import actor as actor


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def saved_receipt(store, actor, run_id, task_id, project_id, *, mutate=lambda entry: None):
    invocation = uuid4()
    output = {
        "id": str(uuid4()),
        "run_id": str(run_id),
        "plan_id": str(uuid4()),
        "task_id": "research",
        "agent_id": "researcher",
        "name": "answer.txt",
        "media_type": "text/plain",
        "size": 120,
        "sha256": "b" * 64,
        "created_at": utc_now().isoformat(),
        "download_url": "/not-carried-to-model",
        "project_copy": {
            "root": f"project:{project_id}",
            "path": "outputs/strategy.md",
            "revision": "b" * 64,
            "bytes": 120,
            "saved_at": utc_now().isoformat(),
            "download_url": "/not-carried-to-model",
        },
    }
    entry = {
        "tool_id": "project.output_save",
        "invocation_id": str(invocation),
        "status": "succeeded",
        "side_effect": True,
        "arguments": {"run_id": str(run_id), "artifact_id": output["id"]},
        "output": output,
    }
    mutate(entry)
    artifact = store.publish_text(
        workspace_id=actor.workspace_id,
        actor_id=actor.actor_id,
        run_id=run_id,
        task_id=task_id,
        name=f"{invocation}.json",
        media_type="application/json",
        text=encode(entry),
    )
    serialized = encode(entry["output"])
    return (
        {
            "event": "tool_dispatch",
            "tool_id": "project.output_save",
            "invocation_id": str(invocation),
            "side_effect": True,
        },
        {
            "event": "tool_complete",
            "tool_id": "project.output_save",
            "invocation_id": str(invocation),
            "status": "succeeded",
            "output_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
            "output_chars": len(serialized),
            "evidence_artifact": artifact.model_dump(mode="json"),
        },
    )


def context(store, actor, run_id, task_id, project_id, events, *, status="succeeded"):
    return dependency_receipt_context(
        store,
        actor=actor,
        run_id=run_id,
        project_id=project_id,
        dependencies=(TaskExecution(id="writer", agent_id="writer", status=status, events=events),),
        task_ids={"writer": task_id},
        revalidate=lambda: None,
    )


def test_only_verified_project_copy_facts_reach_dependency_context(tmp_path, actor):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = saved_receipt(store, actor, run_id, task_id, project_id)
    text = context(store, actor, run_id, task_id, project_id, events)["writer"]
    payload = json.loads(text.rsplit("\n", 1)[1])
    receipt = payload["receipts"][0]
    assert receipt["project_copy"]["path"] == "outputs/strategy.md"
    assert receipt["project_copy"]["revision"] == receipt["sha256"] == "b" * 64
    assert receipt["project_copy"]["bytes"] == receipt["bytes"] == 120
    assert "not-carried-to-model" not in text
    assert "historical actions, not new actions by this task" in text


@pytest.mark.parametrize("mismatch", ["actor", "workspace", "run", "task", "hash", "dispatch"])
def test_receipt_owner_identity_integrity_and_dispatch_must_match(tmp_path, actor, mismatch):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = saved_receipt(store, actor, run_id, task_id, project_id)
    if mismatch in {"actor", "workspace", "run", "task"}:
        field = {
            "actor": "actor_id",
            "workspace": "workspace_id",
            "run": "run_id",
            "task": "task_id",
        }[mismatch]
        events[1]["evidence_artifact"][field] = str(uuid4())
    elif mismatch == "hash":
        events[1]["output_sha256"] = "c" * 64
    else:
        events[0]["side_effect"] = False
    with pytest.raises(ArtifactError):
        context(store, actor, run_id, task_id, project_id, events)


@pytest.mark.parametrize("mismatch", ["unknown", "source", "root", "revision", "bytes", "null"])
def test_succeeded_event_does_not_override_invalid_or_unavailable_receipt(
    tmp_path, actor, mismatch
):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()

    def mutate(entry):
        if mismatch == "unknown":
            entry["status"] = "unknown"
        elif mismatch == "source":
            entry["arguments"]["artifact_id"] = str(uuid4())
        elif mismatch in {"root", "revision", "bytes"}:
            entry["output"]["project_copy"][mismatch] = {
                "root": f"project:{uuid4()}",
                "revision": "c" * 64,
                "bytes": 121,
            }[mismatch]

    events = saved_receipt(store, actor, run_id, task_id, project_id, mutate=mutate)
    if mismatch == "null":
        events[1]["evidence_artifact"] = None
    with pytest.raises(ArtifactError):
        context(store, actor, run_id, task_id, project_id, events)


def test_legacy_outcome_without_archive_is_not_verified_action_proof(tmp_path, actor):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = saved_receipt(store, actor, run_id, task_id, project_id)
    events[1].pop("evidence_artifact")
    assert context(store, actor, run_id, task_id, project_id, events) == {}


@pytest.mark.parametrize("tool_id", [None, [], {}, 12])
def test_malformed_completed_tool_id_is_a_controlled_integrity_failure(tmp_path, actor, tool_id):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = saved_receipt(store, actor, run_id, task_id, project_id)
    events[1]["tool_id"] = tool_id
    with pytest.raises(ArtifactError, match="could not be verified"):
        context(store, actor, run_id, task_id, project_id, events)


def test_failed_dependency_or_unknown_call_never_becomes_save_proof(tmp_path, actor):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = saved_receipt(store, actor, run_id, task_id, project_id)
    with pytest.raises(ArtifactError):
        context(store, actor, run_id, task_id, project_id, events, status="failed")
    events[1]["status"] = "unknown"
    assert context(store, actor, run_id, task_id, project_id, events) == {}


def test_receipt_context_has_fixed_total_bound(tmp_path, actor):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = tuple(
        event
        for _ in range(12)
        for event in saved_receipt(
            store,
            actor,
            run_id,
            task_id,
            project_id,
            mutate=lambda entry: entry["output"]["project_copy"].update(path="x" * 1000),
        )
    )
    result = context(store, actor, run_id, task_id, project_id, events)
    assert sum(map(len, result.values())) <= MAX_RECEIPT_CHARS
    assert 0 < len(json.loads(result["writer"].rsplit("\n", 1)[1])["receipts"]) < 12


def read_receipt(store, actor, run_id, task_id, project_id, *, mismatch=None):
    def mutate(entry):
        item = entry["output"]
        if mismatch == "absent":
            item["project_copy"] = None
        elif mismatch is not None:
            item["project_copy"][mismatch] = {
                "root": f"project:{uuid4()}",
                "revision": "c" * 64,
                "bytes": 121,
                "path": "../outside.md",
            }[mismatch]
        entry.update(
            tool_id="project.output_read",
            side_effect=False,
            output={"output": item, "text": "report", "characters": 6},
        )

    events = saved_receipt(store, actor, run_id, task_id, project_id, mutate=mutate)
    for event in events:
        event["tool_id"] = "project.output_read"
    events[0]["side_effect"] = False
    return events


def test_read_carries_existing_copy_metadata_without_claiming_new_save(tmp_path, actor):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = read_receipt(store, actor, run_id, task_id, project_id)
    text = context(store, actor, run_id, task_id, project_id, events)["writer"]
    receipt = json.loads(text.rsplit("\n", 1)[1])["receipts"][0]
    assert receipt["project_copy"]["revision"] == receipt["sha256"] == "b" * 64
    assert receipt["project_copy_evidence"] == "existing_copy_metadata"
    assert receipt["tool_id"] == "project.output_read"
    assert receipt["read"] == {"offset": 0, "characters": 6, "total_chars": 6}
    assert "not proof of a later edit" in text


@pytest.mark.parametrize("mismatch", ["root", "revision", "bytes", "path"])
def test_existing_copy_metadata_on_read_requires_same_integrity(tmp_path, actor, mismatch):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = read_receipt(store, actor, run_id, task_id, project_id, mismatch=mismatch)
    with pytest.raises(ArtifactError):
        context(store, actor, run_id, task_id, project_id, events)


def test_read_without_existing_copy_remains_valid(tmp_path, actor):
    store = ArtifactStore(tmp_path)
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    events = read_receipt(store, actor, run_id, task_id, project_id, mismatch="absent")
    text = context(store, actor, run_id, task_id, project_id, events)["writer"]
    receipt = json.loads(text.rsplit("\n", 1)[1])["receipts"][0]
    assert "project_copy" not in receipt and "project_copy_evidence" not in receipt
    assert receipt["read"]["characters"] == 6
