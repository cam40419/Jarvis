import pytest

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.model_routing import ModelEndpoint
from simon.services.agent_platform import AgentPlatformService


def test_catalog_requires_login(client):
    assert client.get("/v1/agent-platform/catalog").status_code == 401


@pytest.mark.parametrize("override", ["", "x" * 97])
def test_invalid_model_override_returns_validation_error(client, auth_headers, override):
    response = client.post(
        "/v1/agent-platform/plans", headers=auth_headers,
        json={"team_id": "solo", "idempotency_key": "invalid-model-override",
              "tasks": [{"id": "draft", "agent_id": "writer", "objective": "Draft",
                         "model_override": override}]},
    )
    assert response.status_code == 422


def test_authenticated_plan_api_preserves_state_and_csrf(client, container, auth_headers, tmp_path):
    manifest = PlatformManifest(
        agents=(AgentProfile(id="writer", instructions="Write clearly."),),
        teams=(TeamTemplate(id="solo", name="One worker", agent_ids=("writer",)),),
        models=(ModelEndpoint(id="local", provider="openai_compatible", local=True,
                              model="test-local", tier="economy",
                              base_url="http://localhost:11434/v1"),),
    )
    # Keep the object bound to the router while replacing its configured components.
    configured = AgentPlatformService(container.store, manifest, state_dir=tmp_path, environ={})
    container.agent_platform.__dict__.update(configured.__dict__)
    catalog = client.get("/v1/agent-platform/catalog").json()
    assert catalog["configured"] is True and catalog["execution_enabled"] is False
    body = {"team_id": "solo", "idempotency_key": "api-plan-first",
            "tasks": [{"id": "draft", "agent_id": "writer", "objective": "Draft a report"}]}
    assert client.post("/v1/agent-platform/plans", json=body).status_code == 403
    result = client.post("/v1/agent-platform/plans", headers=auth_headers, json=body)
    assert result.status_code == 201
    plan = result.json()
    assert plan["state"] == "planned" and plan["execution_started"] is False
    assert client.get("/v1/agent-platform/plans/" + plan["id"]).json() == plan
    assert client.get("/v1/agent-platform/plans").json() == [plan]
    assert client.get("/v1/jobs/" + plan["id"]).status_code == 404
    assert client.post("/v1/jobs", headers=auth_headers,
                       json={"kind": "platform.plan", "input": {},
                             "idempotency_key": "forged-api-plan"}).status_code == 422
