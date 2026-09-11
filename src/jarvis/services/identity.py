from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from jarvis.config import Settings
from jarvis.domain.errors import AuthenticationError, AuthorizationError
from jarvis.domain.identity import (
    DEV_ACTOR_ID,
    DEV_HOUSEHOLD_ID,
    Challenge,
    Enrollment,
    Membership,
    Passkey,
    Session,
)
from jarvis.domain.models import ActorContext, Channel, utc_now
from jarvis.domain.ports import Store
from jarvis.services.audit import AuditService

# One initial transaction lock for identity state, shared across API processes.
IDENTITY_LOCK = UUID("00000000-0000-4000-8000-000000000001")
ROLE_SCOPES = {
    "owner": frozenset(
        {
            "system:read",
            "jobs:read",
            "jobs:write",
            "identity:manage",
            "threads:read",
            "threads:write",
            "memories:read",
            "memories:write",
            "memories:manage",
        }
    ),
    "member": frozenset(
        {
            "system:read",
            "jobs:read",
            "jobs:write",
            "threads:read",
            "threads:write",
            "memories:read",
            "memories:write",
        }
    ),
    "guest": frozenset({"system:read"}),
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"jarvis-csrf-v1", hashlib.sha256).hexdigest()


def validate_client_context(credential: dict[str, Any]) -> None:
    response = credential.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("clientDataJSON"), str):
        raise AuthenticationError("passkey verification failed")
    data = json.loads(base64url_to_bytes(response["clientDataJSON"]))
    if (
        not isinstance(data, dict)
        or data.get("crossOrigin", False) is not False
        or "topOrigin" in data
    ):
        raise AuthenticationError("passkey verification failed")


