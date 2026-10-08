"""Real planner protocol exercised through synthetic HTTP, never paid endpoints."""

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from simon.adapters.model_endpoints import ModelEndpointError
from simon.domain.model_routing import ModelEndpoint
from simon.domain.native_intake import StaffingProposal
from simon.services.intake_planner import (
    MAX_PROPOSAL_BYTES,
    IntakePlanner,
    IntakePlanningError,
)


def endpoint(**changes: Any) -> ModelEndpoint:
    return ModelEndpoint(
        **{
            "id": "planning",
            "provider": "openai_compatible",
            "model": "configured-model",
            "base_url": "http://127.0.0.1:19099/v1",
            "local": True,
            "context_window_tokens": 128000,
            "max_output_tokens": 8192,
            **changes,
        }
    )


def proposal_document() -> dict[str, Any]:
    return {
        "summary": "Establish a source-backed brand charter before selecting a launch collection.",
        "next_milestone": "Owner review of the unresolved brand direction.",
        "findings": [
            {
                "kind": "assumption",
                "statement": "The initial launch scope remains provisional.",
                "evidence": [],
            }
        ],
        "questions": [
            {
                "key": "launch-scope",
                "question": "Which launch outcome matters most?",
                "why": "Avoid adding design roles before the intended deliverable is known.",
                "blocking": True,
            }
        ],
        "roles": [],
        "tasks": [
            {
                "key": "confirm-outcome",
                "title": "Confirm the initial launch outcome",
                "description": "Review the unresolved direction with the owner.",
                "acceptance": "One desired outcome and a measurable success criterion are saved.",
                "assignment": "human",
                "role_key": None,
                "review_role_key": None,
                "existing_task_id": None,
            }
        ],
    }


def wire_result(
    provider: str,
    text: str,
    *,
    input_tokens: int | None = 101,
    output_tokens: int | None = 31,
    truncated: bool = False,
    refused: bool = False,
) -> dict[str, Any]:
    if provider == "openai_responses":
        return {
            "status": "incomplete" if truncated else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "refusal", "refusal": "private refusal"}
                        if refused
                        else {"type": "output_text", "text": text}
                    ],
                }
            ],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    if provider == "openai_compatible":
        return {
            "choices": [
                {
                    "finish_reason": "length" if truncated else "stop",
                    "message": {"content": text},
                }
            ],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
        }
    if provider == "anthropic":
        return {
            "stop_reason": "max_tokens" if truncated else "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    return {
        "candidates": [
            {
                "finishReason": "MAX_TOKENS" if truncated else "STOP",
                "content": {"parts": [{"text": text}]},
            }
        ],
        "usageMetadata": {"promptTokenCount": input_tokens, "candidatesTokenCount": output_tokens},
    }


@pytest.mark.parametrize(
    ("provider", "path"),
    [
        ("openai_compatible", "/v1/chat/completions"),
        ("openai_responses", "/v1/responses"),
        ("anthropic", "/v1/messages"),
        ("gemini", "/v1/models/configured-model:generateContent"),
    ],
)
def test_true_generation_and_fresh_review_work_across_configured_providers(
    provider: str, path: str
) -> None:
    requests: list[httpx.Request] = []
    responses = [proposal_document(), {"approved": True, "issues": []}]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=wire_result(provider, json.dumps(responses.pop(0))))

    planner = IntakePlanner(
        [
            endpoint(
                provider=provider, input_cost_per_million_usd=0.125, output_cost_per_million_usd=2
            )
        ],
        environ={},
        transport=httpx.MockTransport(respond),
    )
    context = {"objective": "Launch a clothing brand", "sources": [{"text": "UNTRUSTED SOURCE"}]}
    prepared = planner.prepare("planning", False, context)
    context["objective"] = "CHANGED AFTER PREPARATION"
    prepared.context["objective"] = "CHANGED EXPOSED COPY"
    proposal, generated = planner.generate(prepared)
    review, reviewed = planner.review(prepared, proposal)

    assert review.approved
    assert proposal.questions[0].blocking
    assert len(requests) == 2
    assert all(request.url.path == path for request in requests)
    first, second = [json.loads(request.content) for request in requests]
    assert "UNTRUSTED SOURCE" in json.dumps(first)
    assert "UNTRUSTED SOURCE" in json.dumps(second)
    assert "Independently review" in json.dumps(second)
    assert "Establish a source-backed brand charter" in json.dumps(second)
    assert "CHANGED" not in json.dumps(first) + json.dumps(second)
    assert "tools" not in first and "tools" not in second
    assert "response_format" not in first
    assert "authorization" not in requests[0].headers
    assert generated.input_tokens == reviewed.input_tokens == 101
    assert generated.output_tokens == reviewed.output_tokens == 31
    assert prepared.reservation_microusd >= 150


