"""Contracted controllers reserve useful repair work within the original limits."""

import json

import pytest
from jsonschema import Draft202012Validator

from tests.unit.test_agent_worker import Responses, execute, make_worker, profile
from tests.unit.test_agent_worker import actor as actor
from tests.unit.test_agent_worker import endpoint as endpoint
from tests.unit.test_agent_worker_completion import action, run_completion
from tests.unit.test_worker_completion import check, verdict
from tests.unit.test_worker_context import AdaptiveModel


def assert_final_only(request, endpoint):
    assert "FINAL RESPONSE REQUIRED NOW" in request.system
    if endpoint.provider == "openai_responses":
        validator = Draft202012Validator(request.response_schema)
        validator.validate(json.loads(action(endpoint, "final", output="Actual answer.")))
        assert not validator.is_valid(
            json.loads(action(endpoint, "tool", tool_id="lookup", arguments={"query": "late"}))
        )
        assert not validator.is_valid(
            {
                "action": {
                    "type": "evidence",
                    "invocation_id": "00000000-0000-0000-0000-000000000001",
                    "pointer": "/output/text",
                    "offset": 0,
                    "limit": 100,
                }
            }
        )
        assert request.controller_mode


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses"])
def test_twenty_action_research_budget_keeps_one_targeted_repair_and_final_review(
    actor, endpoint, provider
):
    endpoint = endpoint.model_copy(update={"provider": provider})
    candidate = "Manufacturing memo. " + "Documented cost drivers and explicit assumptions. " * 280
    repaired = candidate + "\nThe remaining supplier fact is now source-backed."
    receipt = "The supplier confirms a sample is required before a production order."
    dispatched = []
    events = []

    def handler(_definition, arguments, context):
        dispatched.append((arguments["query"], context.invocation_id))
        return {"text": receipt if arguments["query"] == "targeted missing fact" else "Read fact."}

    def respond(step, request):
        if step <= 19:
            assert "FINAL RESPONSE REQUIRED NOW" not in request.system
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": f"source-{step}"})
        if step == 20:
            assert_final_only(request, endpoint)
            return action(endpoint, "final", output=candidate)
        if step == 21:
            assert "tool-free reviewer" in request.system
            return verdict(
                check("Provide the memo", text="Manufacturing memo."),
                check(
                    "Verify the remaining supplier fact", kind="source_support", status="unverified"
                ),
                status="partial",
            )
        if step == 22:
            assert "FINAL RESPONSE REQUIRED NOW" not in request.system
            assert candidate in request.prompt
            return action(
                endpoint, "tool", tool_id="lookup", arguments={"query": "targeted missing fact"}
            )
        if step == 23:
            assert_final_only(request, endpoint)
            assert receipt in request.prompt
            return action(endpoint, "final", output=repaired)
        assert step == 24
        return verdict(
            check("Provide the memo", text="Manufacturing memo."),
            check(
                "Verify the remaining supplier fact",
                kind="source_support",
                source="task_context",
                text=receipt,
            ),
        )

    model = AdaptiveModel(respond)
    result = run_completion(
        actor, endpoint, model, max_steps=24, handler=handler, checkpoint=events.append
    )
    assert result.status == "succeeded", result
    assert result.output == repaired
    assert result.steps == len(model.calls) == 24
    assert result.tool_calls == len(dispatched) == len(set(dispatched)) == 20
    assert len(result.provenance) == 20
    dispatches = [event for event in events if event["event"] == "model_dispatch"]
    assert [event["step"] for event in dispatches if event.get("final_only")] == [20, 23]
    assert dispatches[19]["completion_steps_reserved"] == 4
    assert dispatches[22]["completion_steps_reserved"] == 1
    assert (result.input_tokens, result.output_tokens) == (240, 240)


@pytest.mark.parametrize("maximum", [3, 8])
def test_small_profile_keeps_discovery_allowance_but_last_candidate_is_final_only(
    actor, endpoint, maximum
):
    endpoint = endpoint.model_copy(update={"provider": "openai_responses"})
    final = "An evidence-based answer."

    def respond(step, request):
        if step <= maximum - 2:
            assert "FINAL RESPONSE REQUIRED NOW" not in request.system
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": f"fact-{step}"})
        if step == maximum - 1:
            assert_final_only(request, endpoint)
            return action(endpoint, "final", output=final)
        return verdict(check("Answer the request", text=final))

    result = run_completion(actor, endpoint, AdaptiveModel(respond), max_steps=maximum)
    assert result.status == "succeeded", result
    assert result.steps == maximum and result.tool_calls == maximum - 2


