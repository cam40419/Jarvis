"""Project membership edits use local skills and never accept client-made grant snapshots."""

import pytest

from simon.services.project_coordinator import ProjectCoordinator
from tests.integration.test_agent_profiles_api import profile_api as profile_api

READER_ID = "member-" + "c" * 32
WRITER_ID = "member-" + "d" * 32


@pytest.fixture
def member_api(client, container, profile_api, auth_headers):
    # The fixture replaces the configured service without replacing the router object.
    ProjectCoordinator(container.project_work, container.agent_runs)
    response = client.post("/v1/projects", headers=auth_headers, json={
        "name": "Individual skills", "description": "Different members, different skills.",
        "idempotency_key": "individual-project-members",
    })
    assert response.status_code == 200, response.text
    project_id = response.json()["id"]
    skills = {item["tool_ids"][0]: item["id"] for item in client.get(
        "/v1/agent-platform/catalog"
    ).json()["individual_skills"] if item["tool_ids"]}
    team = {
        "name": "Research studio", "agent_ids": [READER_ID, WRITER_ID],
        "lead_agent_id": READER_ID, "members": {
            READER_ID: {"name": "Evidence researcher", "description": "Read project sources.",
                              "skill_ids": [skills["native.local_file_read"]]},
            WRITER_ID: {"name": "Document writer", "description": "Write project reports.",
                              "skill_ids": [skills["native.local_file_write"]]},
        },
    }
    return project_id, team


def test_project_member_api_exposes_each_effective_profile(client, auth_headers, member_api):
    project_id, team = member_api
    endpoint = f"/v1/projects/{project_id}"
    anonymous_write = client.patch(endpoint + "/team", json={"expected_version": 0, "team": team})
    assert anonymous_write.status_code == 403
    response = client.patch(endpoint + "/team", headers=auth_headers,
                            json={"expected_version": 0, "team": team})
    assert response.status_code == 200, response.text
    detail = client.get(endpoint + "/command")
    assert detail.status_code == 200, detail.text
    value = detail.json()
    profiles = {entry["agent_id"]: entry for entry in value["member_profiles"]}
    assert profiles[READER_ID]["profile"]["tool_ids"] == ["native.local_file_read"]
    assert profiles[WRITER_ID]["profile"]["tool_ids"] == ["native.local_file_write"]
    assert profiles[READER_ID]["profile"]["name"] == "Evidence researcher"
    assert "member_snapshots" not in detail.text
    assert "tool_contracts" not in detail.text
    assert client.get("/v1/agent-platform/catalog").json()["custom_agents"] == []
    stale = client.patch(endpoint + "/team", headers=auth_headers,
                         json={"expected_version": 0, "team": team})
    assert stale.status_code == 409


@pytest.mark.parametrize("mutation", ["snapshot", "raw_tools", "outsider", "unknown_skill"])
def test_project_member_api_rejects_forged_members_or_grants(
    client, auth_headers, member_api, mutation,
):
    project_id, team = member_api
    body = {"expected_version": 0, "team": team}
    if mutation == "snapshot":
        body["member_snapshots"] = {READER_ID: {"tool_scopes": ["admin"]}}
    elif mutation == "raw_tools":
        team["members"][READER_ID]["tool_ids"] = ["native.local_file_write"]
    elif mutation == "outsider":
        team["members"]["someone-else"] = team["members"][READER_ID]
    else:
        team["members"][READER_ID]["skill_ids"] = ["tool.not-authorized"]
    response = client.patch(f"/v1/projects/{project_id}/team", headers=auth_headers, json=body)
    assert response.status_code in {403, 422}, response.text
    assert client.get(f"/v1/projects/{project_id}/command").json()["state"]["team"] is None