def test_no_default_or_fake_model_and_preparation_never_probes_network() -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Readiness must not call a provider")

    planner = IntakePlanner([], environ={}, transport=httpx.MockTransport(forbidden))
    with pytest.raises(IntakePlanningError, match="endpoint_unavailable") as failure:
        planner.prepare("missing", False, {})
    assert not failure.value.may_have_been_dispatched


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"capabilities": frozenset({"image"})},
        {"api_key_env": "DO_NOT_EXPOSE_CREDENTIAL_NAME"},
        {"input_cost_per_million_usd": 1},
        {"output_cost_per_million_usd": 1},
        {
            "local": False,
            "base_url": "https://approved.example/v1",
            "api_key_env": "PRESENT_KEY",
        },
    ],
)
def test_unready_endpoints_do_not_dispatch_or_disclose_secret_configuration(
    changes: dict[str, Any],
) -> None:
    planner = IntakePlanner([endpoint(**changes)], environ={"PRESENT_KEY": "private-value"})
    with pytest.raises(IntakePlanningError, match="endpoint_not_ready") as failure:
        planner.prepare("planning", True, {})
    assert "private-value" not in str(failure.value)
    assert "DO_NOT_EXPOSE_CREDENTIAL_NAME" not in str(failure.value)


def test_cloud_permission_is_separate_from_admin_configuration_and_price_readiness() -> None:
    planner = IntakePlanner(
        [
            endpoint(
                local=False,
                base_url="https://approved.example/v1",
                api_key_env="KEY",
                input_cost_per_million_usd=0.3,
                output_cost_per_million_usd=0.7,
            )
        ],
        environ={"KEY": "enrolled-secret"},
    )
    with pytest.raises(IntakePlanningError, match="cloud_not_authorized"):
        planner.prepare("planning", False, {})
    assert planner.prepare("planning", True, {}).reservation_microusd > 0


def test_ordinary_small_context_model_has_actual_bounds_and_two_call_reservation() -> None:
    planner = IntakePlanner(
        [
            endpoint(
                context_window_tokens=32768,
                max_output_tokens=4096,
                input_cost_per_million_usd=0.7,
                output_cost_per_million_usd=1.3,
            )
        ],
        environ={},
    )
    prepared = planner.prepare("planning", False, {"objective": "One concise research memo"})
    first, second = prepared.decision.request, prepared.review_decision.request
    assert first.input_tokens < 32000 and second.input_tokens < 32000
    assert first.input_tokens + first.output_tokens <= 32768
    assert second.input_tokens + second.output_tokens <= 32768
    assert first.output_tokens == 4096
    cost = Decimal(first.input_tokens + second.input_tokens) * Decimal("0.7") + Decimal(
        first.output_tokens + second.output_tokens
    ) * Decimal("1.3")
    assert Decimal(prepared.reservation_microusd) >= cost
    # Each independently settled call rounds up its own reservation.
    assert Decimal(prepared.reservation_microusd) < cost + 2
    assert prepared.reservation_microusd == (
        prepared.generation_reservation_microusd + prepared.review_reservation_microusd
    )


@pytest.mark.parametrize(
    ("context", "changes", "code"),
    [
        ({"text": "x" * 65000}, {}, "context_limit_exceeded"),
        ({"text": "\ud800"}, {}, "invalid_context"),
        ({"bad": float("nan")}, {}, "invalid_context"),
        ({"bad": object()}, {}, "invalid_context"),
        (
            {},
            {"context_window_tokens": 1000, "max_output_tokens": 100},
            "endpoint_context_unavailable",
        ),
    ],
)
def test_preparation_rejects_unrepresentable_or_overlong_context_without_truncation(
    context: dict[str, Any], changes: dict[str, Any], code: str
) -> None:
    planner = IntakePlanner([endpoint(**changes)], environ={})
    with pytest.raises(IntakePlanningError, match=code) as failure:
        planner.prepare("planning", False, context)
    assert not failure.value.may_have_been_dispatched


@pytest.mark.parametrize(
    "text",
    [
        "secret-provider-body that is not JSON",
        '```json\n{"summary":"no"}\n```',
        '{"summary":"one","summary":"two","next_milestone":"bad"}',
        '{"summary":"bad","next_milestone":"bad","can_manage_team":true}',
        '{"summary":NaN,"next_milestone":"bad"}',
        json.dumps(
            {
                **proposal_document(),
                "questions": [{"key": "x", "question": "x", "why": "x", "blocking": "true"}],
            }
        ),
    ],
)
def test_invalid_schema_preserves_usage_without_raw_error_body_or_retry(text: str) -> None:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=wire_result("openai_compatible", text))

    planner = IntakePlanner(
        [endpoint(input_cost_per_million_usd=1, output_cost_per_million_usd=1)],
        environ={},
        transport=httpx.MockTransport(respond),
    )
    prepared = planner.prepare("planning", False, {})
    with pytest.raises(IntakePlanningError, match="invalid_proposal_schema") as failure:
        planner.generate(prepared)
    error = failure.value
    assert error.result is not None and error.may_have_been_dispatched
    assert error.result.input_tokens == 101 and error.result.output_tokens == 31
    assert "secret-provider-body" not in str(error)
    assert calls == 1


