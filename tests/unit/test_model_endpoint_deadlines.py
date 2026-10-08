"""Slow responses are bounded without relying on sleeps or live provider timing."""

import json

import httpx
import pytest

from simon.adapters.model_endpoints import ModelEndpointClient, ModelEndpointError
from simon.domain.model_routing import RoutingRequest, TextGenerationRequest
from simon.services.model_router import ModelRouter
from tests.unit.test_model_routing import endpoint


@pytest.mark.parametrize("delay", ["headers", "chunks", "completion"])
def test_total_elapsed_deadline_closes_response_without_retry(monkeypatch, delay):
    clock = [0.0]
    monkeypatch.setattr("simon.adapters.model_endpoints.monotonic", lambda: clock[0])
    requests, consumed, closed = [], [], []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            if delay == "completion":
                clock[0] = 12
                return
            for index in range(10):
                clock[0] += 4
                consumed.append(index)
                yield b"x"

        def close(self):
            closed.append(True)

    def handle(request):
        requests.append(request)
        if delay == "headers":
            clock[0] = 12
        return httpx.Response(200, stream=SlowStream())

    configured = endpoint()
    client = ModelEndpointClient(
        (configured,), environ={}, timeout_seconds=10, transport=httpx.MockTransport(handle)
    )
    decision = ModelRouter((configured,), environ={}).route(RoutingRequest())
    with pytest.raises(ModelEndpointError) as caught:
        client.generate(decision, TextGenerationRequest(prompt="Synthetic input"))
    assert caught.value.code == "provider_deadline_exceeded"
    assert caught.value.may_have_been_dispatched
    assert len(requests) == 1 and closed == [True]
    assert len(consumed) == (3 if delay == "chunks" else 0)


def test_timely_chunked_response_retains_bounded_network_phase_timeouts(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("simon.adapters.model_endpoints.monotonic", lambda: clock[0])
    body = json.dumps(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": "Done"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
    ).encode()
    seen, closed = [], []

    class TimelyStream(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(body), 10):
                clock[0] += 0.1
                yield body[offset : offset + 10]

        def close(self):
            closed.append(True)

    def handle(request):
        seen.append(request)
        return httpx.Response(200, stream=TimelyStream())

    configured = endpoint()
    client = ModelEndpointClient(
        (configured,), environ={}, timeout_seconds=600, transport=httpx.MockTransport(handle)
    )
    decision = ModelRouter((configured,), environ={}).route(RoutingRequest())
    result = client.generate(decision, TextGenerationRequest(prompt="Synthetic input"))
    assert result.text == "Done" and result.input_tokens == 10
    assert seen[0].extensions["timeout"] == {"connect": 10, "pool": 10, "write": 30, "read": 120}
    assert len(seen) == 1 and closed == [True]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_elapsed_timeout_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError):
        ModelEndpointClient((endpoint(),), timeout_seconds=timeout)
