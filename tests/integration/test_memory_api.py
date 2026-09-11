from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership


def test_memory_api_csrf_scopes_and_context(client, auth_headers, container):
    body = {"subject": "Dinner", "content": "Vegetarian", "idempotency_key": "api-memory-001"}
    assert client.post("/v1/memories", json=body).status_code == 403
    response = client.post("/v1/memories", json=body, headers=auth_headers)
    assert response.status_code == 201
    memory = response.json()
    assert client.get("/v1/memories").json() == [memory]
    assert client.get("/v1/memories?offset=1&limit=1").json() == []
    assert (
        client.post(
            "/v1/memories", json={**body, "scope": "private"}, headers=auth_headers
        ).status_code
        == 422
    )
    path = f"/v1/memories/{memory['id']}/retract"
    assert client.post(path).status_code == 403
    assert client.post(path, headers=auth_headers).json()["accepted"] is False
    assert client.get("/v1/memories").json() == []
    container.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, household_id=DEV_HOUSEHOLD_ID, role="guest")
    )
    assert client.get("/v1/memories").status_code == 403
    assert client.post("/v1/memories", json=body, headers=auth_headers).status_code == 403
    client.cookies.clear()
    assert client.get("/v1/memories").status_code == 401
