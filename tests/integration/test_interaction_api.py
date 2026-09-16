from uuid import UUID

import pytest

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext, Channel
from tests.contract.test_interaction import feedback_request, preference_request


def test_preferences_and_feedback_api(client, container, auth_headers):
    assert client.get("/v1/preferences").json()["version"] == 0
    body = preference_request().model_dump(mode="json")
    assert client.post("/v1/preferences", json=body).status_code == 403
    saved = client.post("/v1/preferences", headers=auth_headers, json=body)
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert client.get("/v1/preferences").json() == saved.json()
    assert (
        client.post(
            "/v1/preferences", headers=auth_headers, json={**body, "actor_id": "injected"}
        ).status_code
        == 422
    )
    stale = client.post(
        "/v1/preferences", headers=auth_headers, json={**body, "idempotency_key": "stale-settings"}
    )
    assert stale.status_code == 409
    thread = client.post(
        "/v1/threads",
        headers=auth_headers,
        json={"title": "Feedback", "idempotency_key": "feedback-thread"},
    ).json()
    client.post(
        "/v1/memories",
        headers=auth_headers,
        json={
            "subject": "Preference",
            "content": "Vegetarian dinners",
            "idempotency_key": "feedback-memory",
        },
    )
    run = client.post(
        f"/v1/threads/{thread['id']}/runs",
        headers=auth_headers,
        json={"text": "Plan dinner", "idempotency_key": "feedback-question"},
    ).json()
    path = f"/v1/runs/{run['id']}/feedback"
    rating = feedback_request().model_dump(mode="json")
    assert client.post(path, json=rating).status_code == 403
    response = client.post(path, headers=auth_headers, json=rating)
    assert response.status_code == 200 and response.json()["rating"] == "helpful"
    assert client.post(path, headers=auth_headers, json=rating).json() == response.json()
    answers = client.get(f"/v1/threads/{thread['id']}/answers").json()
    assert answers == [
        {
            "run_id": run["id"],
            "output_message_id": run["output_message_id"],
            "feedback": response.json(),
            "web_sources": [],
            "actions": [],
            "home_commands": [],
        }
    ]
    assert client.get(f"/v1/threads/{thread['id']}/answers?limit=101").status_code == 422
    assert (
        client.post(
            path, headers=auth_headers, json={**rating, "rating": "unsafe_value"}
        ).status_code
        == 422
    )
    assert client.get(f"/v1/runs/{run['id']}").json() == run
    actor = ActorContext(
        actor_id=UUID(run["actor_id"]),
        household_id=UUID(thread["household_id"]),
        channel=Channel.API,
        scopes=frozenset({"threads:read", "threads:write"}),
    )
    with pytest.raises(AuthorizationError):
        container.interaction.answers(actor, UUID(thread["id"]))


def test_interaction_requires_session(client):
    assert client.get("/v1/preferences").status_code == 401
