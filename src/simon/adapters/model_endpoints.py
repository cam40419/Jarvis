"""Bounded text transports for configured local and hosted model endpoints.

No automatic retries, hidden tool execution, provider error bodies, or credentials
are exposed to the orchestration layer. A network failure can still incur cost.
Routing estimates are planning inputs; a durable budget ledger belongs to dispatch.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from simon.domain.model_routing import (
    ModelEndpoint,
    RoutingDecision,
    TextGenerationRequest,
    TextGenerationResult,
)
from simon.services.model_router import ModelRouter, ModelRoutingError


class ModelEndpointError(RuntimeError):
    def __init__(self, code: str, message: str, *, may_have_been_dispatched: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.may_have_been_dispatched = may_have_been_dispatched


class ModelEndpointClient:
    def __init__(
        self,
        endpoints: Sequence[ModelEndpoint],
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 120,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("Timeout and response bound must be positive")
        self._environ = os.environ if environ is None else environ
        self._router = ModelRouter(endpoints, environ=self._environ)
        self._endpoints = {endpoint.id: endpoint for endpoint in endpoints}
        self._transport = transport
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def generate(
        self, decision: RoutingDecision, request: TextGenerationRequest
    ) -> TextGenerationResult:
        endpoint = self._validate(decision, request)
        path, payload, headers = self._wire_request(endpoint, decision, request)
        try:
            with (
                httpx.Client(
                    transport=self._transport,
                    timeout=self._timeout,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "POST", f"{endpoint.base_url}{path}", json=payload, headers=headers
                ) as response,
            ):
                if response.status_code != 200:
                    raise ModelEndpointError(
                        "provider_request_failed",
                        f"Model endpoint returned HTTP {response.status_code}; request not retried",
                        may_have_been_dispatched=True,
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > self._max_response_bytes:
                        raise ModelEndpointError(
                            "response_too_large",
                            "Model response exceeded the configured byte limit",
                            may_have_been_dispatched=True,
                        )
        except httpx.HTTPError:
            raise ModelEndpointError(
                "provider_connection_failed",
                "Model request failed; it may have incurred cost and was not retried",
                may_have_been_dispatched=True,
            ) from None
        try:
            document = httpx.Response(200, content=bytes(data)).json()
            return self._parse(endpoint, document, controller_mode=request.controller_mode)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            raise ModelEndpointError(
                "invalid_model_response",
                "Model endpoint returned an invalid or unsupported text result",
                may_have_been_dispatched=True,
            ) from None

    def _validate(self, decision: RoutingDecision, request: TextGenerationRequest) -> ModelEndpoint:
        try:
            pinned = decision.request.model_copy(update={"model_override": decision.endpoint_id})
            current = self._router.route(pinned)
        except ModelRoutingError:
            raise ModelEndpointError(
                "route_unavailable", "The selected model no longer satisfies routing constraints"
            ) from None
        if any(
            getattr(current, name) != getattr(decision, name)
            for name in ("model", "provider", "tier", "local", "reasoning_effort")
        ):
            raise ModelEndpointError("route_changed", "Model configuration changed; route again")
        endpoint = self._endpoints[decision.endpoint_id]
        if request.response_schema is not None and endpoint.provider != "openai_responses":
            raise ModelEndpointError(
                "unsupported_generation_mode",
                "Structured response schemas are supported only by the OpenAI Responses transport",
            )
        allowed_capabilities = {"text", "tools"} if request.controller_mode else {"text"}
        if (
            "text" not in endpoint.capabilities
            or not decision.request.required_capabilities <= allowed_capabilities
            or (request.controller_mode and "tools" not in endpoint.capabilities)
        ):
            raise ModelEndpointError(
                "unsupported_generation_mode",
                "Generation requires text capability; controller mode also requires tools "
                "capability, and media requires its own runner",
            )
        if request.max_output_tokens > decision.request.output_tokens:
            raise ModelEndpointError(
                "output_reservation_exceeded", "Requested output exceeds the routed token estimate"
            )
        return endpoint

    def _wire_request(
        self,
        endpoint: ModelEndpoint,
        decision: RoutingDecision,
        request: TextGenerationRequest,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        key = self._environ.get(endpoint.api_key_env, "") if endpoint.api_key_env else ""
        headers: dict[str, str] = {}
        effort = decision.reasoning_effort
        if endpoint.provider == "openai_responses":
            if key:
                headers["Authorization"] = f"Bearer {key}"
            payload: dict[str, Any] = {
                "model": endpoint.model,
                "input": request.prompt,
                "instructions": request.system,
                "max_output_tokens": request.max_output_tokens,
                "store": False,
            }
            if effort:
                payload["reasoning"] = {"effort": effort}
            if request.controller_mode:
                payload["tools"] = [
                    {
                        "type": "function",
                        "name": "simon_controller",
                        "description": "Return one next controller action or final result",
                        "strict": True,
                        "parameters": request.response_schema,
                    }
                ]
                payload["tool_choice"] = {"type": "function", "name": "simon_controller"}
                payload["parallel_tool_calls"] = False
            elif request.response_schema is not None:
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": "simon_controller",
                        "strict": True,
                        "schema": request.response_schema,
                    }
                }
            return "/responses", payload, headers
        if endpoint.provider == "openai_compatible":
            if key:
                headers["Authorization"] = f"Bearer {key}"
            messages: list[dict[str, str]] = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.append({"role": "user", "content": request.prompt})
            payload = {
                "model": endpoint.model,
                "messages": messages,
                "max_tokens": request.max_output_tokens,
                "stream": False,
            }
            if effort:
                payload["reasoning_effort"] = effort
            return "/chat/completions", payload, headers
        if endpoint.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["x-api-key"] = key
            payload = {
                "model": endpoint.model,
                "messages": [{"role": "user", "content": request.prompt}],
                "max_tokens": request.max_output_tokens,
            }
            if request.system:
                payload["system"] = request.system
            if effort:
                payload["output_config"] = {"effort": effort}
            return "/messages", payload, headers
        if key:
            headers["x-goog-api-key"] = key
        generation: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
        if effort:
            generation["thinkingConfig"] = {"thinkingLevel": effort.upper()}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": generation,
        }
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        model = endpoint.model.removeprefix("models/")
        return f"/models/{quote(model, safe='')}:generateContent", payload, headers

    @staticmethod
    def _parse(
        endpoint: ModelEndpoint, document: Any, controller_mode: bool = False
    ) -> TextGenerationResult:
        if not isinstance(document, dict):
            raise ValueError("Expected an object")
        text_parts: list[str] = []
        truncated = False
        refused = False
        reasoning_tokens = None
        if endpoint.provider == "openai_responses":
            status = document.get("status")
            if status == "incomplete":
                if document.get("incomplete_details", {}).get("reason") != "max_output_tokens":
                    raise ValueError("Unsupported incomplete response")
                truncated = True
            elif status != "completed":
                raise ValueError("Response did not complete")
            output = document["output"]
            if not isinstance(output, list):
                raise ValueError("Expected output items")
            for item in output:
                if item["type"] == "reasoning":
                    continue
                if item["type"] == "message":
                    for part in item["content"]:
                        if part["type"] == "refusal":
                            if not isinstance(part.get("refusal"), str):
                                raise ValueError("Invalid refusal content")
                            # Record the typed outcome, never the provider's
                            # refusal body or accompanying generated text.
                            refused = True
                        elif part["type"] == "output_text":
                            if not isinstance(part.get("text"), str):
                                raise ValueError("Invalid text content")
                            if not controller_mode:
                                text_parts.append(part["text"])
                        else:
                            raise ValueError("Unsupported response content")
                    continue
                if controller_mode:
                    if (
                        item["type"] != "function_call"
                        or item.get("name") != "simon_controller"
                        or not isinstance(item.get("arguments"), str)
                    ):
                        raise ValueError("Unexpected controller output")
                    text_parts.append(item["arguments"])
                    continue
                raise ValueError("Unexpected non-message output")
            if controller_mode and (
                len(text_parts) > 1 or (not text_parts and not (truncated or refused))
            ):
                raise ValueError("Expected exactly one controller function call")
            usage = document.get("usage") or {}
            incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
            reasoning_tokens = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
        elif endpoint.provider == "openai_compatible":
            choice = document["choices"][0]
            if choice.get("finish_reason") not in {"stop", "length"}:
                raise ValueError("Unexpected completion finish reason")
            if choice["message"].get("tool_calls") or choice["message"].get("function_call"):
                raise ValueError("Tool execution is not supported by this text adapter")
            text_parts.append(choice["message"]["content"])
            truncated = choice["finish_reason"] == "length"
            usage = document.get("usage") or {}
            incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
        elif endpoint.provider == "anthropic":
            if document.get("stop_reason") not in {"end_turn", "max_tokens", "stop_sequence"}:
                raise ValueError("Unexpected message stop reason")
            for part in document["content"]:
                if part["type"] in {"thinking", "redacted_thinking"}:
                    continue
                if part["type"] != "text":
                    raise ValueError("Unsupported message content")
                text_parts.append(part["text"])
            truncated = document["stop_reason"] == "max_tokens"
            usage = document.get("usage") or {}
            incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        else:
            candidate = document["candidates"][0]
            if candidate.get("finishReason") not in {"STOP", "MAX_TOKENS"}:
                raise ValueError("Unexpected candidate finish reason")
            text_parts.extend(
                part["text"]
                for part in candidate["content"]["parts"]
                if not part.get("thought", False)
            )
            truncated = candidate["finishReason"] == "MAX_TOKENS"
            usage = document.get("usageMetadata") or {}
            incoming = usage.get("promptTokenCount")
            outgoing = usage.get("candidatesTokenCount")
            thoughts = usage.get("thoughtsTokenCount")
            if outgoing is not None and thoughts is not None:
                outgoing += thoughts
        text = "" if refused else "\n".join(text_parts)
        if not text and not (truncated or refused):
            raise ValueError("Response contained no text")
        return TextGenerationResult(
            endpoint_id=endpoint.id,
            model=document.get("model") or document.get("modelVersion") or endpoint.model,
            text=text,
            input_tokens=incoming,
            output_tokens=outgoing,
            truncated=truncated,
            refused=refused,
            response_reason="refusal" if refused else "max_output_tokens" if truncated else None,
            reasoning_tokens=reasoning_tokens,
        )
