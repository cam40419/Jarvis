import hmac
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.errors import AuthorizationError
from jarvis.domain.identity import Session
from jarvis.domain.models import utc_now
from jarvis.services.identity import IdentityService, csrf_token


class SecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200, repr=False)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ceremony_id: str = Field(min_length=1, max_length=200)
    credential: dict[str, Any]


class HouseholdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    household_id: UUID


def session_cookie(identity: IdentityService) -> str:
    return "__Host-jarvis_session" if identity.settings.secure_cookies else "jarvis_session"


def ceremony_cookie(identity: IdentityService) -> str:
    return "__Host-jarvis_ceremony" if identity.settings.secure_cookies else "jarvis_ceremony"


def same_origin(request: Request, identity: IdentityService) -> None:
    if request.headers.get("origin") != identity.settings.public_origin:
        raise AuthorizationError("request origin is not allowed")


def require_csrf(request: Request, identity: IdentityService, token: str) -> None:
    same_origin(request, identity)
    supplied = request.headers.get("x-csrf-token", "")
    if not hmac.compare_digest(supplied.encode(), csrf_token(token).encode()):
        raise AuthorizationError("CSRF token missing or invalid")


def payload(identity: IdentityService, token: str, session: Session) -> dict[str, Any]:
    actor = identity.actor(session)
    return {
        "actor_id": str(actor.actor_id),
        "household_id": str(actor.household_id),
        "scopes": sorted(actor.scopes),
        "method": session.method,
        "expires_at": session.expires_at.isoformat(),
        "csrf_token": csrf_token(token),
        "memberships": [
            m.model_dump(mode="json") for m in identity.store.memberships(actor.actor_id)
        ],
    }


def set_session(
    response: Response, identity: IdentityService, issued: tuple[str, Session]
) -> dict[str, Any]:
    token, session = issued
    response.set_cookie(
        session_cookie(identity),
        token,
        max_age=max(1, int((session.expires_at - utc_now()).total_seconds())),
        httponly=True,
        secure=identity.settings.secure_cookies,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(
        ceremony_cookie(identity),
        path="/",
        secure=identity.settings.secure_cookies,
        httponly=True,
        samesite="strict",
    )
    return payload(identity, token, session)


def auth_router(identity: IdentityService) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["identity"])

    def begin(
        request: Request, response: Response, options: tuple[dict[str, Any], str]
    ) -> dict[str, Any]:
        value, binding = options
        response.set_cookie(
            ceremony_cookie(identity),
            binding,
            max_age=300,
            httponly=True,
            secure=identity.settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        return value

    @router.get("/config")
    def config() -> dict[str, Any]:
        return {
            "dev_login_enabled": identity.settings.dev_login_enabled,
            "origin": identity.settings.public_origin,
        }

    @router.get("/session")
    def session_info(request: Request) -> dict[str, Any]:
        token = request.cookies.get(session_cookie(identity), "")
        session, _actor = identity.resolve(token)
        return payload(identity, token, session)

    @router.post("/dev-login")
    def dev_login(body: SecretRequest, request: Request, response: Response) -> dict[str, Any]:
        same_origin(request, identity)
        return set_session(
            response,
            identity,
            identity.development_login(body.token, request.cookies.get(session_cookie(identity))),
        )

    @router.post("/passkeys/register/options")
    def registration_options(
        body: SecretRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        same_origin(request, identity)
        return begin(request, response, identity.registration_options(body.token))

    @router.post("/passkeys/register/verify")
    def register(body: VerifyRequest, request: Request, response: Response) -> dict[str, Any]:
        same_origin(request, identity)
        return set_session(
            response,
            identity,
            identity.register(
                body.ceremony_id,
                request.cookies.get(ceremony_cookie(identity)),
                body.credential,
                request.cookies.get(session_cookie(identity)),
            ),
        )

    @router.post("/passkeys/login/options")
    def authentication_options(request: Request, response: Response) -> dict[str, Any]:
        same_origin(request, identity)
        return begin(request, response, identity.authentication_options())

    @router.post("/passkeys/login/verify")
    def authenticate(body: VerifyRequest, request: Request, response: Response) -> dict[str, Any]:
        same_origin(request, identity)
        return set_session(
            response,
            identity,
            identity.authenticate(
                body.ceremony_id,
                request.cookies.get(ceremony_cookie(identity)),
                body.credential,
                request.cookies.get(session_cookie(identity)),
            ),
        )

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response) -> None:
        token = request.cookies.get(session_cookie(identity), "")
        identity.resolve(token)
        require_csrf(request, identity, token)
        identity.logout(token)
        response.delete_cookie(
            session_cookie(identity),
            path="/",
            secure=identity.settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )

    @router.post("/household")
    def switch_household(
        body: HouseholdRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        token = request.cookies.get(session_cookie(identity), "")
        identity.resolve(token)
        require_csrf(request, identity, token)
        return set_session(response, identity, identity.switch_household(token, body.household_id))

    return router
