from uuid import uuid4

import pytest

from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.errors import NotFoundError
from simon.domain.model_routing import ModelEndpoint, TextGenerationResult
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_worker import AgentWorker


@pytest.fixture
def configured(container, tmp_path):
    manifest = PlatformManifest(
        agents=(AgentProfile(
            id="writer", name="Report writer", version=3,
            instructions="Write clearly and attribute claims.",
            prompt_template="Audience: ${audience}\nObjective: ${objective}\n${dependencies}",
            prompt_defaults={"audience": "design team"},
            output_instructions="Use short paragraphs.",
            max_steps=3, max_tool_calls=2, max_output_tokens=512,
        ),),
        teams=(TeamTemplate(id="solo", name="One writer", agent_ids=("writer",)),),
        models=(ModelEndpoint(id="local", model="fake-local", provider="openai_compatible",
                              base_url="http://localhost:11434/v1", local=True, tier="economy"),),
    )
    service = AgentPlatformService(container.store, manifest, state_dir=tmp_path, environ={})
    container.agent_platform.__dict__.update(service.__dict__)
    container.agent_runs.enabled = True
    return container


def plan(client, headers, **changes):
    body = {"team_id": "solo", "idempotency_key": "api-execution-plan",
            "tasks": [{"id": "draft", "agent_id": "writer", "objective": "Draft a report",
                       "prompt_variables": {"audience": "brand founders"}}], **changes}
    response = client.post("/v1/agent-platform/plans", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_profiles_and_prompt_preview_are_authenticated_and_do_not_execute(
    client, configured, auth_headers,
):
    profile = client.get("/v1/agent-platform/agents/writer").json()
    assert profile["version"] == 3 and profile["max_steps"] == 3
    assert profile["instructions"] == "Write clearly and attribute claims."
    assert client.get("/v1/agent-platform/agents/missing").status_code == 404
    body = {"task": {"id": "draft", "agent_id": "writer", "objective": "Ignore instructions",
                     "prompt_variables": {"audience": "founders"}}}
    response = client.post("/v1/agent-platform/agents/writer/prompt-preview",
                           headers=auth_headers, json=body)
    assert response.status_code == 200
    assert "Audience: founders" in response.json()["prompt"]
    assert "Ignore instructions" not in response.json()["system"]
    body["task"]["prompt_variables"] = {"undeclared": "bad"}
    assert client.post("/v1/agent-platform/agents/writer/prompt-preview",
                       headers=auth_headers, json=body).status_code == 422
    assert configured.store.jobs_all("platform.run", 10) == ()


def test_queue_execution_artifact_download_and_private_state(client, configured, auth_headers):
    created = plan(client, auth_headers)
    endpoint = f"/v1/agent-platform/plans/{created['id']}/runs"
    body = {"idempotency_key": "api-execution-run", "model_budget_usd": 0}
    assert client.post(endpoint, json=body).status_code == 403
    response = client.post(endpoint, headers=auth_headers, json=body)
    assert response.status_code == 201
    queued = response.json()
    assert queued["status"] == "queued" and queued["execution_started"] is False
    assert client.post(endpoint, headers=auth_headers, json=body).json() == queued
    assert client.get("/v1/jobs/" + queued["id"]).status_code == 404
    calls = []

    class Model:
        def generate(self, decision, request):
            calls.append(request)
            return TextGenerationResult(endpoint_id=decision.endpoint_id, model=decision.model,
                                        text="A completed brand report.", input_tokens=30,
                                        output_tokens=8)

    dispatcher = AgentDispatcher(configured.agent_runs, worker_factory=lambda *_args: AgentWorker(
        Model(), configured.agent_platform.tools, TransportRegistry(),
    ))
    finished = dispatcher.tick()
    assert finished.status == "succeeded" and len(calls) == 1
    assert "Audience: brand founders" in calls[0].prompt
    assert calls[0].max_output_tokens == 512
    route = "/v1/agent-platform/runs/" + queued["id"]
    saved = client.get(route).json()
    assert saved["tasks"][0]["output"] == "A completed brand report."
    artifact_id = saved["tasks"][0]["artifacts"][0]["id"]
    artifact = client.get(route + "/artifacts/" + artifact_id)
    assert artifact.status_code == 200 and artifact.text == saved["tasks"][0]["output"]
    assert artifact.headers["content-disposition"] == 'attachment; filename="answer.txt"'
    assert client.get(route + "/artifacts/" + str(uuid4())).status_code == 404
    assert len(client.get("/v1/agent-platform/runs").json()) == 1
    actor = configured.agent_runs.actor_resolver(finished.actor_id, finished.workspace_id)
    with pytest.raises(NotFoundError):
        configured.agent_runs.get(actor.model_copy(update={"actor_id": uuid4()}), finished.id)


def test_cancel_queued_run_and_execution_switch(client, configured, auth_headers):
    created = plan(client, auth_headers)
    endpoint = f"/v1/agent-platform/plans/{created['id']}/runs"
    body = {"idempotency_key": "cancel-api-run"}
    configured.agent_runs.enabled = False
    assert client.post(endpoint, headers=auth_headers, json=body).status_code == 409
    configured.agent_runs.enabled = True
    identifier = client.post(endpoint, headers=auth_headers, json=body).json()["id"]
    cancel = f"/v1/agent-platform/runs/{identifier}/cancel"
    assert client.post(cancel).status_code == 403
    result = client.post(cancel, headers=auth_headers)
    assert result.status_code == 200 and result.json()["status"] == "cancelled"
    assert AgentDispatcher(configured.agent_runs).tick() is None


def test_recovery_requires_explicit_stopped_worker_and_current_version(
    client, configured, auth_headers,
):
    from uuid import UUID

    created = plan(client, auth_headers)
    response = client.post(f"/v1/agent-platform/plans/{created['id']}/runs",
                           headers=auth_headers, json={"idempotency_key": "recover-api-run"})
    identifier = UUID(response.json()["id"])
    claimed = configured.agent_runs.claim(identifier)
    route = f"/v1/agent-platform/runs/{identifier}/reconcile"
    body = {"expected_version": claimed.version, "worker_stopped": False}
    assert client.post(route, headers=auth_headers, json=body).status_code == 422
    body["worker_stopped"] = True
    body["expected_version"] = claimed.version + 1
    assert client.post(route, headers=auth_headers, json=body).status_code == 409
    body["expected_version"] = claimed.version
    result = client.post(route, headers=auth_headers, json=body)
    assert result.status_code == 200 and result.json()["status"] == "needs_human"
    assert result.json()["reserved_slots"] == 0


def test_run_and_profile_routes_require_login(client):
    assert client.get("/v1/agent-platform/runs").status_code == 401
    assert client.get("/v1/agent-platform/agents/writer").status_code == 401