@pytest.mark.parametrize("provider", ["openai_compatible", "openai_responses"])
def test_ignored_final_only_constraint_never_dispatches_a_last_step_action(
    actor, endpoint, provider
):
    endpoint = endpoint.model_copy(update={"provider": provider})
    model = Responses(
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "first"}),
        action(endpoint, "tool", tool_id="lookup", arguments={"query": "forbidden last step"}),
    )
    dispatched = []
    result = run_completion(
        actor,
        endpoint,
        model,
        max_steps=3,
        handler=lambda _definition, arguments, _context: dispatched.append(arguments["query"]),
    )
    assert result.status == "failed"
    assert result.error_code in {"invalid_controller_response", "worker_step_limit"}
    assert dispatched == ["first"] and result.tool_calls == 1
    assert_final_only(model.calls[1][1], endpoint)


def test_legacy_task_without_contract_does_not_reserve_completion_steps(actor, endpoint):
    def respond(step, request):
        assert "FINAL RESPONSE REQUIRED NOW" not in request.system
        assert "additional model steps are reserved" not in request.system
        return (
            action(endpoint, "tool", tool_id="lookup", arguments={"query": f"fact-{step}"})
            if step <= 8
            else action(endpoint, "final", output="Legacy answer.")
        )

    result = execute(
        make_worker(AdaptiveModel(respond)),
        actor,
        endpoint,
        tools=True,
        agent=profile(tools=True, max_steps=9),
    )
    assert result.status == "succeeded" and result.steps == 9 and result.tool_calls == 8


def test_reserved_repair_pages_missing_source_proof_without_replaying_external_reads(
    actor, endpoint
):
    endpoint = endpoint.model_copy(update={"provider": "openai_responses"})
    source_fact = "A sample costs 45 USD according to the inspected supplier page."
    candidate = "A useful but incompletely sourced manufacturing memo. " + "Cost discussion. " * 850
    final = candidate + "\nSource: https://supplier.example/prices. " + source_fact
    invocation_ids = []
    events = []

    def handler(_definition, _arguments, context):
        invocation_ids.append(str(context.invocation_id))
        return {
            "url": "https://supplier.example/prices",
            "text": "x" * 3000 + source_fact + "z" * 4000,
            "text_truncated": False,
        }

    def respond(step, request):
        if step <= 19:
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": f"source-{step}"})
        if step == 20:
            assert_final_only(request, endpoint)
            return action(endpoint, "final", output=candidate)
        if step == 21:
            assert source_fact not in request.prompt
            return verdict(
                check("Provide the memo", text="manufacturing memo"),
                check(
                    "Verify sample price from the source",
                    kind="source_support",
                    status="unverified",
                ),
                status="partial",
            )
        if step == 22:
            return json.dumps(
                {
                    "action": {
                        "type": "evidence",
                        "invocation_id": invocation_ids[0],
                        "pointer": "/output/text",
                        "offset": 3000,
                        "limit": len(source_fact),
                    }
                }
            )
        if step == 23:
            assert_final_only(request, endpoint)
            assert source_fact in request.prompt
            return action(endpoint, "final", output=final)
        assert step == 24
        assert "Requested evidence page (untrusted):" in request.prompt
        return verdict(
            check("Provide the memo", text="manufacturing memo"),
            check(
                "Verify sample price from the source",
                kind="source_support",
                source="task_context",
                text=source_fact,
            ),
        )

    model = AdaptiveModel(respond)
    result = run_completion(
        actor, endpoint, model, max_steps=24, handler=handler, checkpoint=events.append
    )
    assert result.status == "succeeded", result
    assert result.output == final and result.steps == 24
    assert result.tool_calls == len(invocation_ids) == len(set(invocation_ids)) == 19
    assert len(result.provenance) == 19
    assert [event["step"] for event in events if event["event"] == "evidence_read"] == [22]
    assert any(event.get("context_compacted") for event in events)
    assert all(len(request.system) + len(request.prompt) <= 60000 for request in model.calls)
