from uuid import uuid4

import pytest

from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership


def test_conversation_api_and_sse(client, auth_headers, container):
    body = {"title": "API test", "idempotency_key": "api-thread-001"}
    assert client.post("/v1/threads", json=body).status_code == 403
    thread = client.post("/v1/threads", json=body, headers=auth_headers).json()
    base = f"/v1/threads/{thread['id']}"
    assert client.get(base).json() == thread
    assert client.get("/v1/threads").json() == [thread]
    request = {"text": "Hello\n世界", "idempotency_key": "api-run-001"}
    response = client.post(base + "/runs", json=request, headers=auth_headers)
    assert response.status_code == 201
    run = response.json()
    assert client.get(f"/v1/runs/{run['id']}").json() == run
    events = f"/v1/runs/{run['id']}/events"
    stream = client.get(events)
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text.count("id: ") == 4
    resumed = client.get(events + "?after=0", headers={"Last-Event-ID": "2"})
    assert "id: 2\n" not in resumed.text
    assert "id: 3\n" in resumed.text and "id: 4\n" in resumed.text
    assert client.get(events, headers={"Last-Event-ID": "4"}).status_code == 204
    for cursor in ["-1", "bogus", "999"]:
        assert client.get(events, headers={"Last-Event-ID": cursor}).status_code == 422
    assert client.post(base + "/runs", json=request, headers=auth_headers).json() == run
    assert len(client.get(base + "/messages").json()) == 2
    assert len(client.get(base + "/messages?after=1&limit=1").json()) == 1
    assert client.get("/v1/threads?limit=101").status_code == 422
    assert client.get(f"/v1/threads/{uuid4()}").status_code == 404
    assert client.get("/chat").status_code == 200
    assert "default-src 'self'" in client.get("/chat").headers["content-security-policy"]
    container.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, household_id=DEV_HOUSEHOLD_ID, role="guest")
    )
    assert client.get(events).status_code == 403
    assert client.get(base + "/messages").status_code == 403
    client.cookies.clear()
    assert client.get(events).status_code == 401


@pytest.mark.postgres
def test_database_prevents_snapshot_rewrites(postgres_url):
    import psycopg

    from jarvis.adapters.postgres import PostgresStore
    from jarvis.seed import seed_development_identity
    from tests.contract.test_conversations import actor, create, service, submit

    seed_development_identity(postgres_url)
    svc = service.__wrapped__(PostgresStore(postgres_url))
    who = actor.__wrapped__()
    thread = create(svc, who)
    run = submit(svc, who, thread)
    for statement, value in [
        ("UPDATE runs SET snapshot = '{}' WHERE id = %s", run.id),
        ("DELETE FROM messages WHERE id = %s", run.input_message_id),
        ("UPDATE run_events SET event_type = 'changed' WHERE run_id = %s", run.id),
    ]:
        with (
            psycopg.connect(postgres_url) as connection,
            pytest.raises(psycopg.errors.RaiseException, match="append-only"),
        ):
            connection.execute(statement, (value,))
