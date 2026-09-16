import base64
import json
from email.message import EmailMessage
from email.policy import SMTP
from time import monotonic, time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from simon.config import Settings
from simon.domain.connected_tools import ActionProposal, CalendarQuery
from simon.domain.errors import DomainError

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
EMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = ("openid", "email", CALENDAR_SCOPE, EMAIL_SCOPE)
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class ConnectedError(DomainError):
    code = "connected_error"

    def __init__(self, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown


class GoogleTokens(BaseModel):
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)
    expires_at: float


class GoogleAPI:
    """Fixed Google endpoints; no retries, redirects, or provider error-body logging."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        data: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        write: bool = False,
    ) -> dict[str, Any]:
        try:
            started = monotonic()
            with (
                httpx.Client(timeout=20, follow_redirects=False) as client,
                client.stream(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    data=data,
                    json=body,
                    params=params,
                ) as response,
            ):
                if not 200 <= response.status_code < 300:
                    raise ConnectedError(
                        "Google rejected the request. Check the connection and API setup.",
                        unknown=write
                        and (response.status_code >= 500 or response.status_code == 408),
                    )
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 200000 or monotonic() - started > 20:
                        raise ConnectedError(
                            "Google returned an oversized response.", unknown=write
                        )
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("object required")
                return result
        except httpx.HTTPError:
            raise ConnectedError(
                "Google could not be reached. Check your connection.", unknown=write
            ) from None
        except (ValueError, TypeError):
            raise ConnectedError("Google returned an unexpected response.", unknown=write) from None

    def exchange(
        self, code: str, verifier: str, redirect_uri: str
    ) -> tuple[GoogleTokens, tuple[str, ...]]:
        assert self.settings.google_client_secret
        result = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret.get_secret_value(),
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        return self._tokens(result), tuple(str(result.get("scope", "")).split())

    @staticmethod
    def _tokens(result: dict[str, Any], refresh_token: str = "") -> GoogleTokens:
        try:
            return GoogleTokens(
                access_token=result.get("access_token", ""),
                refresh_token=result.get("refresh_token", refresh_token),
                expires_at=time() + min(int(result.get("expires_in", 3600)), 86400),
            )
        except (ValueError, TypeError):
            raise ConnectedError(
                "Google authorization is incomplete. Connect the account again."
            ) from None

    def refresh(self, tokens: GoogleTokens) -> GoogleTokens:
        assert self.settings.google_client_secret
        result = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret.get_secret_value(),
                "refresh_token": tokens.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        return self._tokens(result, tokens.refresh_token)

    def account_email(self, token: str) -> str:
        result = self._request(
            "GET", "https://openidconnect.googleapis.com/v1/userinfo", token=token
        )
        email = result.get("email")
        if not isinstance(email, str) or not result.get("email_verified") or len(email) > 254:
            raise ConnectedError("Google did not provide a verified account address.")
        return email

    def events(self, token: str, query: CalendarQuery) -> dict[str, Any]:
        result = self._request(
            "GET",
            CALENDAR_URL,
            token=token,
            params={
                "timeMin": query.start.isoformat(),
                "timeMax": query.end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "25",
                "fields": "items(summary,start,end,location,status),nextPageToken,timeZone",
            },
        )
        return {
            "events": [
                {
                    "title": str(item.get("summary", "Busy"))[:200],
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "location": str(item.get("location", ""))[:500],
                }
                for item in result.get("items", [])[:25]
                if item.get("status") != "cancelled"
            ],
            "more_events": bool(result.get("nextPageToken")),
            "timezone": result.get("timeZone"),
        }

    def execute(self, token: str, action: ActionProposal, sender: str) -> tuple[str, str | None]:
        if action.kind == "calendar.create":
            draft = action.calendar
            assert draft is not None
            result = self._request(
                "POST",
                CALENDAR_URL,
                token=token,
                write=True,
                body={
                    "id": action.id.hex,
                    "summary": draft.title,
                    "description": draft.description,
                    "location": draft.location,
                    "start": {"dateTime": draft.start.isoformat()},
                    "end": {"dateTime": draft.end.isoformat()},
                },
            )
            link = result.get("htmlLink")
            # Receipts link only to the expected Google application.
            url = (
                link
                if isinstance(link, str) and link.startswith("https://www.google.com/calendar/")
                else None
            )
        else:
            email = action.email
            assert email is not None
            message = EmailMessage(policy=SMTP)
            message["To"] = email.to
            message["From"] = sender
            message["Subject"] = email.subject
            message["Message-ID"] = f"<{action.id.hex}@simon.local>"
            message.set_content(email.body)
            result = self._request(
                "POST",
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                token=token,
                write=True,
                body={
                    "raw": base64.urlsafe_b64encode(message.as_bytes()).decode(),
                },
            )
            url = "https://mail.google.com/mail/u/0/#sent"
        identifier = result.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ConnectedError(
                "Google did not return a receipt. Check Google before trying again.", unknown=True
            )
        return identifier, url
