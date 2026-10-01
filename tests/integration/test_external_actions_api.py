from uuid import uuid4

import httpx

from simon.adapters.external_action_providers import ExternalActionProviders
from simon.adapters.optional_http import BoundedHTTP
from tests.unit.test_external_actions import SECRET, call_draft, call_response, phone


def test_external_review_requires_authentication_and_user_csrf(client, auth_headers):
    anonymous = client.__class__(client.app, base_url="http://localhost:8000")
    with anonymous:
        assert anonymous.get("/v1/external-actions").status_code == 401
    body = {"draft": call_draft().model_dump(mode="json"), "idempotency_key": "api-review-request"}
    assert client.post("/v1/external-actions", json=body).status_code == 403
    created = client.post("/v1/external-actions", json=body, headers=auth_headers)
    assert created.status_code == 201, created.text
    action = created.json()
    assert action["status"] == "pending" and action["blockers"]
    assert client.get("/v1/external-actions").json()[0]["id"] == action["id"]
    assert client.get("/v1/external-actions/" + str(uuid4())).status_code == 404
    cancelled = client.post(
        "/v1/external-actions/" + action["id"] + "/cancel",
        json={"review_digest": action["review_digest"]},
        headers=auth_headers,
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_review_api_confirms_exact_details_only_once(client, container, auth_headers):
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(201, json=call_response())

    service = container.external_actions
    service.definitions = {"phone": phone()}
    service.providers = ExternalActionProviders(
        BoundedHTTP(
            transport=httpx.MockTransport(send),
            environ={"PHONE_SECRET": SECRET},
        )
    )
    body = {
        "draft": call_draft().model_dump(mode="json"),
        "idempotency_key": "confirmed-api-request",
    }
    action = client.post("/v1/external-actions", json=body, headers=auth_headers).json()
    url = "/v1/external-actions/" + action["id"] + "/confirm"
    assert not requests
    assert (
        client.post(url, json={"review_digest": "0" * 64}, headers=auth_headers).status_code == 409
    )
    assert not requests
    approved = client.post(
        url, json={"review_digest": action["review_digest"]}, headers=auth_headers
    )
    assert approved.status_code == 200 and approved.json()["status"] == "accepted"
    assert (
        client.post(
            url, json={"review_digest": action["review_digest"]}, headers=auth_headers
        ).json()["status"]
        == "accepted"
    )
    assert len(requests) == 1
    assert client.get("/v1/external-actions/providers").json()[0]["available"] is True
    assert SECRET not in client.get("/v1/external-actions").text


def test_unconfigured_quote_is_an_honest_blocker(client, auth_headers):
    response = client.post(
        "/v1/external-actions/quotes",
        headers=auth_headers,
        json={"provider_id": "unconfigured", "kind": "purchase", "recipient_id": "merchant"},
    )
    assert response.status_code in {400, 409, 422}
    assert "configured" in response.text
