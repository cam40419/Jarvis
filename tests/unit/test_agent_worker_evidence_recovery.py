"""Internal evidence corrections never replay a provider read or completed write."""

import json
from uuid import uuid4

import pytest

from simon.services.agent_worker import WorkerCheckpointError
from simon.services.worker_context import EvidenceReadError, ToolEvidenceBuffer
from tests.unit.test_agent_worker import actor as actor
from tests.unit.test_agent_worker import endpoint as endpoint
from tests.unit.test_agent_worker import execute, make_worker, profile, tool
from tests.unit.test_agent_worker_completion import action, run_completion
from tests.unit.test_worker_context import AdaptiveModel


def evidence_action(endpoint, invocation, **changes):
    value = {
        "type": "evidence",
        "invocation_id": invocation,
        "pointer": "/output/text",
        "offset": 0,
        "limit": 80,
        **changes,
    }
    return json.dumps({"action": value} if endpoint.provider == "openai_responses" else value)


@pytest.mark.parametrize("provider", ["openai_responses", "openai_compatible"])
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("mistake", ["pointer", "invocation"])
def test_correctable_evidence_mistake_returns_safe_feedback_then_exact_local_page(
    actor, endpoint, provider, write, mistake
):
    endpoint = endpoint.model_copy(update={"provider": provider})
    invocations = []
    events = []
    unknown = str(uuid4())
    source = "The captured source confirms a sample is required."

    def handler(_tool, _arguments, context):
        invocations.append(str(context.invocation_id))
        return {"text": source}

    def respond(step, request):
        if step == 1:
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": "source"})
        if step == 2:
            return evidence_action(
                endpoint,
                invocations[0] if mistake == "pointer" else unknown,
                pointer="/output/PRIVATE-GUESSED-PATH" if mistake == "pointer" else "/output/text",
            )
        if step == 3:
            assert '"status":"not_read"' in request.prompt
            assert (
                '"reason":"invalid_pointer"'
                if mistake == "pointer"
                else '"reason":"unavailable_evidence"'
            ) in request.prompt
            assert '"remaining_attempts":2' in request.prompt
            assert "PRIVATE-GUESSED-PATH" not in request.prompt and unknown not in request.prompt
            return evidence_action(endpoint, invocations[0])
        assert step == 4
        assert source in request.prompt and '"eof":true' in request.prompt
        return action(endpoint, "final", output="The source confirms a sample is required.")

    model = AdaptiveModel(respond)
    result = execute(
        make_worker(
            model,
            handler=handler,
            definition=tool(side_effect=write, action_policy="write" if write else "read"),
        ),
        actor,
        endpoint,
        tools=True,
        agent=profile(tools=True, max_action="write" if write else "read"),
        checkpoint=events.append,
    )
    assert result.status == "succeeded", result
    assert result.steps == len(model.calls) == 4
    assert result.tool_calls == len(invocations) == len(result.provenance) == 1
    assert result.provenance[0].status == "succeeded"
    rejected = [event for event in events if event["event"] == "evidence_read_rejected"]
    assert len(rejected) == 1 and rejected[0]["recoverable"]
    assert rejected[0]["attempt"] == 1 and rejected[0]["max_attempts"] == 3
    assert rejected[0]["offset"] == 0 and rejected[0]["limit"] == 80
    assert "PRIVATE" not in json.dumps(events) and unknown not in json.dumps(events)
    assert len([event for event in events if event["event"] == "tool_dispatch"]) == 1
    assert len([event for event in events if event["event"] == "evidence_read"]) == 1


