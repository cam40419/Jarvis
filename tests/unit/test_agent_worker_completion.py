"""A final proposal must pass its explicit contract without replaying known actions."""

import json
from uuid import uuid4

import pytest

from simon.adapters.tool_transports import TransportRegistry
from simon.services.agent_worker import AgentWorker, WorkerCheckpointError
from simon.services.tool_catalog import ToolCatalog
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from tests.completion_review_fixtures import review_text as review_source
from tests.unit.test_agent_worker import (
    Responses,
    assignment,
    profile,
    tool,
)
from tests.unit.test_agent_worker import actor as actor
from tests.unit.test_agent_worker import endpoint as endpoint
from tests.unit.test_worker_completion import COMPETITION_OBJECTIVE, PROMISE_ONLY, check, verdict


def action(endpoint, kind, **values):
    if endpoint.provider == "openai_responses":
        if kind == "tool":
            values["arguments_json"] = json.dumps(values.pop("arguments"))
        else:
            values.setdefault("artifacts", [])
        return json.dumps({"action": {"type": kind, **values}})
    return json.dumps({"type": kind, **values})


def rejected(candidate=PROMISE_ONLY):
    return verdict(
        check("Provide the competition document", status="missing", text=candidate),
        status="not_delivered",
        summary="The response promises a document but does not deliver it.",
    )


def run_completion(
    actor,
    endpoint,
    model,
    *,
    handler=None,
    write=False,
    tools=True,
    exportable_workspace=False,
    max_steps=10,
    objective=COMPETITION_OBJECTIVE,
    additional_instructions="Separate source-backed findings from unverified competitor claims.",
    checkpoint=lambda _event: None,
    cancelled=lambda: False,
):
    definition = tool(side_effect=write, action_policy="write" if write else "read")
    registry = TransportRegistry()
    if tools:
        registry.register("test", handler or (lambda *_args: {"answer": "Known source fact."}))
    worker = AgentWorker(model, ToolCatalog((definition,) if tools else ()), registry)
    task, spec = assignment(
        endpoint,
        tools=tools,
        objective=objective,
        completion_contract=PROJECT_DELIVERABLE_CONTRACT,
        additional_instructions=additional_instructions,
    )
    return worker.execute(
        actor=actor,
        run_id=uuid4(),
        task=task,
        spec=spec,
        profile=profile(
            tools=tools,
            max_steps=max_steps,
            max_action="write" if write else "read",
            output_instructions="Lead with the requested deliverable, then limitations.",
        ),
        dependency_outputs={},
        exportable_workspace=exportable_workspace,
        checkpoint=checkpoint,
        cancelled=cancelled,
    )


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses"])
def test_completion_repairs_promise_using_known_write_without_replaying_it(
    actor, endpoint, provider
):
    endpoint = endpoint.model_copy(update={"provider": provider})
    final = (
        "Competition comparison: Brand A sells numbered editions of 20, while Stdout's "
        "design system uses a unique seed per garment. Saved competition.md, revision 1."
    )
    receipt = "Saved competition.md revision 1"
    readback = "Read-back: Brand A editions of 20; Stdout unique seed per garment."
    model = Responses(
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "save comparison"}),
        action(endpoint, "final", output=PROMISE_ONLY),
        rejected(),
        action(endpoint, "final", output=final, artifacts=["competition.md"]),
        verdict(
            check("Provide the actual comparison", text="Brand A sells numbered editions of 20"),
            check("Save the document", kind="saved_result", source="task_context", text=receipt),
            check(
                "Verify the saved content",
                kind="verification",
                source="task_context",
                text=readback,
            ),
        ),
    )
    writes = []

    def handler(definition, arguments, context):
        writes.append(context.invocation_id)
        return {"receipt": receipt, "verified_content": readback}

    events = []
    result = run_completion(
        actor,
        endpoint,
        model,
        handler=handler,
        write=True,
        checkpoint=events.append,
        exportable_workspace=True,
    )
    assert result.status == "succeeded", result
    assert result.output == final
    assert result.artifact_paths == ("competition.md",)
    assert (result.steps, result.tool_calls, len(writes)) == (5, 1, 1)
    assert (result.input_tokens, result.output_tokens) == (100, 25)
    assert len(result.provenance) == 1 and result.provenance[0].status == "succeeded"
    assert [event["event"] for event in events].count("model_dispatch") == 5
    assert [event["event"] for event in events].count("model_complete") == 5
    assert [event["event"] for event in events].count("tool_dispatch") == 1
    assert any("completion" in event["event"] for event in events)
    for index in (2, 4):
        decision, request = model.calls[index]
        assert decision.request.required_capabilities == frozenset({"text"})
        assert not request.controller_mode
        assert "tool-free reviewer" in request.system
        assert "Separate source-backed findings" in request.system + request.prompt
        assert "Lead with the requested deliverable" in request.system + request.prompt
        context = review_source(request.prompt, "task_context")
        assert receipt in context and readback in context
        assert "Granted tools:" not in request.system
        if provider == "openai_responses":
            assert request.response_schema is not None
    assert "do not replay a write" in model.calls[3][1].system
    assert receipt in model.calls[3][1].prompt


