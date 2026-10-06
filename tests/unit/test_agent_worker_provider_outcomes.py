"""Real provider parsing preserves known terminal outcomes through worker accounting."""

import json

import httpx
import pytest

from simon.adapters.model_endpoints import ModelEndpointClient
from tests.unit.test_agent_worker import actor as actor
from tests.unit.test_agent_worker import endpoint as endpoint
from tests.unit.test_agent_worker import execute, make_worker
from tests.unit.test_agent_worker_completion import action, run_completion


def terminal_document(outcome):
    usage = {
        "input_tokens": 1200,
        "output_tokens": 2000,
        "output_tokens_details": {"reasoning_tokens": 2000},
    }
    if outcome == "max_output_tokens":
        return {
            "status": "incomplete",
            "incomplete_details": {"reason": outcome},
            "output": [{"type": "reasoning", "summary": []}],
            "usage": usage,
        }
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "refusal", "refusal": "PRIVATE provider refusal body"}],
            }
        ],
        "usage": usage,
    }


def controller_document(arguments):
    return {
        "status": "completed",
        "output": [{"type": "function_call", "name": "simon_controller", "arguments": arguments}],
        "usage": {
            "input_tokens": 101,
            "output_tokens": 23,
            "output_tokens_details": {"reasoning_tokens": 7},
        },
    }


@pytest.mark.parametrize("outcome", ["max_output_tokens", "refusal"])
@pytest.mark.parametrize("phase", ["ordinary", "final_only", "completion_review"])
def test_known_provider_terminal_outcome_stops_worker_without_repair_or_replay(
    actor, endpoint, outcome, phase
):
    endpoint = endpoint.model_copy(update={"provider": "openai_responses"})
    candidate = "Competition comparison: numbered editions differ from unique seeded garments."
    documents = []
    if phase != "ordinary":
        documents.append(
            controller_document(
                action(endpoint, "tool", tool_id="lookup", arguments={"query": "source"})
            )
        )
    if phase == "completion_review":
        documents.append(
            controller_document(
                action(endpoint, "final", output=candidate, artifacts=["competition.md"])
            )
        )
    documents.append(terminal_document(outcome))
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= len(documents), "A known terminal outcome must never be retried"
        return httpx.Response(200, json=documents[len(requests) - 1])

    model = ModelEndpointClient((endpoint,), environ={}, transport=httpx.MockTransport(handle))
    events = []
    tool_calls = []

    def handler(definition, arguments, context):
        tool_calls.append(context.invocation_id)
        return {"receipt": "Saved competition.md revision 1", "source": "Numbered editions."}

    if phase == "ordinary":
        result = execute(
            make_worker(model, handler=handler), actor, endpoint, checkpoint=events.append
        )
    else:
        result = run_completion(
            actor,
            endpoint,
            model,
            handler=handler,
            write=phase == "completion_review",
            max_steps=3 if phase == "final_only" else 8,
            exportable_workspace=phase == "completion_review",
            checkpoint=events.append,
        )

    expected_calls = {"ordinary": 1, "final_only": 2, "completion_review": 3}[phase]
    assert len(requests) == result.steps == expected_calls
    assert result.status == "failed"
    assert result.error_code == (
        "model_output_truncated" if outcome == "max_output_tokens" else "model_refused"
    )
    assert (result.input_tokens, result.output_tokens) == (
        1200 + 101 * (expected_calls - 1),
        2000 + 23 * (expected_calls - 1),
    )
    assert result.tool_calls == len(tool_calls) == (0 if phase == "ordinary" else 1)
    assert result.artifact_paths == ()
    assert result.output == (candidate if phase == "completion_review" else "")
    assert all(item.status == "succeeded" for item in result.provenance)
    assert len(result.provenance) == len(tool_calls)
    assert [event["event"] for event in events].count("tool_dispatch") == len(tool_calls)
    completed = [event for event in events if event["event"] == "model_complete"]
    assert len(completed) == expected_calls
    assert completed[-1]["response_reason"] == outcome
    assert completed[-1]["truncated"] is (outcome == "max_output_tokens")
    assert completed[-1]["refused"] is (outcome == "refusal")
    assert completed[-1]["reasoning_tokens"] == 2000
    assert completed[-1]["output_chars"] == 0
    assert completed[-1]["input_tokens"] == 1200
    assert completed[-1]["output_tokens"] == 2000
    assert not any(event["event"] == "completion_review" for event in events)
    assert not any(event.get("format_correction") is True for event in events)
    assert "PRIVATE" not in json.dumps(events) + result.model_dump_json()

    final_request = requests[-1]
    if phase == "final_only":
        assert final_request["tool_choice"] == {"type": "function", "name": "simon_controller"}
        assert final_request["parallel_tool_calls"] is False
        branches = final_request["tools"][0]["parameters"]["properties"]["action"]["anyOf"]
        assert [branch["properties"]["type"]["enum"] for branch in branches] == [["final"]]
    else:
        assert "tools" not in final_request and "tool_choice" not in final_request
    if phase == "completion_review":
        assert completed[-1]["phase"] == "completion_review"
        assert final_request["text"]["format"]["type"] == "json_schema"
        assert len(tool_calls) == len(set(tool_calls)) == 1
