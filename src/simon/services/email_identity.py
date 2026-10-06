"""Email verification and password recovery without exposing account existence."""

import logging
import secrets
from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

from argon2.exceptions import VerificationError

from simon.domain.email_identity import EmailCode, ResetPasswordWithEmail
from simon.domain.errors import AuthenticationError, ValidationError
from simon.domain.models import ActorContext, utc_now
from simon.services.identity import IDENTITY_LOCK, PASSWORD_HASHER, IdentityService

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    @property
    def configured(self) -> bool: ...
    def send(self, recipient: str, subject: str, text: str, identifier: UUID) -> None: ...


class EmailIdentityService:
    def __init__(self, identity: IdentityService, sender: EmailSender) -> None:
        self.identity, self.store, self.sender = identity, identity.store, sender

    def require_delivery(self) -> None:
        if not self.sender.configured:
            raise ValidationError(
                "Email recovery is not ready. Ask your administrator to set up "
                "email delivery in Connections."
            )

    def _issue(
        self, actor: ActorContext, email: str, purpose: Literal["verify", "reset"]
    ) -> tuple[EmailCode, str] | None:
        now = utc_now()
        rows = self.store.email_codes(
            email, purpose, now - timedelta(hours=1), actor_id=actor.actor_id
        )
        if len(rows) >= 5 or (rows and rows[0].created_at > now - timedelta(seconds=60)):
            return None
        self.store.purge_email_codes(now - timedelta(hours=1))
        text = f"{secrets.randbelow(100_000_000):08d}"
        code = EmailCode(
            id=uuid4(),
            actor_id=actor.actor_id,
            workspace_id=actor.workspace_id,
            email=email,
            purpose=purpose,
            code_hash=PASSWORD_HASHER.hash(text),
            expires_at=now + timedelta(minutes=10),
        )
        self.store.save_email_code(code)
        return code, text

    def _deliver(self, code: EmailCode, text: str) -> None:
        label = "password reset" if code.purpose == "reset" else "email verification"
        self.sender.send(
            code.email,
            f"Your Simon {label} code",
            f"Your Simon {label} code is {text}.\n\n"
            "It expires in 10 minutes and can be used once.\n"
            "If you did not request this, ignore this email.",
            code.id,
        )

    def request_reset(self, email: str) -> None:
        """Run after the generic HTTP response; never reveal unknown/disabled accounts."""
        try:
            with self.store.transaction(IDENTITY_LOCK):
                credential = self.store.password_for_email(email.casefold())
                if credential is None:
                    return
                memberships = self.store.memberships(credential.actor_id)
                if not memberships:
                    return
                member = self.identity.membership(credential.actor_id, memberships[0].workspace_id)
                from simon.domain.models import Channel

                actor = ActorContext(
                    actor_id=member.actor_id, workspace_id=member.workspace_id, channel=Channel.API
                )
                issued = self._issue(actor, email.casefold(), "reset")
            if issued:
                self._deliver(*issued)
        except Exception as error:
            # Do not log addresses, codes, credentials or provider responses.
            logger.warning("Password recovery email was not sent (%s)", type(error).__name__)

    def request_verification(self, actor: ActorContext, email: str, password: str) -> UUID:
        self.require_delivery()
        email = email.casefold()
        with self.store.transaction(IDENTITY_LOCK):
            self.identity.membership(actor.actor_id, actor.workspace_id)
            credential = self.store.password_for_actor(actor.actor_id)
            try:
                if credential is None or not PASSWORD_HASHER.verify(
                    credential.password_hash, password
                ):
                    raise AuthenticationError(
                        "Enter your current password to change recovery email"
                    )
            except VerificationError:
                raise AuthenticationError(
                    "Enter your current password to change recovery email"
                ) from None
            existing = self.store.password_for_email(email)
            if existing and existing.actor_id != actor.actor_id:
                raise ValidationError("This email address cannot be used for this account")
            issued = self._issue(actor, email, "verify")
            if issued is None:
                raise ValidationError("Please wait before requesting another verification code")
        self._deliver(*issued)
        return issued[0].id

    def _check(self, code: EmailCode | None, text: str) -> bool:
        if code is None or code.consumed or code.attempts >= 5 or code.expires_at <= utc_now():
            return False
        accepted: bool
        try:
            accepted = PASSWORD_HASHER.verify(code.code_hash, text)
        except VerificationError:
            accepted = False
        self.store.save_email_code(code.model_copy(update={"attempts": code.attempts + 1}))
        return accepted

    def confirm_verification(self, actor: ActorContext, identifier: UUID, text: str) -> str:
        result = None
        with self.store.transaction(IDENTITY_LOCK):
            self.identity.membership(actor.actor_id, actor.workspace_id)
            code = self.store.email_code(identifier)
            if (
                code
                and (code.actor_id, code.workspace_id, code.purpose)
                == (actor.actor_id, actor.workspace_id, "verify")
                and self._check(code, text)
            ):
                credential = self.store.password_for_actor(actor.actor_id)
                existing = self.store.password_for_email(code.email)
                if credential and (existing is None or existing.actor_id == actor.actor_id):
                    self.store.save_password(credential.model_copy(update={"email": code.email}))
                    self.store.save_email_code(code.model_copy(update={"consumed": True}))
                    result = code.email
        if result is None:
            raise AuthenticationError("Verification code is invalid or expired")
        return result

    def reset(self, request: ResetPasswordWithEmail) -> None:
        if request.password != request.confirm_password:
            raise ValidationError("The new passwords do not match")
        address = request.email.casefold()
        accepted = False
        with self.store.transaction(IDENTITY_LOCK):
            rows = self.store.email_codes(address, "reset", utc_now() - timedelta(hours=1))
            code = rows[0] if rows else None
            if self._check(code, request.code):
                assert code is not None
                self.identity.membership(code.actor_id, code.workspace_id)
                old = self.store.password_for_actor(code.actor_id)
                if old and old.email == address:
                    credential = self.identity._password_credential(
                        code.actor_id, old.username, request.password
                    )
                    self.store.save_password(credential)
                    self.store.revoke_sessions(code.actor_id)
                    for purpose in ("verify", "reset"):
                        for pending in self.store.email_codes(
                            address, purpose, utc_now() - timedelta(hours=1)
                        ):
                            self.store.save_email_code(
                                pending.model_copy(update={"consumed": True})
                            )
                    self.identity.operator_audit(
                        "identity.password_recovered", code.actor_id, code.workspace_id
                    )
                    accepted = True
        if not accepted:
            raise AuthenticationError("Reset code is invalid or expired")
        try:
            self.sender.send(
                address,
                "Your Simon password was reset",
                "Your Simon password was changed. All previous sessions were signed out.\n"
                "If you did not make this change, contact your administrator immediately.",
                uuid4(),
            )
        except Exception as error:
            logger.warning("Password reset notification failed (%s)", type(error).__name__)
