"""Bounded native decisions through synthetic providers, without tools or paid calls."""

import json
from dataclasses import replace
from decimal import ROUND_CEILING, Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.model_endpoints import ModelEndpointError
from simon.domain.model_routing import TextGenerationResult
from simon.domain.native_execution import bounded_json
from simon.domain.native_models import ProjectModel
from simon.services.project_models import ResolvedProjectModel
from simon.services.workflow_executor import (
    MAX_ACTION_BYTES,
    WorkflowAction,
    WorkflowExecutor,
    WorkflowPlanningError,
)
from tests.unit.test_intake_planner import endpoint, wire_result


def binding(**changes: Any) -> ResolvedProjectModel:
    return ResolvedProjectModel(
        model=ProjectModel(
            workspace_id=uuid4(),
            project_id=uuid4(),
            template_id="planning",
            label="Project planning model",
            created_by=uuid4(),
        ),
        endpoint=endpoint(**changes),
        fingerprint="a" * 64,
    )


def action_document(kind: str = "draft", **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "draft": {"output": "Candidate launch brief for owner review."},
        "question": {"question": "Which launch deadline is approved?"},
        "wait": {"wait_seconds": 300},
        "reference": {"operation": "echo", "text": "Checkpoint example"},
        "delegate": {
            "children": [
                {
                    "title": "Research fabric options",
                    "description": "Compare fabrics using the supplied source evidence.",
                    "agent_id": str(uuid4()),
                    "rationale": "The existing material researcher covers this bounded subtask.",
                }
            ]
        },
    }[kind]
    return {"kind": kind, "summary": "Advance the launch brief.", **fields, **changes}


@pytest.mark.parametrize(
    ("provider", "path"),
    [
        ("openai_compatible", "/v1/chat/completions"),
        ("openai_responses", "/v1/responses"),
        ("anthropic", "/v1/messages"),
        ("gemini", "/v1/models/configured-model:generateContent"),
    ],
)
def test_one_model_call_produces_only_a_candidate_across_providers(
    provider: str, path: str
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=wire_result(provider, json.dumps(action_document())))

    executor = WorkflowExecutor(binding(provider=provider), transport=httpx.MockTransport(respond))
    context = {"task": {"description": "UNTRUSTED SOURCE"}}
    history = ({"kind": "signal", "text": "Owner chose a small first drop."},)
    prepared = executor.prepare(context, history)
    assert not requests
    context["task"]["description"] = "CHANGED INPUT"
    history[0]["text"] = "CHANGED HISTORY"
    action, result = executor.generate(prepared)

    assert action.kind == "draft" and action.output == action_document()["output"]
    assert result.input_tokens == 101 and result.output_tokens == 31
    assert len(requests) == 1 and requests[0].url.path == path
    body = json.loads(requests[0].content)
    assert "tools" not in body and "response_format" not in body
    assert "authorization" not in requests[0].headers
    assert "UNTRUSTED SOURCE" in json.dumps(body)
    assert "Owner chose a small first drop." in json.dumps(body)
    assert "CHANGED" not in json.dumps(body)
    assert "untrusted data" in prepared.request.system
    assert "never accepted, published or externally executed" in prepared.request.system