@pytest.mark.parametrize(
    ("flags", "code"),
    [({"refused": True}, "model_refused"), ({"truncated": True}, "model_output_truncated")],
)
def test_terminal_provider_outcomes_keep_known_usage(flags: dict[str, bool], code: str) -> None:
    planner = IntakePlanner(
        [endpoint(provider="openai_responses")],
        environ={},
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=wire_result("openai_responses", "partial", **flags))
        ),
    )
    prepared = planner.prepare("planning", False, {})
    with pytest.raises(IntakePlanningError, match=code) as failure:
        planner.generate(prepared)
    assert failure.value.result is not None
    assert failure.value.result.input_tokens == 101


def test_reviewer_rejection_is_a_saved_result_not_a_silent_repair() -> None:
    planner = IntakePlanner(
        [endpoint()],
        environ={},
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=wire_result(
                    "openai_compatible",
                    '{"approved":false,"issues":'
                    '["The proposed specialist duplicates an active role."]}',
                ),
            )
        ),
    )
    prepared = planner.prepare("planning", False, {})
    review, result = planner.review(prepared, StaffingProposal.model_validate(proposal_document()))
    assert not review.approved
    assert review.issues == ("The proposed specialist duplicates an active role.",)
    assert result.input_tokens == 101


@pytest.mark.parametrize("response", ["timeout", "http-error", "invalid-body"])
def test_uncertain_dispatch_never_retries_or_falls_back(response: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if response == "timeout":
            raise httpx.ReadTimeout("secret-error-details", request=request)
        if response == "http-error":
            return httpx.Response(503, text="secret-error-details")
        return httpx.Response(200, text="secret-error-details")

    planner = IntakePlanner([endpoint()], environ={}, transport=httpx.MockTransport(respond))
    with pytest.raises(ModelEndpointError) as failure:
        planner.generate(planner.prepare("planning", False, {}))
    assert failure.value.may_have_been_dispatched
    assert "secret-error-details" not in str(failure.value)
    assert calls == 1


def test_output_size_limit_preserves_charge_and_never_passes_oversize_to_reviewer() -> None:
    planner = IntakePlanner(
        [endpoint()],
        environ={},
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=wire_result("openai_compatible", " " * (MAX_PROPOSAL_BYTES + 1))
            )
        ),
    )
    with pytest.raises(IntakePlanningError, match="model_output_too_large") as failure:
        planner.generate(planner.prepare("planning", False, {}))
    assert failure.value.result is not None


def test_review_checks_its_exact_reserved_context_before_network() -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("No call after violated context reservation")

    planner = IntakePlanner([endpoint()], environ={}, transport=httpx.MockTransport(forbidden))
    prepared = planner.prepare("planning", False, {})
    bounded = replace(
        prepared,
        review_decision=prepared.review_decision.model_copy(
            update={
                "request": prepared.review_decision.request.model_copy(update={"input_tokens": 1})
            }
        ),
    )
    with pytest.raises(IntakePlanningError, match="review_context_limit_exceeded"):
        planner.review(bounded, StaffingProposal.model_validate(proposal_document()))


def test_reuse_and_exact_evidence_survive_provider_neutral_schema() -> None:
    source_id, agent_id = uuid4(), uuid4()
    document = proposal_document()
    document["findings"] = [
        {
            "kind": "fact",
            "statement": "The launch is small.",
            "evidence": [{"source_id": str(source_id), "quote": "Start with one design."}],
        }
    ]
    document["roles"] = [
        {
            "role_key": "researcher",
            "action": "reuse",
            "name": "Product researcher",
            "instructions": "Research product choices.",
            "success_criteria": "Evidence-backed recommendations.",
            "rationale": "Existing role covers the immediate milestone.",
            "agent_id": str(agent_id),
            "need": "reuse",
            "reuse_assessment": "No additional specialist is needed.",
        }
    ]
    planner = IntakePlanner(
        [endpoint()],
        environ={},
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=wire_result("openai_compatible", json.dumps(document))
            )
        ),
    )
    proposal, _ = planner.generate(planner.prepare("planning", False, {}))
    assert proposal.roles[0].agent_id == agent_id
    assert proposal.findings[0].evidence[0].source_id == source_id
