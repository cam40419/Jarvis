from uuid import uuid4


def project(client, headers, name):
    response = client.post("/v1/projects", headers=headers, json={
        "name": name, "description": "Knowledge test", "idempotency_key": name + "-project",
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_knowledge_api_persists_edit_search_and_pin_source(client, auth_headers):
    identifier = project(client, auth_headers, "Knowledge")
    base = f"/v1/projects/{identifier}"
    initial = client.get(base + "/knowledge")
    assert initial.status_code == 200 and initial.json()["version"] == 0
    posted = client.post(base + "/activity", headers=auth_headers, json={
        "entry": {"kind": "finding", "text": "Linen meets the fabric requirement."},
        "idempotency_key": "seed-fabric-finding",
    })
    assert posted.status_code == 201, posted.text
    source = client.get(base + "/knowledge/history", params={"query": "linen"}).json()["items"][0]
    body = {"expected_version": 0, "idempotency_key": "save-fabric-brief", "brief": "Launch linen.",
            "pinned_decisions": [{"id": "fabric", "title": "Approved fabric", "text": "Use linen.",
                                  "source_activity_id": source["id"]}]}
    saved = client.patch(base + "/knowledge", headers=auth_headers, json=body)
    assert saved.status_code == 200 and saved.json()["version"] == 1
    replay = client.patch(base + "/knowledge", headers=auth_headers, json=body)
    assert replay.json() == saved.json()
    assert client.get(base + "/knowledge").json() == saved.json()
    assert client.get(base + "/knowledge/history/" + source["id"]).json() == source
    stale = client.patch(base + "/knowledge", headers=auth_headers,
                         json={**body, "idempotency_key": "different-stale-save"})
    assert stale.status_code == 409
    assert client.get(base + "/knowledge/history", params={"limit": 51}).status_code == 422
    invalid = {**body, "brief": "Unsupported null \x00", "expected_version": 1,
               "idempotency_key": "invalid-text-save"}
    assert client.patch(base + "/knowledge", headers=auth_headers, json=invalid).status_code == 422
    invalid_search = client.get(base + "/knowledge/history", params={"query": "\x00"})
    assert invalid_search.status_code in {400, 422}


def test_knowledge_api_auth_csrf_and_cross_project_provenance(client, auth_headers):
    identifier = project(client, auth_headers, "Private knowledge")
    foreign = project(client, auth_headers, "Other knowledge")
    base = f"/v1/projects/{identifier}/knowledge"
    anonymous = client.__class__(client.app, base_url="http://localhost:8000")
    with anonymous:
        assert anonymous.get(base).status_code == 401
        assert anonymous.get(base + "/history").status_code == 401
    body = {"expected_version": 0, "idempotency_key": "new-private-brief", "brief": "Saved."}
    assert client.patch(base, json=body).status_code == 403
    assert client.get(f"/v1/projects/{uuid4()}/knowledge").status_code == 404
    client.post(f"/v1/projects/{foreign}/activity", headers=auth_headers, json={
        "entry": {"kind": "decision", "text": "Other project's private decision."},
        "idempotency_key": "other-project-decision",
    })
    source = client.get(f"/v1/projects/{foreign}/knowledge/history").json()["items"][0]
    assert client.get(base + "/history/" + source["id"]).status_code == 404
    body["pinned_decisions"] = [{"id": "wrong", "title": "Wrong project", "text": "Do not pin",
                                 "source_activity_id": source["id"]}]
    assert client.patch(base, headers=auth_headers, json=body).status_code == 404
    assert client.get(base).json()["version"] == 0