class IdentityService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.audit = AuditService(store)

    def membership(self, actor_id: UUID, household_id: UUID) -> Membership:
        for membership in self.store.memberships(actor_id):
            if membership.household_id == household_id:
                return membership
        raise AuthorizationError("household membership required")

    def actor(self, session: Session) -> ActorContext:
        membership = self.membership(session.actor_id, session.household_id)
        return ActorContext(
            actor_id=session.actor_id,
            household_id=session.household_id,
            scopes=ROLE_SCOPES[membership.role],
            channel=Channel.API,
        )

    def resolve(self, token: str | None) -> tuple[Session, ActorContext]:
        if not token or len(token) > 200:
            raise AuthenticationError("sign in required")
        with self.store.transaction(IDENTITY_LOCK):
            session = self.store.get_session(token_hash(token))
            if session is None or session.expires_at <= utc_now():
                raise AuthenticationError("session expired or invalid")
            if session.method == "development" and not self.settings.dev_login_enabled:
                raise AuthenticationError("development sessions are disabled")
            return session, self.actor(session)

    def _issue(
        self,
        actor_id: UUID,
        household_id: UUID,
        method: Literal["passkey", "development"],
        previous_token: str | None = None,
        previous_session: Session | None = None,
    ) -> tuple[str, Session]:
        self.membership(actor_id, household_id)
        token = secrets.token_urlsafe(32)
        session = Session(
            token_hash=token_hash(token),
            actor_id=actor_id,
            household_id=household_id,
            method=method,
            expires_at=utc_now() + timedelta(hours=self.settings.session_hours),
        )
        if previous_session is not None:
            session = session.model_copy(
                update={
                    "created_at": previous_session.created_at,
                    "expires_at": previous_session.expires_at,
                }
            )
        if previous_token:
            self.store.delete_session(token_hash(previous_token))
        self.store.save_session(session)
        self.audit.record(
            event_type="identity.session_created",
            actor=self.actor(session),
            resource_type="user",
            resource_id=str(actor_id),
            payload={"method": method},
        )
        return token, session

    def development_login(self, secret: str, previous_token: str | None) -> tuple[str, Session]:
        configured = self.settings.dev_login_token
        if (
            not self.settings.dev_login_enabled
            or configured is None
            or not hmac.compare_digest(secret.encode(), configured.get_secret_value().encode())
        ):
            raise AuthenticationError("development login unavailable or token invalid")
        with self.store.transaction(IDENTITY_LOCK):
            return self._issue(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, "development", previous_token)

    def logout(self, token: str) -> None:
        with self.store.transaction(IDENTITY_LOCK):
            session, actor = self.resolve(token)
            self.store.delete_session(session.token_hash)
            self.audit.record(
                event_type="identity.session_revoked",
                actor=actor,
                resource_type="user",
                resource_id=str(actor.actor_id),
                payload={},
            )

    def switch_household(self, token: str, household_id: UUID) -> tuple[str, Session]:
        with self.store.transaction(IDENTITY_LOCK):
            session, _actor = self.resolve(token)
            return self._issue(session.actor_id, household_id, session.method, token, session)

    def enroll(self, actor_id: UUID, household_id: UUID) -> str:
        with self.store.transaction(IDENTITY_LOCK):
            self.membership(actor_id, household_id)
            secret = secrets.token_urlsafe(32)
            self.store.save_enrollment(
                Enrollment(
                    token_hash=token_hash(secret),
                    actor_id=actor_id,
                    household_id=household_id,
                    expires_at=utc_now() + timedelta(minutes=15),
                )
            )
            self.operator_audit("identity.enrollment_issued", actor_id, household_id)
            return secret

    def operator_audit(self, event_type: str, actor_id: UUID, household_id: UUID) -> None:
        self.audit.record(
            event_type=event_type,
            actor=ActorContext(
                actor_id=UUID("00000000-0000-4000-8000-000000000002"),
                household_id=household_id,
                channel=Channel.API,
            ),
            resource_type="user",
            resource_id=str(actor_id),
            payload={"authority": "local_operator", "subject_actor_id": str(actor_id)},
        )

    def _enrollment(self, hashed_token: str) -> Enrollment:
        enrollment = self.store.get_enrollment(hashed_token)
        if enrollment is None or enrollment.expires_at <= utc_now():
            raise AuthenticationError("enrollment token expired or invalid")
        self.membership(enrollment.actor_id, enrollment.household_id)
        return enrollment

    def registration_options(self, enrollment_token: str) -> tuple[dict[str, Any], str]:
        with self.store.transaction(IDENTITY_LOCK):
            enrollment = self._enrollment(token_hash(enrollment_token))
            membership = self.membership(enrollment.actor_id, enrollment.household_id)
            options = generate_registration_options(
                rp_id=self.settings.rp_id,
                rp_name="Jarvis",
                user_id=enrollment.actor_id.bytes,
                user_name=str(enrollment.actor_id),
                user_display_name=membership.display_name,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
                exclude_credentials=[
                    PublicKeyCredentialDescriptor(id=base64url_to_bytes(key.credential_id))
                    for key in self.store.passkeys(enrollment.actor_id)
                ],
            )
            return self._challenge(json.loads(options_to_json(options)), "registration", enrollment)

    def authentication_options(self) -> tuple[dict[str, Any], str]:
        options = generate_authentication_options(
            rp_id=self.settings.rp_id, user_verification=UserVerificationRequirement.REQUIRED
        )
        with self.store.transaction(IDENTITY_LOCK):
            return self._challenge(json.loads(options_to_json(options)), "authentication")

    def _challenge(
        self,
        options: dict[str, Any],
        kind: Literal["registration", "authentication"],
        enrollment: Enrollment | None = None,
    ) -> tuple[dict[str, Any], str]:
        ceremony_id, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.save_challenge(
            Challenge(
                token_hash=token_hash(ceremony_id),
                binding_hash=token_hash(binding),
                kind=kind,
                challenge=options["challenge"],
                actor_id=enrollment.actor_id if enrollment else None,
                household_id=enrollment.household_id if enrollment else None,
                enrollment_hash=enrollment.token_hash if enrollment else None,
                expires_at=utc_now() + timedelta(minutes=5),
            )
        )
        return {"ceremony_id": ceremony_id, "options": options}, binding

    def _consume(self, ceremony_id: str, binding: str | None, kind: str) -> Challenge:
        # Consumption commits before verification, including failed verification attempts.
        with self.store.transaction(IDENTITY_LOCK):
            challenge = self.store.take_challenge(
                token_hash(ceremony_id), token_hash(binding or "")
            )
        if challenge is None or challenge.kind != kind or challenge.expires_at <= utc_now():
            raise AuthenticationError("passkey challenge expired or invalid")
        return challenge

    def register(
        self,
        ceremony_id: str,
        binding: str | None,
        credential: dict[str, Any],
        previous_token: str | None,
    ) -> tuple[str, Session]:
        challenge = self._consume(ceremony_id, binding, "registration")
        try:
            validate_client_context(credential)
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge.challenge),
                expected_rp_id=self.settings.rp_id,
                expected_origin=self.settings.public_origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError) as exc:
            raise AuthenticationError("passkey verification failed") from exc
        with self.store.transaction(IDENTITY_LOCK):
            enrollment = self._enrollment(challenge.enrollment_hash or "")
            self.store.save_passkey(
                Passkey(
                    credential_id=bytes_to_base64url(verified.credential_id),
                    actor_id=enrollment.actor_id,
                    public_key=bytes_to_base64url(verified.credential_public_key),
                    sign_count=verified.sign_count,
                    device_type=verified.credential_device_type.value,
                    backed_up=verified.credential_backed_up,
                )
            )
            self.store.delete_enrollment(enrollment.token_hash)
            self.audit.record(
                event_type="identity.passkey_registered",
                actor=ActorContext(
                    actor_id=enrollment.actor_id,
                    household_id=enrollment.household_id,
                    channel=Channel.API,
                ),
                resource_type="passkey",
                resource_id=bytes_to_base64url(verified.credential_id),
                payload={},
            )
            return self._issue(
                enrollment.actor_id, enrollment.household_id, "passkey", previous_token
            )

    def authenticate(
        self,
        ceremony_id: str,
        binding: str | None,
        credential: dict[str, Any],
        previous_token: str | None,
    ) -> tuple[str, Session]:
        challenge = self._consume(ceremony_id, binding, "authentication")
        with self.store.transaction(IDENTITY_LOCK):
            key = self.store.get_passkey(str(credential.get("id", "")))
            if key is None:
                raise AuthenticationError("passkey verification failed")
            try:
                validate_client_context(credential)
                verified = verify_authentication_response(
                    credential=credential,
                    expected_challenge=base64url_to_bytes(challenge.challenge),
                    expected_rp_id=self.settings.rp_id,
                    expected_origin=self.settings.public_origin,
                    credential_public_key=base64url_to_bytes(key.public_key),
                    credential_current_sign_count=key.sign_count,
                    require_user_verification=True,
                )
                handle = credential["response"].get("userHandle")
                if handle is None or base64url_to_bytes(handle) != key.actor_id.bytes:
                    raise AuthenticationError("passkey verification failed")
            except (WebAuthnException, ValueError, TypeError, KeyError) as exc:
                raise AuthenticationError("passkey verification failed") from exc
            if verified.credential_device_type.value != key.device_type:
                raise AuthenticationError("passkey verification failed")
            memberships = self.store.memberships(key.actor_id)
            if not memberships:
                raise AuthorizationError("household membership required")
            self.store.update_passkey(
                key.model_copy(
                    update={
                        "sign_count": verified.new_sign_count,
                        "backed_up": verified.credential_backed_up,
                    }
                )
            )
            return self._issue(key.actor_id, memberships[0].household_id, "passkey", previous_token)
