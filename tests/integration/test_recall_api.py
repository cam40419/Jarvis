from uuid import uuid4


def test_context_api_recalls_persisted_messages_and_personal_memories(client, auth_headers):
    thread = client.post(
        "/v1/threads",
        headers=auth_headers,
        json={"title": "Printer project", "idempotency_key": str(uuid4())},
    ).json()
    assert thread["visibility"] == "personal"
    assert (
        client.post(
            f"/v1/threads/{thread['id']}/runs",
            headers=auth_headers,
            json={"text": "My Atlas project uses a Bambu A1.", "idempotency_key": str(uuid4())},
        ).status_code
        == 201
    )
    memory = client.post(
        "/v1/memories",
        headers=auth_headers,
        json={
            "subject": "Atlas",
            "content": "Bambu A1 plate changer",
            "scope": "personal",
            "category": "project",
            "idempotency_key": str(uuid4()),
        },
    ).json()
    response = client.get("/v1/context/search", params={"query": "Atlas"})
    assert response.status_code == 200
    assert response.json()["memories"][0]["id"] == memory["id"]
    assert "Bambu A1" in str(response.json()["conversations"])
    assert client.get("/v1/context/search?offset=-1").status_code == 422
    assert client.get("/v1/context/search", params={"query": "x" * 201}).status_code == 422
    client.cookies.clear()
    assert client.get("/v1/context/search").status_code == 401
