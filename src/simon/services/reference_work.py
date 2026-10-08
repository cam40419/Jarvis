"""Deterministic reference effects for exercising workflow reconciliation.

This is a core test tool, not a business connector or publishing capability.
Receipts live independently of the caller's response and are recoverable by operation ID.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

from simon.domain.models import utc_now
from simon.domain.ports import Store
from simon.services.canonical import digest


class ReferenceWork:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _namespace(workspace_id: UUID, project_id: UUID) -> str:
        return f"native_reference:{workspace_id}:{project_id}"

    def execute(
        self, workspace_id: UUID, project_id: UUID, operation_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        def perform() -> dict[str, Any]:
            operation = request["operation"]
            if operation == "delay":
                return {
                    "operation": operation,
                    "operation_id": str(operation_id),
                    "ready_at": (
                        utc_now() + timedelta(seconds=request["delay_seconds"])
                    ).isoformat(),
                }
            return {
                "operation": operation,
                "operation_id": str(operation_id),
                "text": request["text"],
                "receipt": digest({"id": str(operation_id), "request": request}),
            }

        with self.store.transaction(workspace_id):
            output, _ = self.store.execute_once(
                self._namespace(workspace_id, project_id),
                str(operation_id),
                digest(request),
                perform,
            )
            return output

    def lookup(
        self, workspace_id: UUID, project_id: UUID, operation_id: UUID
    ) -> dict[str, Any] | None:
        return self.store.command_receipt(
            self._namespace(workspace_id, project_id), str(operation_id)
        )
