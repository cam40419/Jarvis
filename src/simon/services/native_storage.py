"""Storage authority belongs to the installation administrator, above tenant preferences."""

from datetime import timedelta
from typing import Any
from uuid import UUID

from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, utc_now
from simon.domain.native_storage import DEFAULT_STORAGE_LIMIT_BYTES, StorageEntitlement
from simon.domain.ports import Store
from simon.domain.storage_commands import UpdateStorageEntitlement
from simon.services.accounts import AccountService
from simon.services.identity import IDENTITY_LOCK
from simon.services.native_projects import NativeProjectService

ACCOUNTING = (
    "Logical retained content: original source bytes and UTF-8 extracted text, artifact "
    "revisions, canonical delivery manifests and generated preview bytes. Revoked and "
    "superseded content still counts. Pending source uploads reserve allowance until "
    "finalized or reconciled. Database overhead, execution traces, backups and transfer "
    "bytes are outside this allowance."
)


class NativeStorageService:
    def __init__(
        self, store: Store, projects: NativeProjectService, accounts: AccountService
    ) -> None:
        self.store, self.projects, self.accounts = store, projects, accounts

    def _admin(self, actor: ActorContext) -> None:
        if not isinstance(actor, ActorContext):
            raise AuthorizationError("Storage entitlements require a human administrator.")
        self.projects._workspace(actor, write=True)
        self.accounts.authorize(actor)

    def _view(
        self,
        actor: ActorContext,
        workspace_id: UUID,
        project_id: UUID | None,
        *,
        administrative: bool = False,
    ) -> dict[str, Any]:
        current = self.store.storage_entitlement(workspace_id)
        now = utc_now()
        effective = current.effective_limit(now) if current else DEFAULT_STORAGE_LIMIT_BYTES
        usage = self.store.storage_usage(workspace_id)
        project_usage = self.store.storage_usage(workspace_id, project_id) if project_id else {}
        can_manage = self.accounts.is_admin(actor)
        can_read_history = (
            administrative or self.projects._workspace(actor).role == "owner" or can_manage
        )
        entitlement = (
            current.model_dump(mode="json")
            if current
            else {
                "workspace_id": str(workspace_id),
                "version": 0,
                "plan_key": "starter",
                "limit_bytes": DEFAULT_STORAGE_LIMIT_BYTES,
                "override_limit_bytes": None,
                "override_expires_at": None,
                "reason": "Installation default",
                "issued_by": None,
                "updated_at": None,
            }
        )
        if not can_read_history:
            entitlement = {**entitlement, "reason": "", "issued_by": None}
        return {
            "workspace_id": str(workspace_id),
            "project_id": str(project_id) if project_id else None,
            "can_manage": can_manage,
            "entitlement": entitlement,
            "effective_limit_bytes": effective,
            **usage,
            "available_bytes": max(0, effective - usage["used_bytes"]),
            "overage_bytes": max(0, usage["used_bytes"] - effective),
            "project_used_bytes": project_usage.get("used_bytes", 0),
            "project_reserved_bytes": project_usage.get("reserved_bytes", 0),
            "history": [
                item.model_dump(mode="json")
                for item in self.store.storage_entitlement_history(workspace_id, limit=50)
            ]
            if can_read_history
            else [],
            "history_truncated": can_read_history and bool(current and current.version > 50),
            "accounting": ACCOUNTING,
        }

    def view(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.projects._project(actor, project_id)
            return self._view(actor, actor.workspace_id, project_id)

    def admin_view(self, actor: ActorContext, workspace_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(workspace_id):
            self._admin(actor)
            self._exists(workspace_id)
            return self._view(actor, workspace_id, None, administrative=True)

    def operation(self, actor: ActorContext, project_id: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.projects._project(actor, project_id)
            self._admin(actor)
            return {
                **self._view(actor, actor.workspace_id, project_id),
                "applied_entitlement": self._receipt(actor, actor.workspace_id, key),
            }

    def _receipt(self, actor: ActorContext, workspace_id: UUID, key: str) -> dict[str, Any]:
        receipt = self.store.command_receipt(
            f"native:{workspace_id}:human:{actor.actor_id}:workspace:storage", key
        )
        if receipt is None:
            raise NotFoundError("Storage operation receipt not found.")
        return StorageEntitlement.model_validate(receipt).model_dump(mode="json")

    def admin_operation(self, actor: ActorContext, workspace_id: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(workspace_id):
            self._admin(actor)
            self._exists(workspace_id)
            return {
                **self._view(actor, workspace_id, None, administrative=True),
                "applied_entitlement": self._receipt(actor, workspace_id, key),
            }

    def _exists(self, workspace_id: UUID) -> None:
        if not self.store.workspace_exists(workspace_id):
            raise NotFoundError("Workspace not found.")

    def _update(
        self, actor: ActorContext, workspace_id: UUID, command: UpdateStorageEntitlement
    ) -> StorageEntitlement:
        self._admin(actor)
        self._exists(workspace_id)
        # Cross-workspace administration records the real issuer in the target's audit
        # chain. Authorization was checked against the issuer's current workspace.
        audit_actor = actor.model_copy(update={"workspace_id": workspace_id})

        def apply() -> StorageEntitlement:
            before = self.store.storage_entitlement(workspace_id)
            self.projects._version(before.version if before else 0, command.expected_version)
            now = utc_now()
            if command.override_expires_at is not None and not (
                now < command.override_expires_at <= now + timedelta(days=30)
            ):
                raise ValidationError("A storage override must expire within the next 30 days.")
            result = StorageEntitlement(
                workspace_id=workspace_id,
                version=command.expected_version + 1,
                plan_key=command.plan_key,
                limit_bytes=command.limit_bytes,
                override_limit_bytes=command.override_limit_bytes,
                override_expires_at=command.override_expires_at,
                reason=command.reason,
                issued_by=actor.actor_id,
                updated_at=now,
            )
            self.store.save_storage_entitlement(result, command.expected_version)
            self.projects.audit.record(
                event_type="native.storage.entitlement_updated",
                actor=audit_actor,
                resource_type="workspace_storage",
                resource_id=str(workspace_id),
                payload={
                    "issuer_workspace_id": str(actor.workspace_id),
                    "before": before.model_dump(mode="json") if before else None,
                    "after": result.model_dump(mode="json"),
                    "usage_at_change": self.store.storage_usage(workspace_id),
                },
            )
            return result

        return self.projects._once(
            audit_actor, "workspace:storage", command, StorageEntitlement, apply
        )

    def update(
        self, actor: ActorContext, project_id: UUID, command: UpdateStorageEntitlement
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.projects._project(actor, project_id, write=True)
            applied = self._update(actor, actor.workspace_id, command)
            return {
                **self._view(actor, actor.workspace_id, project_id),
                "applied_entitlement": applied.model_dump(mode="json"),
            }

    def admin_update(
        self, actor: ActorContext, workspace_id: UUID, command: UpdateStorageEntitlement
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(workspace_id):
            applied = self._update(actor, workspace_id, command)
            return {
                **self._view(actor, workspace_id, None, administrative=True),
                "applied_entitlement": applied.model_dump(mode="json"),
            }
