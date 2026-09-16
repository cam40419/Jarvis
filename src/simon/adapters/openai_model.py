from collections.abc import Callable
from time import monotonic
from typing import cast

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)
from openai.types.responses import Response, ResponseInputParam

from simon.adapters.model_tools import cited_text, definitions
from simon.domain.errors import ModelError
from simon.domain.model import ModelAnswer, ModelRequest


class OpenAIModel:
    def __init__(self, api_key: str) -> None:
        # Fixed official endpoint, finite timeouts, and no automatic generation retries.
        self.api_key = api_key

    def generate(self, request: ModelRequest) -> ModelAnswer:
        return self._generate(request, None)

    def generate_stream(
        self, request: ModelRequest, on_delta: Callable[[str], None]
    ) -> ModelAnswer:
        return self._generate(request, on_delta)

    def generate_with_tools(
        self,
        request: ModelRequest,
        on_delta: Callable[[str], None] | None,
        execute: Callable[[str, str], str],
    ) -> ModelAnswer:
        return self._generate(request, on_delta, execute)

    def _generate(
        self,
        request: ModelRequest,
        on_delta: Callable[[str], None] | None,
        execute: Callable[[str, str], str] | None = None,
    ) -> ModelAnswer:
        started = monotonic()
        first_text_ms = None
        try:
            with OpenAI(
                api_key=self.api_key,
                base_url="https://api.openai.com/v1",
                timeout=30,
                max_retries=0,
            ) as client:
                inputs: str | ResponseInputParam = request.input_text
                tools = definitions(request.tools)
                input_tokens = output_tokens = reasoning_tokens = count_ms = 0
                calls: list[str] = []
                # Device setup may need list, name, configure, organize, then control.
                max_rounds = (
                    6
                    if {"home_rename_device", "home_setup_outlet"}.intersection(request.tools)
                    else 4
                )
                for _round in range(max_rounds):
                    if on_delta:
                        on_delta("")
                    remaining = request.timeout_seconds - (monotonic() - started)
                    if remaining <= 0:
                        raise ModelError("model_timeout")
                    counting = monotonic()
                    count = client.responses.input_tokens.count(
                        model=request.model,
                        instructions=request.instructions,
                        input=inputs,
                        reasoning={"effort": request.reasoning_effort},
                        text={"verbosity": request.verbosity},
                        tools=tools,
                        truncation="disabled",
                        timeout=remaining,
                    )
                    count_ms += int((monotonic() - counting) * 1000)
                    if (
                        count.input_tokens > request.input_token_limit
                        or input_tokens + count.input_tokens > 60000
                    ):
                        raise ModelError("model_input_limit")
                    remaining = request.timeout_seconds - (monotonic() - started)
                    budget = request.max_output_tokens - output_tokens
                    if remaining <= 0:
                        raise ModelError("model_timeout")
                    if budget < 16:
                        raise ModelError("model_incomplete")
                    response: Response | None = None
                    if on_delta is None:
                        response = client.responses.create(
                            model=request.model,
                            instructions=request.instructions,
                            input=inputs,
                            store=False,
                            max_output_tokens=budget,
                            tools=tools,
                            max_tool_calls=4,
                            parallel_tool_calls=False,
                            include=["reasoning.encrypted_content"],
                            truncation="disabled",
                            reasoning={"effort": request.reasoning_effort},
                            text={"verbosity": request.verbosity},
                            timeout=remaining,
                        )
                    else:
                        with client.responses.create(
                            model=request.model,
                            instructions=request.instructions,
                            input=inputs,
                            store=False,
                            max_output_tokens=budget,
                            tools=tools,
                            max_tool_calls=4,
                            parallel_tool_calls=False,
                            include=["reasoning.encrypted_content"],
                            truncation="disabled",
                            reasoning={"effort": request.reasoning_effort},
                            text={"verbosity": request.verbosity},
                            timeout=remaining,
                            stream=True,
                        ) as stream:
                            size = 0
                            for event in stream:
                                if monotonic() - started > request.timeout_seconds:
                                    raise ModelError("model_timeout")
                                if event.type == "response.output_text.delta":
                                    size += len(event.delta)
                                    if size > 60000:
                                        raise ModelError("model_incomplete")
                                    if first_text_ms is None:
                                        first_text_ms = int((monotonic() - started) * 1000)
                                    on_delta(event.delta)
                                else:
                                    on_delta("")
                                if event.type == "response.completed":
                                    response = event.response
                                elif event.type in {
                                    "response.failed",
                                    "response.incomplete",
                                    "error",
                                }:
                                    raise ModelError("model_incomplete")
                    if monotonic() - started > request.timeout_seconds:
                        raise ModelError("model_timeout")
                    if response is None or response.status != "completed" or response.usage is None:
                        raise ModelError("model_incomplete")
                    input_tokens += response.usage.input_tokens
                    output_tokens += response.usage.output_tokens
                    reasoning_tokens += (
                        response.usage.output_tokens_details.reasoning_tokens
                        if response.usage.output_tokens_details
                        else 0
                    )
                    if output_tokens > request.max_output_tokens:
                        raise ModelError("model_incomplete")
                    calls.extend(
                        item.type for item in response.output if item.type == "web_search_call"
                    )
                    functions = [item for item in response.output if item.type == "function_call"]
                    if functions:
                        if (
                            execute is None
                            or len(calls) + len(functions) > 12
                            or _round == max_rounds - 1
                        ):
                            raise ModelError("model_incomplete")
                        previous: ResponseInputParam = (
                            [{"role": "user", "content": inputs}]
                            if isinstance(inputs, str)
                            else list(inputs)
                        )
                        previous.extend(
                            cast(
                                ResponseInputParam,
                                [item.model_dump(exclude_none=True) for item in response.output],
                            )
                        )
                        for function in functions:
                            if function.name not in request.tools or function.name == "web_search":
                                raise ModelError("model_incomplete")
                            if on_delta:
                                on_delta("")
                            if monotonic() - started > request.timeout_seconds:
                                raise ModelError("model_timeout")
                            result = execute(function.name, function.arguments)
                            calls.append(function.name)
                            previous.append(
                                {
                                    "type": "function_call_output",
                                    "call_id": function.call_id,
                                    "output": result,
                                }
                            )
                        inputs = previous
                        continue
                    text, sources = cited_text(response)
                    if not text.strip() or len(text) > 60000:
                        raise ModelError("model_incomplete")
                    return ModelAnswer(
                        text=text,
                        web_sources=sources,
                        tool_calls=tuple(calls),
                        response_id=response.id,
                        model=response.model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        reasoning_tokens=reasoning_tokens,
                        token_count_ms=count_ms,
                        first_text_ms=first_text_ms,
                        total_ms=int((monotonic() - started) * 1000),
                    )
                raise ModelError("model_incomplete")
        except AuthenticationError:
            raise ModelError("model_credentials") from None
        except PermissionDeniedError:
            raise ModelError("model_permissions") from None
        except RateLimitError:
            raise ModelError("model_rate_limit") from None
        except APITimeoutError:
            raise ModelError("model_timeout") from None
        except (APIConnectionError, APIError):
            raise ModelError() from None