def test_repeated_promise_fails_and_preserves_candidate_after_one_repair(actor, endpoint):
    second = "I will produce the comparison document next."
    model = Responses(
        action(endpoint, "final", output=PROMISE_ONLY),
        rejected(),
        action(endpoint, "final", output=second, artifacts=["unverified.md"]),
        rejected(second),
    )
    result = run_completion(actor, endpoint, model, exportable_workspace=True)
    assert result.status == "failed"
    assert result.error_code == "incomplete_worker_output"
    assert result.output == second
    assert result.artifact_paths == ()
    assert result.steps == 4 and result.tool_calls == 0
    assert len(model.calls) == 4


@pytest.mark.parametrize("unmet_status", ["missing", "unverified"])
def test_negative_review_discards_invented_excerpt_and_repairs_without_replaying_write(
    actor, endpoint, unmet_status
):
    receipt = "Saved competition.md revision 1"
    comparison = "Brand A numbers editions of 20; Stdout uses a unique seed per garment."
    final = "Competition comparison: " + comparison
    invented_excerpt = "PRIVATE-REVIEWER-BODY: This sentence was never in the supplied evidence."
    negative_review = verdict(
        check("Present the competition comparison", status=unmet_status, text=invented_excerpt),
        check("Save the document", kind="saved_result", source="task_context", text=receipt),
        status="not_delivered",
        summary="The candidate does not contain the requested competition comparison.",
    )
    model = Responses(
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "save comparison"}),
        action(endpoint, "final", output=PROMISE_ONLY),
        negative_review,
        action(endpoint, "final", output=final, artifacts=["competition.md"]),
        verdict(
            check("Present the competition comparison", text=comparison),
            check("Save the document", kind="saved_result", source="task_context", text=receipt),
        ),
    )
    writes = []
    events = []
    result = run_completion(
        actor,
        endpoint,
        model,
        write=True,
        handler=lambda *_args: (
            writes.append("saved") or {"receipt": receipt, "verified_content": comparison}
        ),
        checkpoint=events.append,
        exportable_workspace=True,
    )
    assert result.status == "succeeded" and result.output == final
    assert result.output != PROMISE_ONLY
    assert result.artifact_paths == ("competition.md",)
    assert len(writes) == result.tool_calls == 1
    assert result.steps == len(model.calls) == 5
    assert (result.input_tokens, result.output_tokens) == (100, 25)
    reviews = [event for event in events if event["event"] == "completion_review"]
    assert [event["status"] for event in reviews] == ["not_delivered", "complete"]
    assert [event["repair_used"] for event in reviews] == [False, True]
    assert reviews[0]["review_diagnostics"] == {
        "omitted_evidence_count": 1,
        "omitted_evidence": [
            {
                "check_index": 0,
                "evidence_index": 0,
                "kind": "deliverable",
                "status": unmet_status,
                "source": "candidate",
                "reason": "excerpt_not_found",
            }
        ],
    }
    assert reviews[1]["review_diagnostics"] == {}
    assert not any(event["event"] == "completion_review_rejected" for event in events)
    repair_prompt = model.calls[3][1].prompt
    assert "Present the competition comparison" in repair_prompt
    assert receipt in repair_prompt
    assert invented_excerpt not in repair_prompt
    assert invented_excerpt not in json.dumps(events)


def test_requested_plan_passes_semantic_review_without_future_tense_word_ban(actor, endpoint):
    plan = "I will compare suppliers on Monday, then shortlist two based on fabric and unit cost."
    model = Responses(plan, verdict(check("Provide the requested first-person plan", text=plan)))
    result = run_completion(
        actor,
        endpoint,
        model,
        tools=False,
        objective="Write a first-person plan to compare suppliers next week. Do not execute it.",
        additional_instructions="Only an inline plan is requested; do not save or contact anyone.",
    )
    assert result.status == "succeeded" and result.output == plan
    assert result.steps == 2 and result.tool_calls == 0


