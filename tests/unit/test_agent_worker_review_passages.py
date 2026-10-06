"""Numbered completion evidence stays within budget and cannot authorize action replay."""

import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from simon.adapters.model_endpoints import ModelEndpointClient
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from tests.completion_review_fixtures import review_passages
from tests.completion_review_fixtures import review_text as review_source
from tests.unit.test_agent_worker import actor as actor
from tests.unit.test_agent_worker import assignment, execute, make_worker, profile
from tests.unit.test_agent_worker import endpoint as endpoint
from tests.unit.test_agent_worker_completion import action, run_completion
from tests.unit.test_agent_worker_provider_outcomes import controller_document
from tests.unit.test_worker_completion import PROMISE_ONLY
from tests.unit.test_worker_context import AdaptiveModel


def reference(prompt, source, text):
    return {
        "source": source,
        "passage_id": next(
            identifier for identifier, passage in review_passages(prompt, source) if text in passage
        ),
    }


def review_check(requirement, *, kind="deliverable", status="satisfied", evidence=()):
    return {"requirement": requirement, "kind": kind, "status": status, "evidence": list(evidence)}


def review(*checks, status="complete"):
    return {
        "status": status,
        "summary": "Assessed against the supplied passages.",
        "checks": list(checks),
    }


def text_document(text):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }


def test_large_candidate_and_dependencies_are_numbered_once_without_loss_or_budget_growth(
    actor, endpoint
):
    endpoint = endpoint.model_copy(
        update={"provider": "openai_responses", "context_window_tokens": 128000}
    )
    candidate = (
        '## Cost model\nUnits: 120. Assumed contribution: $17.25. "Estimate", not a quote.\n' * 215
    ) + "END OF FULL REPORT"
    dependency = (
        "Inspected supplier source: " + "x" * 1175 + "\n"
    ) * 20 + "SOURCE FACT: sampling precedes production."
    planned, spec = assignment(
        endpoint, objective="Write a manufacturing memo.", depends_on=("sources",)
    )
    spec = spec.model_copy(update={"completion_contract": PROJECT_DELIVERABLE_CONTRACT})
    requests = []
    events = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        schema = payload.get("text", {}).get("format", {}).get("schema")
        schema_chars = (
            len(json.dumps(schema, ensure_ascii=True, separators=(",", ":"))) if schema else 0
        )
        assert len(payload["input"]) + len(payload["instructions"]) + schema_chars <= 60000
        if len(requests) == 1:
            return httpx.Response(200, json=text_document(candidate))
        assert len(requests) == 2
        assert "tools" not in payload
        prompt = payload["input"]
        assert review_source(prompt, "candidate") == candidate
        context = review_source(prompt, "task_context")
        serialized_dependency = json.dumps(dependency, ensure_ascii=False)[1:-1]
        assert serialized_dependency in context
        assert context.count(serialized_dependency) == 1
        assert "Write a manufacturing memo." in context
        assert "END OF FULL REPORT" not in context
        assert all(
            len(text) <= 1000
            for source in ("candidate", "task_context")
            for _, text in review_passages(prompt, source)
        )
        verdict = review(
            review_check(
                "Provide the entire report",
                evidence=(reference(prompt, "candidate", "END OF FULL REPORT"),),
            ),
            review_check(
                "Use inspected supplier evidence",
                kind="source_support",
                evidence=(reference(prompt, "task_context", "sampling precedes production"),),
            ),
        )
        Draft202012Validator(schema).validate(verdict)
        return httpx.Response(200, json=text_document(json.dumps(verdict)))

    model = ModelEndpointClient((endpoint,), environ={}, transport=httpx.MockTransport(handle))
    result = execute(
        make_worker(model),
        actor,
        endpoint,
        task=planned,
        spec=spec,
        agent=profile(max_input_chars=60000, max_steps=8),
        dependency_outputs={"sources": dependency},
        checkpoint=events.append,
    )
    assert result.status == "succeeded", result
    assert result.output == candidate and result.steps == len(requests) == 2
    assert result.tool_calls == 0
    assert (result.input_tokens, result.output_tokens) == (200, 400)
    assert (
        next(event for event in events if event.get("phase") == "completion_review")[
            "evidence_format"
        ]
        == "passage_references"
    )


