"""Durable per-project board bindings and guarded synchronization requests."""

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from simon.domain.models import StrictModel, utc_now
from simon.domain.project_boards import BoardList, BoardNumber, BoardTaskID


class BoardBinding(StrictModel):
    connection_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    list_id: BoardNumber
    status_map: dict[Literal["ready", "running", "done", "blocked"], str] = Field(
        default_factory=dict
    )
    auto_publish: bool = False
    sync_status: bool = False
    sync_progress: bool = True

    @model_validator(mode="after")
    def valid_statuses(self) -> "BoardBinding":
        if any(not value.strip() or len(value) > 100 for value in self.status_map.values()):
            raise ValueError("Board status names must contain between one and 100 characters")
        if self.sync_status and set(self.status_map) != {"ready", "running", "done", "blocked"}:
            raise ValueError("Status synchronization requires all four status mappings")
        return self


class BindProjectBoard(StrictModel):
    expected_version: int = Field(ge=0)
    binding: BoardBinding


class ImportBoardTasks(StrictModel):
    task_ids: tuple[BoardTaskID, ...] = Field(min_length=1, max_length=25)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=180)


class PublishBoardTasks(StrictModel):
    todo_ids: tuple[str, ...] = Field(min_length=1, max_length=25)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=180)


class BoardMapping(StrictModel):
    todo_id: str
    remote_id: BoardTaskID
    url: str
    date_updated: str
    baseline_hash: str
    remote_status: str
    remote_archived: bool = False
    last_published_status: str | None = None
    last_status_digest: str | None = None
    last_progress_digest: str | None = None


class BoardOperation(StrictModel):
    id: UUID
    project_id: UUID
    kind: Literal["create", "dependency", "status", "comment"]
    todo_id: str
    state: Literal["running", "succeeded", "failed", "unknown"] = "running"
    marker: str
    payload: dict[str, Any]
    claim_id: UUID
    remote_id: str | None = None
    receipt: dict[str, Any] | None = None
    error: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class ProjectBoardState(StrictModel):
    project_id: UUID
    workspace_id: UUID
    actor_id: UUID
    version: int = Field(default=0, ge=0)
    binding: BoardBinding | None = None
    board: BoardList | None = None
    mappings: tuple[BoardMapping, ...] = Field(default=(), max_length=500)
    pending_operation_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    blocked_reasons: tuple[str, ...] = ()
    last_sync_at: AwareDatetime | None = None
    next_sync_at: AwareDatetime | None = None
    mapping_cursor: int = Field(default=0, ge=0)
    abandoned_create_todo_ids: tuple[str, ...] = Field(default=(), max_length=500)
    read_failure_count: int = Field(default=0, ge=0, le=4)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class ReconcileBoardOperation(StrictModel):
    expected_version: int = Field(ge=1)
    operation_id: UUID
    resolution: Literal["attach", "acknowledge"]
    remote_id: BoardTaskID | None = None
    note: str = Field(min_length=1, max_length=2000, pattern=r"\S")
