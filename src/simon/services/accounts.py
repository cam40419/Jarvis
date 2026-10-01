"""Invite-only accounts with independent workspaces and explicit site administration."""

from typing import Any
from uuid import UUID, uuid5

from simon.domain.accounts import AccountAccess, InviteAccount, ManagedAccount
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.identity import Membership
from simon.domain.models import ActorContext, utc_now
from simon.services.identity import IDENTITY_LOCK, IdentityService, token_hash


class AccountService:
    def __init__(self, identity: IdentityService) -> None:
        self.identity, self.store = identity, identity.store

    def is_admin(self, actor: ActorContext) -> bool:
        return (
            actor.actor_id == self.identity.settings.account_admin_actor_id
            and self.identity.membership(actor.actor_id, actor.household_id).role == "owner"
        )

    def authorize(self, actor: ActorContext) -> None:
        if not self.is_admin(actor):
            raise AuthorizationError("Only the configured Simon administrator can manage accounts.")

    def describe(self, account: ManagedAccount) -> dict[str, Any]:
        enrollment = self.store.get_enrollment(account.enrollment_hash or "")
        has_password = self.store.password_for_actor(account.actor_id) is not None
        active = bool(self.store.passkeys(account.actor_id) or has_password)
        status = (
            "disabled"
            if account.disabled
            else "active"
            if active
            else (
                "invited"
                if enrollment and enrollment.expires_at > utc_now()
                else "invitation_expired"
            )
        )
        return {
            **account.model_dump(mode="json", exclude={"enrollment_hash"}),
            "status": status,
            "has_password": has_password,
            "invitation_expires_at": enrollment.expires_at.isoformat() if enrollment else None,
        }

    def list(self, actor: ActorContext) -> list[dict[str, Any]]:
        self.authorize(actor)
        return [self.describe(account) for account in self.store.managed_accounts()]

    def issue(self, account: ManagedAccount) -> dict[str, Any]:
        if (
            account.disabled
            or self.store.passkeys(account.actor_id)
            or self.store.password_for_actor(account.actor_id)
        ):
            raise InvalidTransitionError(
                "Only an unregistered, enabled account can receive an invitation."
            )
        if account.enrollment_hash:
            self.store.delete_enrollment(account.enrollment_hash)
        token = self.identity.enroll(account.actor_id, account.household_id)
        account = account.model_copy(
            update={"enrollment_hash": token_hash(token), "version": account.version + 1}
        )
        self.store.save_managed_account(account)
        return {"account": self.describe(account), "enrollment_token": token}

    def invite(self, actor: ActorContext, request: InviteAccount) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK):
            self.authorize(actor)
            identifier = uuid5(actor.actor_id, "account:" + str(request.idempotency_key))
            if previous := self.store.managed_account(identifier):
                if previous.display_name != request.display_name:
                    raise IdempotencyConflictError(
                        "This invitation request already used a different name."
                    )
                # Never persist or replay a plaintext invitation in idempotency receipts.
                return {"account": self.describe(previous), "enrollment_token": None}
            if len(self.store.managed_accounts()) >= 25:
                raise InvalidTransitionError(
                    "This installation currently supports 25 invited accounts."
                )
            account = ManagedAccount(
                actor_id=identifier,
                household_id=uuid5(identifier, "private-workspace"),
                invited_by=actor.actor_id,
                display_name=request.display_name,
            )
            self.store.put_membership(
                Membership(
                    actor_id=identifier,
                    household_id=account.household_id,
                    role="owner",
                    display_name=request.display_name,
                    household_name=request.display_name + "'s workspace",
                )
            )
            self.store.save_managed_account(account)
            self.identity.audit.record(
                event_type="account.invited",
                actor=actor,
                resource_type="user",
                resource_id=str(identifier),
                payload={"private_workspace": str(account.household_id)},
            )
            return self.issue(account)

    def get(self, identifier: UUID) -> ManagedAccount:
        account = self.store.managed_account(identifier)
        if not account:
            raise NotFoundError("Managed account not found.")
        return account

    def renew(self, actor: ActorContext, identifier: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK):
            self.authorize(actor)
            return self.issue(self.get(identifier))

    def recovery(self, actor: ActorContext, identifier: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK):
            self.authorize(actor)
            account = self.get(identifier)
            if account.disabled:
                raise InvalidTransitionError("Enable the account before issuing a recovery code.")
            if self.store.password_for_actor(identifier) is None:
                raise InvalidTransitionError("This account has no password to reset.")
            if account.enrollment_hash:
                self.store.delete_enrollment(account.enrollment_hash)
            token = self.identity.enroll(identifier, account.household_id)
            self.store.save_managed_account(
                account.model_copy(
                    update={
                        "enrollment_hash": token_hash(token),
                        "version": account.version + 1,
                    }
                )
            )
            self.identity.audit.record(
                event_type="account.password_recovery_issued",
                actor=actor,
                resource_type="user",
                resource_id=str(identifier),
                payload={},
            )
            return {"enrollment_token": token}

    def access(
        self, actor: ActorContext, identifier: UUID, request: AccountAccess
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK):
            self.authorize(actor)
            account = self.get(identifier)
            if identifier == self.identity.settings.account_admin_actor_id:
                raise AuthorizationError("The site administrator cannot be disabled here.")
            if account.version != request.expected_version:
                raise InvalidTransitionError("Account changed. Refresh before trying again.")
            account = account.model_copy(
                update={"disabled": not request.enabled, "version": account.version + 1}
            )
            self.store.save_managed_account(account)
            self.store.revoke_sessions(identifier)
            if account.enrollment_hash:
                self.store.delete_enrollment(account.enrollment_hash)
            self.identity.audit.record(
                event_type="account.access_updated",
                actor=actor,
                resource_type="user",
                resource_id=str(identifier),
                payload={"enabled": request.enabled},
            )
            return self.describe(account)
