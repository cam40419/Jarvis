"""Authenticated history summaries link to existing run/artifact authorization."""

from uuid import UUID, uuid4

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.services.agent_platform import AgentPlatformService
from tests.contract.test_project_history import PRIVATE_TEXT, saved_run


def test_history_authentication_and_empty_project(client, auth_headers):
    anonymous = client.__class__(client.app, base_url="http://localhost:8000")
    with anonymous:
        assert anonymous.get(f"/v1/projects/{uuid4()}/runs").status_code == 401
    created = client.post(
        "/v1/projects",
        headers=auth_headers,
        json={
            "name": "History",
            "description": "Review older work",
            "idempotency_key": "history-api",
        },
    )
    identifier = created.json()["id"]
    response = client.get(f"/v1/projects/{identifier}/runs")
    assert response.status_code == 200
    assert response.json() == {"project_id": identifier, "items": [], "next_cursor": None}
    assert client.get(f"/v1/projects/{uuid4()}/runs").status_code == 404
    assert client.get(f"/v1/projects/{identifier}/runs?limit=51").status_code == 422
    assert client.get(f"/v1/projects/{identifier}/runs?cursor=bad!!").status_code in {400, 422}


def test_history_pages_open_exact_run_and_download_artifact(
    client,
    container,
    auth_headers,
    tmp_path,
):
    team = TeamTemplate(id="studio", name="Studio", agent_ids=("writer",))
    configured = AgentPlatformService(
        container.store,
        PlatformManifest(
            agents=(AgentProfile(id="writer", instructions="Write."),),
            teams=(team,),
        ),
        state_dir=tmp_path,
        environ={},
    )
    container.agent_platform.__dict__.update(configured.__dict__)
    container.agent_platform.project_team_resolver = lambda *_: team
    identifiers = []
    for number in (1, 2):
        response = client.post(
            "/v1/projects",
            headers=auth_headers,
            json={
                "name": f"History {number}",
                "description": "Review work",
                "idempotency_key": f"history-linked-{number}",
            },
        )
        identifiers.append(UUID(response.json()["id"]))
    for number in (1, 2, 3):
        saved_run(container.store, identifiers[0], number=number, state_dir=tmp_path)
    saved_run(container.store, identifiers[1], number=4)
    base = f"/v1/projects/{identifiers[0]}/runs"
    first = client.get(base, params={"limit": 2})
    assert first.status_code == 200, first.text
    assert PRIVATE_TEXT not in first.text and "sha256" not in first.text
    page = first.json()
    assert [UUID(row["id"]).int for row in page["items"]] == [3, 2]
    second = client.get(base, params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [UUID(row["id"]).int for row in second["items"]] == [1]
    assert second["next_cursor"] is None
    mismatch = client.get(
        f"/v1/projects/{identifiers[1]}/runs",
        params={
            "cursor": page["next_cursor"],
        },
    )
    assert mismatch.status_code in {400, 422}
    item = page["items"][0]
    detail = client.get(item["run_url"])
    assert detail.status_code == 200 and detail.json()["id"] == item["id"]
    assert client.get(item["plan_url"]).json()["id"] == item["plan_id"]
    artifact = detail.json()["tasks"][0]["artifacts"][0]
    download = client.get(item["run_url"] + "/artifacts/" + artifact["id"])
    assert download.status_code == 200 and download.content == b"Result"
    assert client.get(item["run_url"] + "/artifacts/" + str(uuid4())).status_code == 404