def test_invalid_reviewer_reply_cannot_turn_candidate_into_success(actor, endpoint):
    model = Responses(
        action(endpoint, "final", output=PROMISE_ONLY),
        "This looks done.",
        "This still looks done.",
    )
    result = run_completion(actor, endpoint, model)
    assert result.status == "failed"
    assert result.error_code == "invalid_completion_review"
    assert result.output == PROMISE_ONLY
    assert result.steps == len(model.calls) == 3 and result.tool_calls == 0


def test_review_format_correction_keeps_candidate_and_known_write_without_replaying(
    actor, endpoint
):
    candidate = "Competition comparison: Brand A numbers editions of 20. Saved competition.md."
    receipt = "Saved competition.md revision 1"
    model = Responses(
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "save comparison"}),
        action(endpoint, "final", output=candidate, artifacts=["competition.md"]),
        "The saved document looks complete.",
        verdict(
            check("Provide the comparison", text="Brand A numbers editions of 20"),
            check("Save the document", kind="saved_result", source="task_context", text=receipt),
        ),
    )
    writes = []
    events = []
    result = run_completion(
        actor,
        endpoint,
        model,
        write=True,
        handler=lambda *_args: writes.append("saved") or {"receipt": receipt},
        checkpoint=events.append,
        exportable_workspace=True,
    )
    assert result.status == "succeeded" and result.output == candidate
    assert result.artifact_paths == ("competition.md",)
    assert result.steps == len(model.calls) == 4
    assert len(writes) == result.tool_calls == 1
    assert (result.input_tokens, result.output_tokens) == (80, 20)
    assert [event["event"] for event in events].count("model_dispatch") == 4
    assert [event["event"] for event in events].count("model_complete") == 4
    assert [event["event"] for event in events].count("tool_dispatch") == 1
    assert [event["event"] for event in events].count("completion_review_rejected") == 1
    assert model.calls[2][1].prompt == model.calls[3][1].prompt
    assert "Review format correction" in model.calls[3][1].system
    for decision, request in model.calls[2:]:
        assert decision.request.required_capabilities == frozenset({"text"})
        assert not request.controller_mode
        assert receipt in request.prompt


def test_grounded_completion_ignores_extra_bad_quote_without_retrying_model_or_write(
    actor, endpoint
):
    candidate = "Competition comparison: Brand A numbers editions of 20; Stdout uses unique seeds."
    receipt = "Saved competition.md revision 1"
    delivered = check("Present the competition comparison", text=candidate)
    delivered["evidence"].append({"source": "candidate", "excerpt": "Invented second quote"})
    model = Responses(
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "save comparison"}),
        action(endpoint, "final", output=candidate, artifacts=["competition.md"]),
        verdict(
            delivered,
            check("Save the document", kind="saved_result", source="task_context", text=receipt),
        ),
    )
    writes = []
    events = []
    result = run_completion(
        actor,
        endpoint,
        model,
        write=True,
        handler=lambda *_args: writes.append("saved") or {"receipt": receipt},
        checkpoint=events.append,
        exportable_workspace=True,
    )
    assert result.status == "succeeded" and result.output == candidate
    assert result.artifact_paths == ("competition.md",)
    assert result.steps == len(model.calls) == 3
    assert len(writes) == result.tool_calls == 1
    reviews = [event for event in events if event["event"] == "completion_review"]
    assert len(reviews) == 1 and reviews[0]["status"] == "complete"
    assert reviews[0]["review_diagnostics"]["omitted_evidence_count"] == 1
    assert reviews[0]["review_diagnostics"]["omitted_evidence"][0]["evidence_index"] == 1
    assert not reviews[0]["repair_used"]
    assert not any(event["event"] == "completion_review_rejected" for event in events)
    assert "Invented second quote" not in json.dumps(events)


