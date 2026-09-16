import base64
import json
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from simon.adapters.google import (
    CALENDAR_SCOPE,
    EMAIL_SCOPE,
    ConnectedError,
    GoogleAPI,
    GoogleTokens,
)
from simon.config import Settings
from simon.domain.connected_tools import ActionProposal, CalendarDraft, CalendarQuery, EmailDraft
from tests.contract.test_connected import EMAIL, EVENT


def adapter(monkeypatch, respond):
    client = httpx.Client

    def factory(**kwargs):
        assert kwargs == {"timeout": 20, "follow_redirects": False}
        return client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("simon.adapters.google.httpx.Client", factory)
    return GoogleAPI(
        Settings(_env_file=None, google_client_id="test", google_client_secret=SecretStr("secret"))
    )


def proposal(kind):
    return ActionProposal(
        actor_id=uuid4(),
        household_id=uuid4(),
        run_id=uuid4(),
        connection_id=uuid4(),
        account_email="sender@example.com",
        kind=kind,
        email=EmailDraft(**EMAIL) if kind == "email.send" else None,
        calendar=CalendarDraft(**EVENT) if kind == "calendar.create" else None,
    )


def test_google_token_exchange_refresh_account_and_read(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/token":
            assert b"client_secret=secret" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "scope": CALENDAR_SCOPE + " " + EMAIL_SCOPE,
                },
            )
        assert request.headers["Authorization"] == "Bearer access"
        if request.url.path.endswith("userinfo"):
            return httpx.Response(200, json={"email": "test@example.com", "email_verified": True})
        assert request.method == "GET" and request.url.params["maxResults"] == "25"
        return httpx.Response(
            200,
            json={
                "items": [{"summary": "Lunch", "start": {}, "end": {}}, {"status": "cancelled"}],
                "nextPageToken": "next",
                "timeZone": "America/New_York",
            },
        )

    api = adapter(monkeypatch, respond)
    tokens, scopes = api.exchange("code", "verifier", "http://localhost:8000/auth/google/callback")
    assert scopes == (CALENDAR_SCOPE, EMAIL_SCOPE)
    assert api.refresh(tokens).refresh_token == "refresh"
    assert api.account_email(tokens.access_token) == "test@example.com"
    result = api.events(tokens.access_token, CalendarQuery(start=EVENT["start"], end=EVENT["end"]))
    assert len(result["events"]) == 1 and result["more_events"]
    assert len(calls) == 4


@pytest.mark.parametrize("kind", ["email.send", "calendar.create"])
def test_exact_email_or_event_payload(monkeypatch, kind):
    action = proposal(kind)

    def respond(request):
        assert request.method == "POST" and request.headers["Authorization"] == "Bearer access"
        body = json.loads(request.content)
        if kind == "email.send":
            message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(body["raw"])
            )
            assert (
                str(request.url) == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
            )
            assert message["To"] == EMAIL["to"] and message["From"] == "sender@example.com"
            assert (
                message["Subject"] == EMAIL["subject"]
                and message.get_content().strip() == EMAIL["body"]
            )
            assert not message["Bcc"] and not message["Cc"]
        else:
            assert body["id"] == action.id.hex and body["summary"] == EVENT["title"]
            assert body["start"]["dateTime"] == EVENT["start"] and "attendees" not in body
        return httpx.Response(
            200, json={"id": "receipt", "htmlLink": "https://www.google.com/calendar/event"}
        )

    assert (
        adapter(monkeypatch, respond).execute("access", action, "sender@example.com")[0]
        == "receipt"
    )


@pytest.mark.parametrize(
    "failure,unknown",
    [
        (401, False),
        (429, False),
        (500, True),
        (408, True),
        (302, False),
        ("timeout", True),
        ("json", True),
        ("large", True),
        ("list", True),
        ("receipt", True),
    ],
)
def test_google_errors_are_bounded_sanitized_and_not_retried(monkeypatch, failure, unknown):
    calls = []

    def respond(request):
        calls.append(1)
        if isinstance(failure, int):
            return httpx.Response(failure, text="private provider details")
        if failure == "timeout":
            raise httpx.ReadTimeout("private provider details")
        if failure == "json":
            return httpx.Response(200, text="private provider details")
        if failure == "large":
            return httpx.Response(200, text="x" * 200001)
        return httpx.Response(200, json=[] if failure == "list" else {})

    with pytest.raises(ConnectedError) as error:
        adapter(monkeypatch, respond).execute(
            "access", proposal("email.send"), "sender@example.com"
        )
    assert error.value.unknown == unknown
    assert "private provider details" not in str(error.value) and len(calls) == 1


def test_missing_tokens_identity_and_invalid_configuration(monkeypatch):
    with pytest.raises(ConnectedError):
        GoogleAPI._tokens({})
    with pytest.raises(ConnectedError):
        GoogleAPI._tokens({"expires_in": "invalid"})
    assert GoogleAPI._tokens({"access_token": "a"}, "r").refresh_token == "r"
    assert "secret" not in repr(
        GoogleTokens(access_token="secret", refresh_token="secret", expires_at=0)
    )
    api = adapter(monkeypatch, lambda req: httpx.Response(200, json={"email_verified": False}))
    with pytest.raises(ConnectedError):
        api.account_email("access")
    with pytest.raises(ValidationError, match="Fernet key"):
        Settings(_env_file=None, google_token_key=SecretStr("not-a-key"))


@pytest.mark.parametrize(
    "change",
    [
        {"end": "2026-10-01T11:00:00-04:00"},
        {"end": "2026-12-01T13:00:00-04:00"},
        {"start": "2026-10-01T12:00:00"},
    ],
)
def test_invalid_calendar_times_rejected(change):
    with pytest.raises(ValidationError):
        CalendarDraft(**{**EVENT, **change})