@pytest.mark.parametrize("kind", ["draft", "question", "wait", "delegate", "reference"])
def test_actions_roundtrip_without_authority_or_hidden_controls(kind: str) -> None:
    action = WorkflowAction.model_validate_json(json.dumps(action_document(kind)), strict=True)
    restored = WorkflowAction.model_validate_json(action.model_dump_json(), strict=True)
    assert restored == action
    assert "additionalProperties" in WorkflowAction.model_json_schema()
    assert WorkflowAction.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "fields",
    [
        {"operation": "commit", "text": "Saved reference text"},
        {"operation": "delay", "text": None, "delay_seconds": 120},
    ],
)
def test_reference_operations_are_exact_bounded_local_requests(fields: dict[str, Any]) -> None:
    action = WorkflowAction.model_validate_json(
        json.dumps(action_document("reference", **fields)), strict=True
    )
    assert action.operation == fields["operation"]


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("draft", {"output": None}),
        ("draft", {"output": " "}),
        ("draft", {"question": "Hidden second action"}),
        ("draft", {"output": "x" * 24001}),
        ("draft", {"output": "😀" * 9000}),
        ("draft", {"allow_paid": True}),
        ("draft", {"summary": "\x00"}),
        ("draft", {"output": "\ud800"}),
        ("question", {"question": None}),
        ("wait", {"wait_seconds": "300"}),
        ("wait", {"wait_seconds": 300.0}),
        ("wait", {"wait_seconds": True}),
        ("wait", {"wait_seconds": 59}),
        ("wait", {"wait_seconds": 86401}),
        ("delegate", {"children": []}),
        ("delegate", {"output": "Hidden draft"}),
        ("delegate", {"children": [action_document("delegate")["children"][0]] * 5}),
        ("reference", {"operation": None}),
        ("reference", {"operation": "shell"}),
        ("reference", {"text": None}),
        ("reference", {"delay_seconds": 120}),
        ("reference", {"operation": "delay", "text": None, "delay_seconds": 59}),
        ("reference", {"operation": "delay", "text": None, "delay_seconds": 3601}),
    ],
)
def test_mixed_missing_unbounded_or_coerced_actions_are_rejected(
    kind: str, changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        WorkflowAction.model_validate_json(
            json.dumps(action_document(kind, **changes)), strict=True
        )


def test_children_cannot_create_agents_or_include_credentials() -> None:
    document = action_document("delegate")
    document["children"][0]["credential"] = "private"
    with pytest.raises(ValidationError):
        WorkflowAction.model_validate_json(json.dumps(document), strict=True)


def test_reservation_covers_utf8_prompt_framing_and_bounded_output_at_declared_rates() -> None:
    executor = WorkflowExecutor(
        binding(
            input_cost_per_million_usd=0.125,
            output_cost_per_million_usd=1.333,
            max_output_tokens=2048,
            context_window_tokens=32768,
        )
    )
    prepared = executor.prepare({"source": "é😀"}, ())
    bound = len(prepared.request.system.encode()) + len(prepared.request.prompt.encode()) + 2048
    assert prepared.decision.request.input_tokens == bound
    assert prepared.request.max_output_tokens == prepared.decision.request.output_tokens == 2048
    assert bound + 2048 <= 32768
    cost = bound * Decimal("0.125") + 2048 * Decimal("1.333")
    assert prepared.reservation_microusd == int(cost.to_integral_value(rounding=ROUND_CEILING))
    assert prepared.input_rate == Decimal("0.125")
    assert prepared.output_rate == Decimal("1.333")
    assert prepared.request_digest == executor.prepare({"source": "é😀"}, ()).request_digest
    assert prepared.request_digest != executor.prepare({"source": "Changed"}, ()).request_digest
    assert len(prepared.request_digest) == 64


def test_default_local_has_zero_api_cost_and_global_output_bound() -> None:
    prepared = WorkflowExecutor(binding()).prepare({}, ())
    assert prepared.reservation_microusd == 0
    assert prepared.input_rate == prepared.output_rate == Decimal(0)
    assert prepared.request.max_output_tokens == 4096
    assert prepared.decision.request.privacy == "local_only"


def test_cloud_uses_only_the_explicit_scoped_binding_credential(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_PROJECT_KEY", "wrong-process-secret")
    selected = replace(
        binding(
            local=False,
            api_key_env="MODEL_PROJECT_KEY",
            base_url="https://approved.example/v1",
            input_cost_per_million_usd=1,
            output_cost_per_million_usd=2,
        ),
        credential="scoped-secret",
    )
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json=wire_result("openai_compatible", json.dumps(action_document()))
        )

    executor = WorkflowExecutor(selected, transport=httpx.MockTransport(respond))
    prepared = executor.prepare({}, ())
    assert prepared.decision.request.privacy == "allow_cloud"
    assert "scoped-secret" not in repr(prepared)
    executor.generate(prepared)
    assert requests[0].headers["authorization"] == "Bearer scoped-secret"
    assert "scoped-secret" not in requests[0].content.decode()
    without_key = WorkflowExecutor(replace(selected, credential=None))
    with pytest.raises(WorkflowPlanningError, match="endpoint_not_ready"):
        without_key.prepare({}, ())


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"capabilities": frozenset({"image"})},
        {"api_key_env": "UNAVAILABLE_CREDENTIAL"},
        {"input_cost_per_million_usd": 1},
        {"output_cost_per_million_usd": 1},
        {"local": False, "api_key_env": "KEY", "base_url": "https://approved.example/v1"},
    ],
)
def test_unready_endpoint_never_calls_a_provider(changes: dict[str, Any]) -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Preparation is offline")

    executor = WorkflowExecutor(binding(**changes), transport=httpx.MockTransport(forbidden))
    with pytest.raises(WorkflowPlanningError, match="endpoint_not_ready") as failure:
        executor.prepare({}, ())
    assert not failure.value.may_have_been_dispatched
    assert failure.value.result is None
    assert "UNAVAILABLE_CREDENTIAL" not in str(failure.value)


