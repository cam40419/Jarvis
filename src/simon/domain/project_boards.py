"""Operator grants and bounded public snapshots for managed project boards."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from simon.domain.models import StrictModel

BoardNumber = Annotated[str, Field(pattern=r"^[0-9]{1,30}$")]
BoardTaskID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]


class BoardConnection(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["clickup"] = "clickup"
    enabled: bool = False
    workspace_id: UUID
    actor_ids: frozenset[UUID] = Field(min_length=1, max_length=100)
    credential_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,160}$")
    clickup_workspace_id: BoardNumber
    list_ids: frozenset[BoardNumber] = Field(default=frozenset(), max_length=50)
    discover_lists: bool = False
    auth_type: Literal["personal", "oauth"] = "personal"

    @model_validator(mode="before")
    @classmethod
    def legacy_clickup_grant(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and "household_id" in value
            and "clickup_workspace_id" not in value
        ):
            value = dict(value)
            value["clickup_workspace_id"] = value.pop("workspace_id", None)
            value["workspace_id"] = value.pop("household_id")
        return value

    @model_validator(mode="after")
    def require_list_grant(self) -> "BoardConnection":
        if not self.discover_lists and not self.list_ids:
            raise ValueError("Choose automatic discovery or explicitly granted Lists")
        return self


class BoardStatus(StrictModel):
    status: str = Field(min_length=1, max_length=100)
    type: str = Field(min_length=1, max_length=40)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class BoardList(StrictModel):
    id: BoardNumber
    workspace_id: BoardNumber
    name: str = Field(min_length=1, max_length=300)
    space_id: BoardNumber
    statuses: tuple[BoardStatus, ...] = Field(max_length=100)
    archived: bool = False
    url: str


class BoardAssignee(StrictModel):
    id: BoardNumber
    name: str = Field(max_length=300)


class BoardTask(StrictModel):
    id: BoardTaskID
    list_id: BoardNumber
    workspace_id: BoardNumber
    name: str = Field(min_length=1, max_length=1000)
    description: str = Field(default="", max_length=100_000)
    status: str = Field(min_length=1, max_length=100)
    status_type: str = Field(min_length=1, max_length=40)
    date_updated: str = Field(pattern=r"^[0-9]{1,20}$")
    assignees: tuple[BoardAssignee, ...] = Field(default=(), max_length=100)
    dependencies: tuple[BoardTaskID, ...] = Field(default=(), max_length=100)
    url: str
    archived: bool = False


class BoardTaskPage(StrictModel):
    tasks: tuple[BoardTask, ...] = Field(max_length=100)
    page: int = Field(ge=0, le=1000)
    next_page: int | None = Field(default=None, ge=0, le=1000)
