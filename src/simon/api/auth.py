import hmac
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from simon.domain.email_identity import (
    ConfirmRecoveryEmail,
    RequestResetEmail,
    ResetPasswordWithEmail,
    VerifyRecoveryEmail,
)
from simon.domain.errors import AuthenticationError, AuthorizationError, ValidationError
from simon.domain.identity import Session
from simon.domain.models import utc_now
from simon.services.email_identity import EmailIdentityService
from simon.services.identity import IdentityService, csrf_token

# Max-Age itself must be finite even when Simon does not expire the server
# session. Browsers may clamp this further, but Simon will not sign the user
# out on its own.
PERSISTENT_COOKIE_MAX_AGE = 2_147_483_647


class SecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200, repr=False)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ceremony_id: str = Field(min_length=1, max_length=200)
    credential: dict[str, Any]


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID


class PasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128, repr=False)


class PasswordSetRequest(PasswordLoginRequest):
    current_password: str | None = Field(default=None, max_length=128, repr=False)


class PasswordRegisterRequest(PasswordLoginRequest):
    token: str = Field(min_length=1, max_length=200, repr=False)


class PasswordResetRequest(PasswordRegisterRequest):
    pass


def session_cookie(identity: IdentityService) -> str:
    return "__Host-simon_session" if identity.settings.secure_cookies else "simon_session"


def ceremony_cookie(identity: IdentityService) -> str:
    return "__Host-simon_ceremony" if identity.settings.secure_cookies else "simon_ceremony"


def require_human_credentials(request: Request) -> None:
    if "authorization" in request.headers:
        raise AuthenticationError("This endpoint accepts human sessions, not bearer credentials.")


def same_origin(request: Request, identity: IdentityService) -> None:
    if request.headers.get("origin") != identity.settings.public_origin:
        raise AuthorizationError("request origin is not allowed")


def password_enabled(identity: IdentityService) -> bool:
    return identity.settings.secure_cookies or urlsplit(
        identity.settings.public_origin
    ).hostname in {"localhost", "127.0.0.1"}


def local_password_access(identity: IdentityService) -> None:
    if not password_enabled(identity):
        raise AuthorizationError("Password sign-in requires HTTPS or loopback development access.")


def require_csrf(request: Request, identity: IdentityService, token: str) -> None:
    same_origin(request, identity)
    supplied = request.headers.get("x-csrf-token", "")
    if not hmac.compare_digest(supplied.encode(), csrf_token(token).encode()):
        raise AuthorizationError("CSRF token missing or invalid")


def payload(identity: IdentityService, token: str, session: Session) -> dict[str, Any]:
    actor = identity.actor(session)
    return {
        "actor_id": str(actor.actor_id),
        "workspace_id": str(actor.workspace_id),
        "scopes": sorted(actor.scopes),
        "can_manage_accounts": actor.actor_id == identity.settings.account_admin_actor_id
        and "identity:manage" in actor.scopes,
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
        max_age=(
            PERSISTENT_COOKIE_MAX_AGE
            if identity.settings.session_hours == 0
            else max(1, int((session.expires_at - utc_now()).total_seconds()))
        ),
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


def auth_router(
    identity: IdentityService, email_identity: EmailIdentityService | None = None
) -> APIRouter:
    router = APIRouter(
        prefix="/auth", tags=["identity"], dependencies=[Depends(require_human_credentials)]
    )

    def email_service() -> EmailIdentityService:
        if email_identity is None:
            raise ValidationError("Email recovery is unavailable")
        return email_identity

    @router.post("/password/forgot")
    def forgot_password(
        body: RequestResetEmail, request: Request, tasks: BackgroundTasks
    ) -> dict[str, str]:
        same_origin(request, identity)
        service = email_service()
        service.require_delivery()
        tasks.add_task(service.request_reset, body.email)
        return {"message": "If this email belongs to an account, a reset code will arrive shortly."}

    @router.post("/password/reset-email", status_code=204)
    def reset_with_email(body: ResetPasswordWithEmail, request: Request) -> Response:
        same_origin(request, identity)
        email_service().reset(body)
        return Response(status_code=204)

    @router.get("/email")
    def recovery_email(request: Request) -> dict[str, Any]:
        session, _ = identity.resolve(request.cookies.get(session_cookie(identity), ""))
        credential = identity.store.password_for_actor(session.actor_id)
        return {
            "email": credential.email if credential else None,
            "configured": email_service().sender.configured,
        }

    @router.post("/email/start")
    def start_email_verification(body: VerifyRecoveryEmail, request: Request) -> dict[str, str]:
        token = request.cookies.get(session_cookie(identity), "")
        _, actor = identity.resolve(token)
        require_csrf(request, identity, token)
        identifier = email_service().request_verification(actor, body.email, body.current_password)
        return {"challenge_id": str(identifier)}

    @router.post("/email/verify")
    def verify_email(body: ConfirmRecoveryEmail, request: Request) -> dict[str, str]:
        token = request.cookies.get(session_cookie(identity), "")
        _, actor = identity.resolve(token)
        require_csrf(request, identity, token)
        return {"email": email_service().confirm_verification(actor, body.challenge_id, body.code)}

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
            "password_enabled": password_enabled(identity),
            "email_recovery_configured": bool(email_identity and email_identity.sender.configured),
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

    @router.post("/password/login")
    def password_login(
        body: PasswordLoginRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        local_password_access(identity)
        same_origin(request, identity)
        return set_session(
            response,
            identity,
            identity.password_login(
                body.username, body.password, request.cookies.get(session_cookie(identity))
            ),
        )

    @router.post("/password/register")
    def password_register(
        body: PasswordRegisterRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        local_password_access(identity)
        same_origin(request, identity)
        return set_session(
            response,
            identity,
            identity.register_password(
                body.token,
                body.username,
                body.password,
                request.cookies.get(session_cookie(identity)),
            ),
        )

    @router.post("/password/reset")
    def password_reset(
        body: PasswordResetRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        local_password_access(identity)
        same_origin(request, identity)
        return set_session(
            response,
            identity,
            identity.reset_password(
                body.token,
                body.username,
                body.password,
                request.cookies.get(session_cookie(identity)),
            ),
        )

    @router.get("/password")
    def password_status(request: Request) -> dict[str, str | None]:
        local_password_access(identity)
        token = request.cookies.get(session_cookie(identity), "")
        return {"username": identity.password_username(token)}

    @router.post("/password")
    def set_password(body: PasswordSetRequest, request: Request) -> dict[str, str]:
        local_password_access(identity)
        token = request.cookies.get(session_cookie(identity), "")
        require_csrf(request, identity, token)
        return {
            "username": identity.set_password(
                token, body.username, body.password, body.current_password
            )
        }

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

    @router.post("/workspace")
    def switch_workspace(
        body: WorkspaceRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        token = request.cookies.get(session_cookie(identity), "")
        identity.resolve(token)
        require_csrf(request, identity, token)
        return set_session(response, identity, identity.switch_workspace(token, body.workspace_id))

    return router