@pytest.mark.parametrize("provider", ["openai_responses", "openai_compatible"])
def test_evidence_page_overshoot_returns_eof_without_refetching_original_source(
    actor, endpoint, provider
):
    endpoint = endpoint.model_copy(update={"provider": provider})
    invocations = []
    events = []
    source = "Captured text."

    def respond(step, request):
        if step == 1:
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": "read"})
        if step == 2:
            return evidence_action(endpoint, invocations[0], offset=6000)
        assert step == 3
        assert '"status":"eof"' in request.prompt and '"text":""' in request.prompt
        assert '"next_offset":null' in request.prompt
        assert "Offsets refer to this captured value, NOT the provider document" in request.system
        return action(
            endpoint, "final", output="Used the captured source; no additional page exists."
        )

    model = AdaptiveModel(respond)
    result = execute(
        make_worker(
            model,
            handler=lambda _tool, _arguments, context: (
                invocations.append(str(context.invocation_id)) or {"text": source}
            ),
        ),
        actor,
        endpoint,
        tools=True,
        checkpoint=events.append,
    )
    assert result.status == "succeeded" and result.tool_calls == len(invocations) == 1
    assert result.steps == len(model.calls) == 3
    page = next(event for event in events if event["event"] == "evidence_read")
    assert page["eof"] and page["status"] == "eof"
    assert page["offset"] == page["total_chars"] == len(source)
    assert page["requested_offset"] == 6000 and page["next_offset"] is None


def test_three_rejected_evidence_attempts_stop_within_original_step_budget(actor, endpoint):
    invocations = []
    events = []

    def respond(step, _request):
        if step == 1:
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": "read"})
        return evidence_action(endpoint, invocations[0], pointer="/output/missing")

    model = AdaptiveModel(respond)
    result = execute(
        make_worker(
            model,
            handler=lambda _tool, _arguments, context: (
                invocations.append(str(context.invocation_id)) or {"text": "Known source."}
            ),
        ),
        actor,
        endpoint,
        tools=True,
        agent=profile(tools=True, max_steps=8),
        checkpoint=events.append,
    )
    assert result.status == "failed" and result.error_code == "invalid_evidence_request"
    assert result.steps == len(model.calls) == 4
    assert result.tool_calls == len(invocations) == 1
    rejected = [event for event in events if event["event"] == "evidence_read_rejected"]
    assert [event["attempt"] for event in rejected] == [1, 2, 3]
    assert [event["recoverable"] for event in rejected] == [True, True, False]


@pytest.mark.parametrize("problem", ["corruption", "authorization"])
def test_integrity_and_checkpoint_authority_failures_still_stop_before_followup(
    actor, endpoint, monkeypatch, problem
):
    invocations = []
    events = []
    if problem == "corruption":

        def corrupt(_self, _arguments):
            raise EvidenceReadError("invalid_evidence")

        monkeypatch.setattr(ToolEvidenceBuffer, "read", corrupt)

    def checkpoint(event):
        events.append(event)
        if problem == "authorization" and event["event"] == "evidence_read_rejected":
            raise WorkerCheckpointError("assignment_no_longer_available")

    def respond(step, _request):
        if step == 1:
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": "read"})
        return evidence_action(endpoint, invocations[0], pointer="/output/missing")

    model = AdaptiveModel(respond)
    result = execute(
        make_worker(
            model,
            handler=lambda _tool, _arguments, context: (
                invocations.append(str(context.invocation_id)) or {"text": "Known source."}
            ),
        ),
        actor,
        endpoint,
        tools=True,
        checkpoint=checkpoint,
    )
    assert result.status == "failed"
    assert result.error_code == (
        "invalid_evidence_request" if problem == "corruption" else "assignment_no_longer_available"
    )
    assert result.steps == len(model.calls) == 2
    assert result.tool_calls == len(invocations) == 1
    assert result.provenance[0].status == "succeeded"


@pytest.mark.parametrize("provider", ["openai_responses", "openai_compatible"])
def test_correctable_feedback_cannot_bypass_final_only_completion_reservation(
    actor, endpoint, provider
):
    endpoint = endpoint.model_copy(update={"provider": provider})
    invocations = []

    def respond(step, request):
        if step == 1:
            return action(endpoint, "tool", tool_id="lookup", arguments={"query": "read"})
        if step == 3:
            assert "FINAL RESPONSE REQUIRED NOW" in request.system
        return evidence_action(endpoint, invocations[0], pointer="/output/missing")

    model = AdaptiveModel(respond)
    result = run_completion(
        actor,
        endpoint,
        model,
        max_steps=4,
        handler=lambda _tool, _arguments, context: (
            invocations.append(str(context.invocation_id)) or {"text": "Known source."}
        ),
    )
    assert result.status == "failed"
    assert result.error_code in {"invalid_controller_response", "worker_step_limit"}
    assert result.steps == len(model.calls) == 3
    assert result.tool_calls == len(invocations) == 1