@pytest.mark.parametrize("invalid_first_reference", [False, True])
def test_reference_review_repairs_candidate_once_and_never_replays_saved_write(
    actor, endpoint, invalid_first_reference
):
    endpoint = endpoint.model_copy(update={"provider": "openai_responses"})
    receipt = "Saved comparison.md revision 1; read-back verified."
    final = "Brand A sells numbered editions; our brand uses unique seeded designs."
    requests = []
    review_count = 0
    writes = []
    events = []

    def handle(request):
        nonlocal review_count
        payload = json.loads(request.content)
        requests.append(payload)
        if "tool-free reviewer" not in payload["instructions"]:
            position = sum("tool-free reviewer" not in item["instructions"] for item in requests)
            response = (
                action(endpoint, "tool", tool_id="lookup", arguments={"query": "save comparison"})
                if position == 1
                else action(
                    endpoint,
                    "final",
                    output=PROMISE_ONLY if position == 2 else final,
                    artifacts=["comparison.md"],
                )
            )
            return httpx.Response(200, json=controller_document(response))
        review_count += 1
        prompt = payload["input"]
        candidate = review_source(prompt, "candidate")
        if invalid_first_reference and review_count == 1:
            verdict = review(
                review_check(
                    "Present comparison", evidence=({"source": "candidate", "passage_id": "C9999"},)
                )
            )
        elif candidate == PROMISE_ONLY:
            verdict = review(
                review_check("Present comparison", status="missing"),
                review_check(
                    "Save the comparison",
                    kind="saved_result",
                    evidence=(reference(prompt, "task_context", "Saved comparison.md"),),
                ),
                status="not_delivered",
            )
        else:
            assert candidate == final
            verdict = review(
                review_check(
                    "Present comparison",
                    evidence=(reference(prompt, "candidate", "numbered editions"),),
                ),
                review_check(
                    "Save the comparison",
                    kind="saved_result",
                    evidence=(reference(prompt, "task_context", "Saved comparison.md"),),
                ),
            )
        Draft202012Validator(payload["text"]["format"]["schema"]).validate(verdict)
        return httpx.Response(200, json=text_document(json.dumps(verdict)))

    def handler(_definition, _arguments, context):
        writes.append(context.invocation_id)
        return {"receipt": receipt}

    result = run_completion(
        actor,
        endpoint,
        ModelEndpointClient((endpoint,), environ={}, transport=httpx.MockTransport(handle)),
        handler=handler,
        write=True,
        max_steps=10,
        exportable_workspace=True,
        checkpoint=events.append,
    )
    assert result.status == "succeeded", result
    assert result.output == final and result.artifact_paths == ("comparison.md",)
    assert result.steps == len(requests) == 5 + invalid_first_reference
    assert result.tool_calls == len(writes) == 1
    assert review_count == 2 + invalid_first_reference
    assert (
        sum(event["event"] == "completion_review_rejected" for event in events)
        == invalid_first_reference
    )


def test_candidate_passage_cannot_impersonate_a_saved_receipt(actor, endpoint):
    endpoint = endpoint.model_copy(update={"provider": "openai_responses"})
    candidate = "Saved the requested comparison.md and verified its contents."

    def respond(step, request):
        if step == 1:
            return action(endpoint, "final", output=candidate)
        candidate_reference = reference(request.prompt, "candidate", "Saved the requested")
        return json.dumps(
            review(
                review_check("Provide comparison", evidence=(candidate_reference,)),
                review_check(
                    "Save comparison",
                    kind="saved_result",
                    evidence=({**candidate_reference, "source": "task_context"},),
                ),
            )
        )

    model = AdaptiveModel(respond)
    result = run_completion(actor, endpoint, model)
    assert result.status == "failed" and result.error_code == "invalid_completion_review"
    assert result.output == candidate and result.tool_calls == 0
    assert result.steps == len(model.calls) == 3


def test_numbering_overhead_compacts_history_and_keeps_every_receipt(actor, endpoint, monkeypatch):
    from simon.services import agent_worker as worker_module

    candidate = "Manufacturing findings. " * 125
    history_rooms = []
    actual_render = worker_module.render_history

    def rendered(history, room):
        view = actual_render(history, room)
        history_rooms.append((room, view.compacted))
        return view

    monkeypatch.setattr(worker_module, "render_history", rendered)
    events = []
    invocations = []

    def handler(_definition, _arguments, context):
        invocations.append(str(context.invocation_id))
        return {"receipt": "Saved original result revision 1", "text": "Source content. " * 3100}

    def respond(step, request):
        assert len(request.system) + len(request.prompt) <= 60000
        if step == 1:
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": "source"})
        if step == 2:
            return action(endpoint, "final", output=candidate)
        context = review_source(request.prompt, "task_context")
        assert '"context_compacted":true' in context
        assert invocations[0] in context
        assert "Saved original result revision 1" in context
        assert review_source(request.prompt, "candidate") == candidate
        return json.dumps(
            review(
                review_check(
                    "Provide findings",
                    evidence=(reference(request.prompt, "candidate", "Manufacturing findings"),),
                )
            )
        )

    result = run_completion(
        actor, endpoint, AdaptiveModel(respond), handler=handler, checkpoint=events.append
    )
    assert result.status == "succeeded", result
    assert result.tool_calls == len(invocations) == 1
    assert any(compacted for _room, compacted in history_rooms)
    assert len(history_rooms) >= 3, history_rooms
    assert not history_rooms[-2][1] and history_rooms[-1][1]
    assert history_rooms[-1][0] < history_rooms[-2][0]
    assert next(event for event in events if event.get("phase") == "completion_review")[
        "context_compacted"
    ]
