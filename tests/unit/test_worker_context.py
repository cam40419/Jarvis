import hashlib
import json
from uuid import uuid4

import pytest

from simon.domain.artifacts import Artifact, ArtifactError
from simon.domain.model_routing import TextGenerationResult
from simon.domain.tool_catalog import ToolExecutionError
from simon.services.agent_worker import WorkerCheckpointError
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from simon.services.worker_context import (
    ContextLimitError,
    EvidenceReadError,
    ToolEvidenceBuffer,
    render_history,
)
from tests.completion_review_fixtures import review_text as review_source
from tests.unit.test_agent_dispatcher import ControlledModel, make_harness, task
from tests.unit.test_agent_worker import (
    actor as actor_fixture,
)
from tests.unit.test_agent_worker import (
    assignment,
    controller,
    execute,
    final,
    make_worker,
    profile,
    tool,
)
from tests.unit.test_agent_worker import (
    endpoint as endpoint_fixture,
)

actor = actor_fixture
endpoint = endpoint_fixture


def entry(output, **changes):
    return {
        "tool_id": "lookup",
        "invocation_id": str(uuid4()),
        "status": "succeeded",
        "side_effect": False,
        "arguments": {"project_id": str(uuid4()), "file_id": "source-file"},
        "output": output,
        **changes,
    }


def capture(buffer, item):
    text, reference = buffer.capture(item)
    return text, {**item, "evidence": reference}


def test_compaction_keeps_every_receipt_reference_and_explicit_omission():
    buffer = ToolEvidenceBuffer()
    history = [
        capture(
            buffer,
            entry(
                {
                    "id": f"document-{index}",
                    "revision": "original-revision",
                    "read_context": {"project_id": "project-id"},
                    "source_account_id": "source-account",
                    "text": "a" * 20000,
                },
                side_effect=index == 2,
            ),
        )[1]
        for index in range(4)
    ]
    view = render_history(history, 18000)
    parsed = json.loads(view.text)
    assert view.compacted and len(view.text) <= 18000 < view.original_chars
    assert len(parsed["calls"]) == 4
    for original, projected in zip(history, parsed["calls"], strict=True):
        assert projected["invocation_id"] == original["invocation_id"]
        assert projected["status"] == "succeeded"
        assert projected["side_effect"] == original["side_effect"]
        assert projected["arguments"] == original["arguments"]
        for key in ("id", "revision", "read_context", "source_account_id"):
            assert projected["output"][key] == original["output"][key]
        assert projected["output"]["text"]["omitted_chars"] > 0
        assert projected["output"]["text"]["evidence_pointer"] == "/output/text"
    assert history[0]["output"]["text"] == "a" * 20000
    assert "never repeat" in parsed["notice"].lower()


def test_unchanged_small_history_and_failed_receipts_remain_exact():
    history = [entry({"status": "failed", "error": "tool_read_failed"}, status="failed")]
    view = render_history(history, 60000)
    assert not view.compacted
    assert json.loads(view.text) == history
    with pytest.raises(ContextLimitError):
        render_history(history, 20)


