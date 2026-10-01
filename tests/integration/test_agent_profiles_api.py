"""Browser API boundaries for reusable agents composed from granted skills."""

from uuid import UUID, uuid4

import pytest

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.agent_profiles import AgentProfileRecord
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_platform import AgentPlatformService


@pytest.fixture
def profile_api(container, tmp_path):
    tools = (
        ToolDefinition(
            id="native.local_file_read",
            description="Read local files",
            transport="native",
            configured=True,
            required_scopes=frozenset({"jobs:read"}),
        ),
        ToolDefinition(
            id="native.local_file_write",
            description="Write local files",
            transport="native",
            configured=True,
            side_effect=True,
            action_policy="write",
            required_scopes=frozenset({"jobs:write"}),
        ),
    )
    manifest = PlatformManifest(
        tools=tools,
        agents=(
            AgentProfile(
                id="file-reader",
                name="File research",
                instructions="Read sources.",
                tool_ids=(tools[0].id,),
                tool_scopes=frozenset({"jobs:read"}),
            ),
            AgentProfile(
                id="file-writer",
                name="Document writing",
                instructions="Write reports.",
                tool_ids=(tools[1].id,),
                max_action="write",
                tool_scopes=frozenset({"jobs:read", "jobs:write"}),
            ),
        ),
        teams=(
            TeamTemplate(
                id="documents", name="Documents", agent_ids=("file-reader", "file-writer")
            ),
        ),
    )
    configured = AgentPlatformService(
        container.store,
        manifest,
        state_dir=tmp_path,
        environ={},
        available_transports=("native",),
    )
    container.agent_platform.__dict__.update(configured.__dict__)
    return container.agent_platform


def new_agent_body(client):
    catalog = client.get("/v1/agent-platform/catalog").json()
    skill_ids = [item["id"] for item in catalog["skills"] if item["tool_ids"]]
    assert len(skill_ids) >= 2
    return {
        "name": "Research & documentation",
        "description": "Find source files and write a sourced launch brief for the team.",
        "skill_ids": skill_ids,
        "idempotency_key": "create-research-documents",
    }


def test_custom_agent_requires_login_and_csrf(client, profile_api, auth_headers):
    body = new_agent_body(client)
    assert client.post("/v1/agent-platform/agents", json=body).status_code == 403
    client.cookies.clear()
    assert client.post("/v1/agent-platform/agents", json=body).status_code == 401


def test_create_edit_reload_and_preview_combined_agent(client, profile_api, auth_headers):
    body = new_agent_body(client)
    created = client.post("/v1/agent-platform/agents", headers=auth_headers, json=body)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["profile"]["max_action"] == "write"
    assert set(record["profile"]["tool_ids"]) == {
        "native.local_file_read",
        "native.local_file_write",
    }
    assert body["description"] in record["profile"]["instructions"]
    retry = client.post("/v1/agent-platform/agents", headers=auth_headers, json=body)
    assert AgentProfileRecord.model_validate(retry.json()) == AgentProfileRecord.model_validate(
        record
    )
    catalog = client.get("/v1/agent-platform/catalog").json()
    assert [item["id"] for item in catalog["custom_agents"]] == [record["id"]]
    assert any(item["id"] == record["id"] for item in catalog["agents"])
    saved_profile = client.get("/v1/agent-platform/agents/" + record["id"]).json()
    assert AgentProfile.model_validate(saved_profile) == AgentProfile.model_validate(
        record["profile"]
    )

    edit = {
        **body,
        "name": "Brand research & reports",
        "expected_version": record["version"],
        "idempotency_key": "edit-research-documents",
    }
    updated = client.patch(
        "/v1/agent-platform/agents/" + record["id"], headers=auth_headers, json=edit
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] > record["version"]
    retry = client.patch(
        "/v1/agent-platform/agents/" + record["id"], headers=auth_headers, json=edit
    )
    assert AgentProfileRecord.model_validate(retry.json()) == AgentProfileRecord.model_validate(
        updated.json()
    )
    conflict = client.patch(
        "/v1/agent-platform/agents/" + record["id"],
        headers=auth_headers,
        json={**edit, "name": "Stale", "idempotency_key": "different-edit-key"},
    )
    assert conflict.status_code == 409
    assert client.get("/v1/agent-platform/catalog").json()["custom_agents"][0]["name"] == (
        "Brand research & reports"
    )
    preview = client.post(
        "/v1/agent-platform/agents/" + record["id"] + "/prompt-preview",
        headers=auth_headers,
        json={
            "task": {
                "id": "brief",
                "agent_id": record["id"],
                "objective": "Research and write the launch brief",
            }
        },
    )
    assert preview.status_code == 200, preview.text
    assert body["description"] in preview.text


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_ids": ["native.local_file_write"]},
        {"tool_scopes": ["admin"]},
        {"environment_ids": ["unrestricted-host"]},
        {"skill_ids": ["invented-skill"]},
        {"name": "   "},
        {"description": "  "},
        {"skill_ids": []},
    ],
)
def test_agent_builder_cannot_invent_grants(client, profile_api, auth_headers, extra):
    response = client.post(
        "/v1/agent-platform/agents", headers=auth_headers, json={**new_agent_body(client), **extra}
    )
    assert response.status_code in {403, 422}, response.text
    assert client.get("/v1/agent-platform/catalog").json()["custom_agents"] == []


def test_other_account_cannot_read_or_edit_custom_agent(client, profile_api, auth_headers):
    record = client.post(
        "/v1/agent-platform/agents", headers=auth_headers, json=new_agent_body(client)
    ).json()
    # Seed a separate owner's durable record through the trusted service, then access it
    # with the authenticated browser account. No direct SQL or fabricated auth token.
    from simon.domain.agent_profiles import CreateAgentProfile

    other = ActorContext(
        actor_id=uuid4(),
        household_id=UUID("00000000-0000-4000-8000-000000000001"),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    hidden = profile_api.agent_profiles.create(
        other,
        CreateAgentProfile(
            **{**new_agent_body(client), "idempotency_key": "other-owner-agent"},
        ),
    )
    assert client.get("/v1/agent-platform/agents/" + hidden.id).status_code == 404
    assert (
        client.patch(
            "/v1/agent-platform/agents/" + hidden.id,
            headers=auth_headers,
            json={
                **new_agent_body(client),
                "expected_version": hidden.version,
                "idempotency_key": "edit-other-owner",
            },
        ).status_code
        == 404
    )
    assert [
        item["id"] for item in client.get("/v1/agent-platform/catalog").json()["custom_agents"]
    ] == [record["id"]]