@pytest.mark.parametrize(
    ("context", "history", "changes", "code"),
    [
        ({"text": "x" * 65000}, (), {}, "context_limit_exceeded"),
        ({"text": "\ud800"}, (), {}, "invalid_context"),
        ({"x": float("nan")}, (), {}, "invalid_context"),
        ({"x": object()}, (), {}, "invalid_context"),
        ({1: "bad key"}, (), {}, "invalid_context"),
        ([], (), {}, "invalid_context"),
        ({}, [], {}, "invalid_context"),
        ({}, ("not a record",), {}, "invalid_context"),
        (
            {},
            (),
            {"context_window_tokens": 1000, "max_output_tokens": 100},
            "endpoint_context_unavailable",
        ),
    ],
)
def test_context_must_be_bounded_valid_data(context, history, changes, code) -> None:
    with pytest.raises(WorkflowPlanningError, match=code) as failure:
        WorkflowExecutor(binding(**changes)).prepare(context, history)
    assert not failure.value.may_have_been_dispatched


def test_deep_or_circular_context_is_rejected_before_serialization() -> None:
    recursive = {}
    recursive["self"] = recursive
    with pytest.raises(WorkflowPlanningError, match="invalid_context"):
        WorkflowExecutor(binding()).prepare(recursive, ())


@pytest.mark.parametrize(
    "text",
    [
        "private-provider-body",
        '```json\n{"kind":"draft"}\n```',
        '{"kind":"draft","kind":"question","summary":"Bad"}',
        '{"kind":"draft","summary":"Bad","output":NaN}',
        json.dumps(action_document("wait", wait_seconds="300")),
        json.dumps(action_document("draft", publish=True)),
        '{"deep":' + "[" * 25 + "0" + "]" * 25 + "}",
        json.dumps([action_document()]),
        json.dumps(action_document(output="\ud800")),
    ],
)
def test_invalid_response_preserves_known_usage_and_never_retries(text: str) -> None:
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=wire_result("openai_compatible", text))

    executor = WorkflowExecutor(binding(), transport=httpx.MockTransport(respond))
    with pytest.raises(WorkflowPlanningError, match="invalid_workflow_action") as failure:
        executor.generate(executor.prepare({}, ()))
    assert failure.value.result is not None
    assert failure.value.result.input_tokens == 101 and failure.value.result.output_tokens == 31
    assert failure.value.may_have_been_dispatched
    assert "private-provider-body" not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("text", "flags", "code"),
    [
        ("", {"refused": True}, "model_refused"),
        ("partial", {"truncated": True}, "model_output_truncated"),
        ("x" * (MAX_ACTION_BYTES + 1), {}, "model_output_too_large"),
    ],
    ids=["refusal", "truncation", "oversize"],
)
def test_terminal_response_errors_keep_settleable_usage(text, flags, code) -> None:
    executor = WorkflowExecutor(
        binding(provider="openai_responses"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=wire_result("openai_responses", text, **flags))
        ),
    )
    with pytest.raises(WorkflowPlanningError, match=code) as failure:
        executor.generate(executor.prepare({}, ()))
    assert failure.value.result is not None and failure.value.result.input_tokens == 101