def test_buffer_detaches_input_and_reads_exact_middle_without_source_replay():
    buffer = ToolEvidenceBuffer()
    source = entry({"text": "start" + "a" * 30000 + "critical middle" + "b" * 30000})
    text, record = capture(buffer, source)
    source["output"]["text"] = "changed mutable provider object"
    page = buffer.read(
        {
            "invocation_id": record["invocation_id"],
            "pointer": "/output/text",
            "offset": 30005,
            "limit": 15,
        }
    )
    assert page["text"] == "critical middle"
    assert page["next_offset"] == 30020
    assert page["partial"] is True
    assert record["evidence"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert "critical middle" in text


@pytest.mark.parametrize(
    "updates",
    [
        {"invocation_id": str(uuid4())},
        {"pointer": "/output/missing"},
        {"pointer": "/output/~bad"},
        {"pointer": "output/text"},
        {"pointer": "/output/values/10"},
        {"offset": -1},
        {"offset": True},
        {"offset": 8 * 1024 * 1024 + 1},
        {"limit": 0},
        {"limit": 8001},
        {"limit": True},
        {"path": "C:/private/file"},
    ],
)
def test_evidence_read_cannot_escape_captured_task_or_bounds(updates):
    buffer = ToolEvidenceBuffer()
    item = entry({"text": "the evidence", "values": ["one"]})
    buffer.capture(item)
    arguments = {
        "invocation_id": item["invocation_id"],
        "pointer": "/output/text",
        "offset": 0,
        "limit": 100,
    }
    with pytest.raises(EvidenceReadError):
        buffer.read({**arguments, **updates})
    with pytest.raises(EvidenceReadError):
        ToolEvidenceBuffer().read(arguments)


def test_json_pointer_escape_array_and_unicode_pages_are_exact():
    buffer = ToolEvidenceBuffer()
    item = entry({"a/b~c": ["日本語 and facts"]})
    buffer.capture(item)
    page = buffer.read(
        {
            "invocation_id": item["invocation_id"],
            "pointer": "/output/a~1b~0c/0",
            "offset": 0,
            "limit": 3,
        }
    )
    assert page["text"] == "日本語"
    assert page["next_offset"] == 3
    assert page["format"] == "text"


def test_list_and_nested_omissions_have_retrievable_pointers():
    buffer = ToolEvidenceBuffer()
    item = entry({"files": [{"id": str(index), "text": "x" * 2000} for index in range(100)]})
    _text, saved = capture(buffer, item)
    view = json.loads(render_history([saved], 4000).text)
    files = view["calls"][0]["output"]["files"]
    assert files["total_items"] == 100 and files["omitted_items"] > 0
    page = buffer.read(
        {
            "invocation_id": item["invocation_id"],
            "pointer": "/output/files/99/id",
            "offset": 0,
            "limit": 100,
        }
    )
    assert page["text"] == "99"


class AdaptiveModel:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def generate(self, decision, request):
        self.calls.append(request)
        return TextGenerationResult(
            endpoint_id=decision.endpoint_id,
            model=decision.model,
            text=self.respond(len(self.calls), request),
            input_tokens=10,
            output_tokens=10,
        )


def test_four_document_reads_finish_within_original_60k_limit(actor, endpoint):
    outputs = [1800, 2500, 21112, 19427]
    calls = []
    events = []

    def handler(definition, arguments, context):
        index = len(calls)
        calls.append(context.invocation_id)
        return {
            "id": f"doc-{index}",
            "read_context": {"project_id": "authorized"},
            "text": "evidence " + "x" * outputs[index],
        }

    def respond(step, request):
        assert "TASK REQUIREMENTS" in request.system
        assert "PREDECESSOR FINDINGS" in request.prompt
        assert len(request.system) + len(request.prompt) <= 60000
        return (
            controller(query=f"document-{step}")
            if step <= 4
            else final("Evidence from four documents supports a limited manufacturing assessment.")
        )

    model = AdaptiveModel(respond)
    planned, spec = assignment(endpoint, tools=True, depends_on=("earlier",))
    result = execute(
        make_worker(model, handler=handler),
        actor,
        endpoint,
        tools=True,
        task=planned,
        spec=spec,
        agent=profile(tools=True, max_input_chars=60000).model_copy(
            update={"instructions": "TASK REQUIREMENTS " + "i" * 14900}
        ),
        dependency_outputs={"earlier": "PREDECESSOR FINDINGS " + "d" * 4000},
        checkpoint=events.append,
    )
    assert result.status == "succeeded" and result.steps == 5 and result.tool_calls == 4, result
    assert len(calls) == len(set(calls)) == 4
    assert all(record.status == "succeeded" for record in result.provenance)
    assert any(event.get("context_compacted") for event in events)
    assert '"context_compacted":true' in model.calls[-1].prompt
    for index in range(4):
        assert f"doc-{index}" in model.calls[-1].prompt


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_internal_evidence_action_reads_omitted_middle_without_replaying_tool(
    actor, endpoint, write, structured
):
    if structured:
        endpoint = endpoint.model_copy(update={"provider": "openai_responses"})
    calls = []
    events = []

    def handler(definition, arguments, context):
        calls.append(context.invocation_id)
        return {
            "id": "original-source",
            "status": "saved" if write else "read",
            "text": "x" * 50000 + "FACT IN THE MIDDLE" + "z" * 50000,
        }

    def respond(step, request):
        if step == 1:
            return controller()
        if step == 2:
            assert '"context_compacted":true' in request.prompt
            assert "FACT IN THE MIDDLE" not in request.prompt
            return json.dumps(
                {
                    "type": "evidence",
                    "invocation_id": str(calls[0]),
                    "pointer": "/output/text",
                    "offset": 50000,
                    "limit": 18,
                }
            )
        assert "FACT IN THE MIDDLE" in request.prompt
        return final("The saved evidence says FACT IN THE MIDDLE.")

    def wire_response(step, request):
        raw = respond(step, request)
        if not structured:
            return raw
        action = json.loads(raw)
        if action["type"] == "tool":
            action["arguments_json"] = json.dumps(action.pop("arguments"))
        elif action["type"] == "final":
            action["artifacts"] = []
        return json.dumps({"action": action})

    model = AdaptiveModel(wire_response)
    definition = tool(side_effect=write, action_policy="write" if write else "read")
    result = execute(
        make_worker(model, definition=definition, handler=handler),
        actor,
        endpoint,
        tools=True,
        agent=profile(tools=True, max_input_chars=20000, max_action="write" if write else "read"),
        checkpoint=events.append,
    )
    assert result.status == "succeeded"
    assert result.steps == 3 and result.tool_calls == len(calls) == 1
    assert len([event for event in events if event["event"] == "evidence_read"]) == 1
    assert result.provenance[0].status == "succeeded"


def test_authorization_checkpoint_blocks_evidence_read_without_new_provider_call(actor, endpoint):
    invocation = []

    def handler(definition, arguments, context):
        invocation.append(context.invocation_id)
        return {"text": "original source"}

    def respond(step, request):
        if step == 1:
            return controller()
        return json.dumps(
            {
                "type": "evidence",
                "invocation_id": str(invocation[0]),
                "pointer": "/output/text",
                "offset": 0,
                "limit": 30,
            }
        )

    def checkpoint(event):
        if event["event"] == "evidence_read":
            raise WorkerCheckpointError("assignment_no_longer_available")

    model = AdaptiveModel(respond)
    result = execute(
        make_worker(model, handler=handler), actor, endpoint, tools=True, checkpoint=checkpoint
    )
    assert result.error_code == "assignment_no_longer_available"
    assert result.status == "failed" and len(model.calls) == 2 and len(invocation) == 1


def test_unknown_write_never_enters_evidence_replay_or_new_model_step(actor, endpoint):
    def handler(*args):
        raise ToolExecutionError("Write outcome lost", unknown=True)

    model = AdaptiveModel(lambda *args: controller())
    result = execute(
        make_worker(
            model, handler=handler, definition=tool(side_effect=True, action_policy="write")
        ),
        actor,
        endpoint,
        tools=True,
        agent=profile(tools=True, max_action="write"),
    )
    assert result.status == "unknown" and result.error_code == "tool_execution_failed"
    assert len(model.calls) == result.tool_calls == 1


def test_archive_failure_keeps_known_successful_write_receipt_and_stops(actor, endpoint):
    events = []

    def fail_archive(*args):
        raise ArtifactError("Storage failed")

    model = AdaptiveModel(lambda *args: controller())
    result = execute(
        make_worker(model, definition=tool(side_effect=True, action_policy="write")),
        actor,
        endpoint,
        tools=True,
        agent=profile(tools=True, max_action="write"),
        evidence_writer=fail_archive,
        checkpoint=events.append,
    )
    assert result.status == "failed" and result.error_code == "evidence_storage_failed"
    assert len(model.calls) == result.tool_calls == 1
    receipts = [event for event in events if event["event"] == "tool_complete"]
    assert receipts[0]["status"] == "succeeded" and receipts[0]["output_sha256"]


def test_dispatcher_archives_scoped_full_evidence_separately_from_deliverables(tmp_path):
    harness = make_harness(tmp_path, tools=True)
    responses = iter([controller(), final("A complete sourced answer.")])
    dispatcher = harness.dispatcher(ControlledModel(lambda _: next(responses)))
    run = harness.queue((task("research"),))
    completed = dispatcher.execute(run.id)
    saved = completed.tasks[0]
    receipt = next(event for event in saved.events if event["event"] == "tool_complete")
    evidence = Artifact.model_validate(receipt["evidence_artifact"])
    assert evidence.actor_id == harness.actor.actor_id
    assert evidence.workspace_id == harness.actor.workspace_id
    assert evidence.run_id == run.id
    assert evidence not in saved.artifacts
    assert len(saved.artifacts) == 1 and saved.artifacts[0].name == "answer.txt"
    content = json.loads(dispatcher.evidence.read(evidence))
    assert content["output"] == {"value": "A tool result."}
    assert content["arguments"] == {"query": "fact"}
    assert content["status"] == "succeeded"
    assert content["invocation_id"] == receipt["invocation_id"]
    with pytest.raises(ArtifactError):
        dispatcher.evidence.read(evidence.model_copy(update={"actor_id": uuid4()}))


def test_large_candidate_with_dependencies_fails_review_safely_and_preserves_text(actor, endpoint):
    candidate = "A substantive partial assessment. " * 400
    planned, spec = assignment(endpoint, depends_on=("earlier",))
    spec = spec.model_copy(update={"completion_contract": PROJECT_DELIVERABLE_CONTRACT})
    model = AdaptiveModel(lambda *args: candidate)
    result = execute(
        make_worker(model),
        actor,
        endpoint,
        task=planned,
        spec=spec,
        agent=profile(max_input_chars=20000),
        dependency_outputs={"earlier": "Prior verified evidence. " * 300},
    )
    assert result.status == "failed" and result.error_code == "completion_review_unavailable"
    assert result.output == candidate
    assert result.steps == len(model.calls) == 1
    assert result.tool_calls == 0
    assert len(model.calls[0].system) + len(model.calls[0].prompt) <= 20000


def test_completion_review_compacts_evidence_after_reserving_candidate_and_dependencies(
    actor, endpoint
):
    candidate = (
        "The manufacturing assessment compares material sourcing and pilot production. " * 50
    )
    receipts = []
    events = []
    planned, spec = assignment(endpoint, tools=True, depends_on=("earlier",))
    spec = spec.model_copy(update={"completion_contract": PROJECT_DELIVERABLE_CONTRACT})

    def handler(definition, arguments, context):
        receipts.append(context.invocation_id)
        return {"id": "supplier-source", "text": "Verified sourcing notes. " * 2100}

    def respond(step, request):
        assert len(request.system) + len(request.prompt) <= 60000
        assert "PRIOR DEPENDENCY" in request.prompt
        if step == 1:
            return controller()
        if step == 2:
            return final(candidate)
        assert '"context_compacted":true' in request.prompt
        assert review_source(request.prompt, "candidate") == candidate
        assert "Granted tools:" not in request.system
        assert "supplier-source" in request.prompt
        return json.dumps(
            {
                "status": "complete",
                "summary": "The requested assessment is present.",
                "checks": [
                    {
                        "requirement": "Provide the manufacturing assessment",
                        "kind": "deliverable",
                        "status": "satisfied",
                        "evidence": [
                            {
                                "source": "candidate",
                                "excerpt": (
                                    "The manufacturing assessment compares material "
                                    "sourcing and pilot production."
                                ),
                            }
                        ],
                    }
                ],
            }
        )

    model = AdaptiveModel(respond)
    result = execute(
        make_worker(model, handler=handler),
        actor,
        endpoint,
        tools=True,
        task=planned,
        spec=spec,
        agent=profile(tools=True, max_input_chars=60000),
        dependency_outputs={"earlier": "PRIOR DEPENDENCY " + "d" * 7000},
        checkpoint=events.append,
    )
    assert result.status == "succeeded" and result.output == candidate
    assert result.steps == len(model.calls) == 3
    assert result.tool_calls == len(receipts) == 1
    review_dispatch = next(event for event in events if event.get("phase") == "completion_review")
    assert review_dispatch["context_compacted"] is True
