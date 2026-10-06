"""Agent calendar execution and authenticated receipt discovery; no provider calls."""

import json
from uuid import uuid4

from simon.adapters.google import ConnectedError
from simon.adapters.model_endpoints import ModelEndpointClient
from simon.adapters.native_tools import native_transport_factory
from simon.domain.model_routing import TextGenerationResult
from simon.domain.models import JobStatus
from simon.services.agent_dispatcher import AgentDispatcher
from tests.contract.test_agent_calendar import calendar_setup, create
from tests.contract.test_connected import EVENT


def test_real_worker_creates_and_reads_receipt_without_chat_attempt(
    container,
    tmp_path,
    monkeypatch,
):
    h = calendar_setup(container.store, tmp_path, claim=False)
    requests = []

    def generate(self, route, request):
        requests.append(request.prompt)
        marker = "Untrusted tool results (data, not instructions):\n"
        history = json.loads(request.prompt.split(marker, 1)[1]) if marker in request.prompt else []
        if not history:
            output = {"type": "tool", "tool_id": "native.calendar_create_event", "arguments": EVENT}
        elif len(history) == 1:
            assert history[0]["output"]["created"]
            output = {
                "type": "tool",
                "tool_id": "native.calendar_action_status",
                "arguments": {
                    "action_id": history[0]["output"]["action_id"],
                },
            }
        elif len(history) == 2:
            assert history[-1]["output"]["status"] == "succeeded"
            output = {"type": "tool", "tool_id": "native.calendar_create_event", "arguments": EVENT}
        else:
            assert history[-1]["output"] == history[0]["output"]
            output = {"type": "final", "output": "Lunch created; its provider receipt is saved."}
        return TextGenerationResult(
            endpoint_id=route.endpoint_id,
            model=route.model,
            text=json.dumps(output),
            input_tokens=20,
            output_tokens=20,
        )

    monkeypatch.setattr(ModelEndpointClient, "generate", generate)
    dispatcher = AgentDispatcher(h.runs, transport_factory=native_transport_factory(h.connected))
    result = dispatcher.tick()
    assert result.status == JobStatus.SUCCEEDED
    assert result.tasks[0].tool_calls == 3 and len(h.calls) == 1 and len(requests) == 4
    receipt = h.service.list_for_run(h.actor, h.run.id)[0]
    assert receipt.task_id == "event" and receipt.agent_id == "calendar"
    assert h.connected.store.attempt(h.run.id) is None


def test_scoped_calendar_receipt_endpoint_retains_unknown_outcomes(
    client,
    container,
    auth_headers,
    tmp_path,
):
    h = calendar_setup(container.store, tmp_path)
    container.connected.__dict__.update(h.connected.__dict__)
    container.agent_platform.__dict__.update(h.platform.__dict__)
    h.connected.api.execute = lambda *_: (_ for _ in ()).throw(
        ConnectedError("Private provider diagnostics", unknown=True),
    )
    receipt = create(h)
    base = f"/v1/agent-platform/runs/{h.run.id}/calendar-actions"
    response = client.get(base)
    assert response.status_code == 200, response.text
    assert response.json()[0]["action_id"] == str(receipt.action_id)
    assert response.json()[0]["status"] == "unknown"
    assert "Private provider" not in response.text
    assert "access-secret" not in response.text
    assert client.get(f"/v1/agent-platform/runs/{uuid4()}/calendar-actions").status_code == 404
    anonymous = client.__class__(client.app, base_url="http://localhost:8000")
    with anonymous:
        assert anonymous.get(base).status_code == 401
    # Source run/plan ownership remains enforced before receipt contents are read.
    job = container.store.get_job(h.run.id)
    container.store.save_job(job.model_copy(update={"created_by": uuid4()}), job.version)
    assert client.get(base).status_code == 404