def test_mismatched_endpoint_retains_result_for_accounting(monkeypatch) -> None:
    executor = WorkflowExecutor(binding())
    result = TextGenerationResult(
        endpoint_id="other", model="unexpected", text=json.dumps(action_document()), input_tokens=9
    )
    monkeypatch.setattr(executor._client, "generate", lambda *_: result)
    with pytest.raises(WorkflowPlanningError, match="model_identity_mismatch") as failure:
        executor.generate(executor.prepare({}, ()))
    assert failure.value.result is result


@pytest.mark.parametrize("response", ["timeout", "http-error", "invalid-body"])
def test_uncertain_dispatch_does_not_retry_fallback_or_fabricate_action(response) -> None:
    calls = []

    def respond(request):
        calls.append(request)
        if response == "timeout":
            raise httpx.ReadTimeout("private-error-detail", request=request)
        if response == "http-error":
            return httpx.Response(503, text="private-error-detail")
        return httpx.Response(200, text="private-error-detail")

    executor = WorkflowExecutor(binding(), transport=httpx.MockTransport(respond))
    with pytest.raises(ModelEndpointError) as failure:
        executor.generate(executor.prepare({}, ()))
    assert failure.value.may_have_been_dispatched
    assert "private-error-detail" not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "output", ["😀" * 9000, "\x1f" * 6000], ids=["multibyte", "escaped-control"]
)
def test_generation_byte_overflow_preserves_usage_before_checkpoint(output):
    text = json.dumps(action_document(output=output), ensure_ascii=False)
    executor = WorkflowExecutor(
        binding(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=wire_result("openai_compatible", text))
        ),
    )
    with pytest.raises(WorkflowPlanningError) as failure:
        executor.generate(executor.prepare({}, ()))
    assert failure.value.result is not None
    assert failure.value.result.input_tokens == 101 and failure.value.result.output_tokens == 31
    assert failure.value.code == "model_output_too_large"


def test_canonical_multibyte_action_expansion_is_checked_before_returning_result():
    document = action_document(output="é" * 16000, summary="x")
    raw_size = len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode())
    document["summary"] = "s" * (MAX_ACTION_BYTES - raw_size - 7)
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    assert len(text.encode()) <= MAX_ACTION_BYTES
    executor = WorkflowExecutor(
        binding(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=wire_result("openai_compatible", text))
        ),
    )
    with pytest.raises(WorkflowPlanningError, match="invalid_workflow_action") as failure:
        executor.generate(executor.prepare({}, ()))
    assert failure.value.result is not None and failure.value.result.output_tokens == 31


def test_valid_multibyte_action_and_worst_case_reference_receipt_fit_checkpoint_bytes():
    document = action_document(output="é" * 16000)
    action = WorkflowAction.model_validate_json(json.dumps(document))
    saved = action.model_dump(mode="json", exclude_none=True)
    assert bounded_json(saved, MAX_ACTION_BYTES) == saved
    reference = {
        "operation": "commit",
        "text": "\x1f" * 4000,
        "operation_id": str(uuid4()),
        "receipt": "a" * 64,
    }
    assert bounded_json(reference, MAX_ACTION_BYTES) == reference