def test_review_cannot_claim_candidate_only_save_text_as_recorded_execution_evidence(
    actor, endpoint
):
    candidate = "Saved an independently verified competitor document."
    unsupported_review = verdict(
        check("Provide a competition document", text=candidate),
        check("Save the document", kind="saved_result", source="task_context", text=candidate),
    )
    model = Responses(
        action(endpoint, "final", output=candidate), unsupported_review, unsupported_review
    )
    events = []
    result = run_completion(actor, endpoint, model, checkpoint=events.append)
    assert result.status == "failed"
    assert result.error_code == "invalid_completion_review"
    assert result.output == candidate
    assert result.tool_calls == 0
    assert result.steps == len(model.calls) == 3
    rejected_events = [event for event in events if event["event"] == "completion_review_rejected"]
    assert [event["format_correction"] for event in rejected_events] == [True, False]
    expected_diagnostics = {
        "stage": "grounding",
        "reason": "unsupported_satisfied_check",
        "issues": [
            {
                "check_index": 1,
                "evidence_index": 0,
                "kind": "saved_result",
                "status": "satisfied",
                "source": "task_context",
                "reason": "excerpt_not_found",
            }
        ],
    }
    assert all(event["review_diagnostics"] == expected_diagnostics for event in rejected_events)
    assert candidate not in json.dumps(rejected_events)
    assert unsupported_review not in json.dumps(rejected_events)


def test_completion_review_cannot_exceed_remaining_steps_or_dispatch_a_last_step_tool(
    actor, endpoint
):
    model = Responses(action(endpoint, "final", output=PROMISE_ONLY))
    result = run_completion(actor, endpoint, model, max_steps=1)
    assert result.status == "failed" and result.error_code is not None
    assert result.output == PROMISE_ONLY
    assert result.steps == 1 and result.tool_calls == 0 and len(model.calls) == 1

    model = Responses(
        action(endpoint, "final", output=PROMISE_ONLY),
        rejected(),
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "last step"}),
    )
    dispatched = []
    result = run_completion(
        actor,
        endpoint,
        model,
        max_steps=3,
        handler=lambda *_args: dispatched.append(True),
    )
    assert result.status == "failed"
    assert result.error_code == "completion_review_unavailable"
    assert result.output == PROMISE_ONLY
    assert len(model.calls) == 2  # A repair needs both a candidate and its review step.
    assert result.tool_calls == 0 and not dispatched


def test_review_dispatch_uses_normal_checkpoint_and_preserves_known_write_on_budget_stop(
    actor, endpoint
):
    model = Responses(
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "save report"}),
        action(endpoint, "final", output=PROMISE_ONLY),
    )
    dispatches = []

    def checkpoint(event):
        if event["event"] == "model_dispatch":
            dispatches.append(event)
            if len(dispatches) == 3:
                raise WorkerCheckpointError("run_budget_exceeded")

    writes = []
    result = run_completion(
        actor,
        endpoint,
        model,
        write=True,
        handler=lambda *_args: writes.append("saved") or {"result": "saved"},
        checkpoint=checkpoint,
    )
    assert result.status == "failed" and result.error_code == "run_budget_exceeded"
    assert result.output == PROMISE_ONLY
    assert len(writes) == result.tool_calls == 1
    assert len(model.calls) == result.steps == 2


def test_unknown_write_stops_before_any_completion_review_or_repair(actor, endpoint):
    model = Responses(
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "save report"}),
    )
    writes = []

    def uncertain_write(*_args):
        writes.append(True)
        raise TimeoutError("provider outcome cannot be established")

    result = run_completion(actor, endpoint, model, write=True, handler=uncertain_write)
    assert result.status == "unknown" and result.error_code == "tool_execution_unknown"
    assert len(writes) == result.tool_calls == 1 and len(model.calls) == result.steps == 1
    assert result.provenance[0].status == "unknown"


def test_repair_cannot_widen_tool_authorization(actor, endpoint):
    model = Responses(
        action(endpoint, "final", output=PROMISE_ONLY),
        rejected(),
        action(endpoint, "tool", tool_id="not_granted.write", arguments={}),
    )
    result = run_completion(actor, endpoint, model)
    assert result.status == "failed" and result.error_code == "tool_not_authorized"
    assert result.tool_calls == 0


def test_cancel_during_completion_review_preserves_candidate_and_never_repairs(actor, endpoint):
    model = Responses(action(endpoint, "final", output=PROMISE_ONLY), rejected())
    generate = model.generate
    state = {"cancelled": False}

    def cancelling_review(decision, request):
        result = generate(decision, request)
        if "tool-free reviewer" in request.system:
            state["cancelled"] = True
        return result

    model.generate = cancelling_review
    result = run_completion(actor, endpoint, model, cancelled=lambda: state["cancelled"])
    assert result.status == "cancelled" and result.error_code == "cancelled"
    assert result.output == PROMISE_ONLY
    assert result.steps == len(model.calls) == 2
    assert result.tool_calls == 0
