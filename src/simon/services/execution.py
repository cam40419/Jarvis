"""One-host lease journal for isolated execution resources, not a work scheduler.

Use a local SQLite path on the execution manager host, never a network filesystem.
Concurrent processes sharing that path reserve atomically. A disconnected or
expired heartbeat never releases ownership automatically: first reconcile or
release the resource. Plans and constructor calls perform no external operation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from simon.adapters.execution_backends import DockerBackend, ExecutionBackend, MachineBackend
from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentLease,
    EnvironmentPlan,
    EnvironmentRequest,
    ExecutionCapacityError,
    ExecutionCommand,
    ExecutionError,
    ExecutionResult,
)


class EnvironmentManager:
    def __init__(
        self,
        definitions: list[EnvironmentDefinition],
        *,
        state_path: Path,
        workspace_root: Path,
        backends: Mapping[str, ExecutionBackend] | None = None,
    ) -> None:
        self.definitions = {item.id: item for item in definitions}
        if len(self.definitions) != len(definitions):
            raise ValueError("Environment IDs must be unique")
        self.state_path = state_path.expanduser().absolute()
        self.workspace_root = workspace_root.expanduser().resolve()
        defaults: dict[str, ExecutionBackend] = {
            "docker": DockerBackend(),
            "machine": MachineBackend(),
        }
        self.backends: dict[str, ExecutionBackend] = dict(
            defaults if backends is None else backends
        )

    def plan(
        self, request: EnvironmentRequest, environment_id: str | None = None
    ) -> EnvironmentPlan:
        candidates = sorted(self.definitions.values(), key=lambda item: item.id)
        for definition in candidates:
            if (
                not definition.enabled
                or (environment_id is not None and definition.id != environment_id)
                or not request.capabilities <= definition.capabilities
                or (request.os is not None and definition.os != request.os)
            ):
                continue
            path = self.workspace_root.joinpath(
                str(request.workspace_id),
                request.agent_id,
                str(request.task_id),
                str(request.attempt_id),
            )
            if not path.resolve().is_relative_to(self.workspace_root) or path.resolve() != path:
                raise ExecutionError("Workspace path escapes or redirects the configured root")
            return EnvironmentPlan(
                environment_id=definition.id,
                kind=definition.kind,
                request=request,
                workspace_path=path,
                network=definition.network,
                cpu_limit=definition.cpu_limit,
                memory_mb=definition.memory_mb,
                gpu_devices=definition.gpu_devices,
            )
        raise ExecutionError(
            "No enabled execution environment satisfies the requested capabilities"
        )

    def _connect(self) -> sqlite3.Connection:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.state_path, timeout=30, isolation_level=None)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS environment_leases ("
            "id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL, "
            "environment_id TEXT NOT NULL, fence INTEGER NOT NULL, "
            "status TEXT NOT NULL, snapshot TEXT NOT NULL)"
        )
        return connection

    def get(self, lease_id: UUID) -> EnvironmentLease:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT snapshot FROM environment_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()
            if row is None:
                raise ExecutionError("Unknown execution lease")
            return EnvironmentLease.model_validate_json(row[0])

    def get_for_attempt(self, attempt_id: UUID) -> EnvironmentLease | None:
        """Locate a quarantined allocation after its backend raised before returning."""
        if not self.state_path.exists():
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT snapshot FROM environment_leases WHERE attempt_id = ?",
                (str(attempt_id),),
            ).fetchone()
            return EnvironmentLease.model_validate_json(row[0]) if row else None

    def _save(self, lease: EnvironmentLease, *, expected_status: str) -> None:
        with closing(self._connect()) as connection:
            changed = connection.execute(
                "UPDATE environment_leases SET status = ?, snapshot = ? "
                "WHERE id = ? AND fence = ? AND status = ?",
                (
                    lease.status,
                    lease.model_dump_json(),
                    str(lease.id),
                    lease.fencing_token,
                    expected_status,
                ),
            ).rowcount
            if changed != 1:
                raise ExecutionError("The execution lease changed during the operation")

    def allocate(
        self, request: EnvironmentRequest, environment_id: str | None = None
    ) -> EnvironmentLease:
        plan = self.plan(request, environment_id)
        definition = self.definitions[plan.environment_id]
        backend = self.backends.get(definition.kind)
        if backend is None:
            raise ExecutionError("No backend is installed for the execution environment")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute(
                "SELECT snapshot FROM environment_leases WHERE attempt_id = ?",
                (str(request.attempt_id),),
            ).fetchone()
            if old is not None:
                existing = EnvironmentLease.model_validate_json(old[0])
                if existing.plan != plan:
                    raise ExecutionError(
                        "The attempt already belongs to a different execution plan"
                    )
                return existing
            count = connection.execute(
                "SELECT COUNT(*) FROM environment_leases "
                "WHERE environment_id = ? AND status != 'released'",
                (definition.id,),
            ).fetchone()
            assert count is not None
            if count[0] >= definition.max_concurrency:
                raise ExecutionCapacityError("The execution environment has no available capacity")
            previous = connection.execute(
                "SELECT COALESCE(MAX(fence), 0) FROM environment_leases WHERE environment_id = ?",
                (definition.id,),
            ).fetchone()
            assert previous is not None
            now = datetime.now(UTC)
            lease = EnvironmentLease(
                id=uuid4(),
                plan=plan,
                definition=definition,
                fencing_token=previous[0] + 1,
                status="allocating",
                created_at=now,
                heartbeat_at=now,
            )
            connection.execute(
                "INSERT INTO environment_leases VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(lease.id),
                    str(request.attempt_id),
                    definition.id,
                    lease.fencing_token,
                    lease.status,
                    lease.model_dump_json(),
                ),
            )
            connection.commit()
        try:
            if definition.kind == "docker":
                # Each attempt owns a new directory. No cleanup ever deletes project files.
                plan.workspace_path.mkdir(parents=True, exist_ok=False)
                if plan.workspace_path.resolve() != plan.workspace_path:
                    raise ExecutionError("The allocated workspace contains a filesystem redirect")
            handle = backend.allocate(lease)
        except Exception:
            self._save(lease.model_copy(update={"status": "unknown"}), expected_status="allocating")
            raise
        active = lease.model_copy(update={"resource_handle": handle, "status": "active"})
        self._save(active, expected_status="allocating")
        return active

    def _owned(self, lease_id: UUID, attempt_id: UUID, fencing_token: int) -> EnvironmentLease:
        lease = self.get(lease_id)
        if lease.plan.request.attempt_id != attempt_id or lease.fencing_token != fencing_token:
            raise ExecutionError("The caller does not own this execution lease")
        return lease

    def heartbeat(
        self, lease_id: UUID, *, attempt_id: UUID, fencing_token: int
    ) -> EnvironmentLease:
        lease = self._owned(lease_id, attempt_id, fencing_token)
        if lease.status != "active":
            raise ExecutionError("Only an active execution lease accepts heartbeats")
        self.backends[lease.definition.kind].heartbeat(lease)
        updated = lease.model_copy(update={"heartbeat_at": datetime.now(UTC)})
        self._save(updated, expected_status="active")
        return updated

    def execute(
        self,
        lease_id: UUID,
        command: ExecutionCommand,
        *,
        attempt_id: UUID,
        fencing_token: int,
    ) -> ExecutionResult:
        """Trusted worker entry point; callers must authorize code execution separately.

        No retry occurs. A timed-out Docker CLI does not establish that the remote
        command stopped, so any uncertain outcome quarantines the entire lease.
        """
        lease = self._owned(lease_id, attempt_id, fencing_token)
        if lease.status != "active":
            raise ExecutionError("Commands require an active execution lease")
        pending = lease.model_copy(update={"status": "executing"})
        self._save(pending, expected_status="active")
        try:
            result = self.backends[lease.definition.kind].execute(pending, command)
        except Exception:
            self._save(
                pending.model_copy(update={"status": "unknown"}), expected_status="executing"
            )
            raise
        self._save(
            pending.model_copy(update={"status": "active", "heartbeat_at": datetime.now(UTC)}),
            expected_status="executing",
        )
        return result

    def release(
        self,
        lease_id: UUID,
        *,
        attempt_id: UUID,
        fencing_token: int,
        recover_interrupted: bool = False,
    ) -> EnvironmentLease:
        """Release a known owned resource; artifacts are retained.

        Administrative crash recovery may set recover_interrupted only AFTER
        stopping the original execution manager process. This permits cleanup of
        an in-flight journal record without retrying its previous operation. An
        old heartbeat alone never justifies taking over an active process.
        """
        lease = self._owned(lease_id, attempt_id, fencing_token)
        if lease.status == "released":
            return lease
        if lease.status in {"allocating", "executing", "releasing"} and not recover_interrupted:
            raise ExecutionError("An execution lease operation is already in progress")
        pending = lease.model_copy(update={"status": "releasing"})
        self._save(pending, expected_status=lease.status)
        try:
            self.backends[lease.definition.kind].release(pending)
        except Exception:
            self._save(
                pending.model_copy(update={"status": "unknown"}), expected_status="releasing"
            )
            raise
        released = pending.model_copy(update={"status": "released"})
        self._save(released, expected_status="releasing")
        return released
