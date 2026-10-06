"""Version-one RobbinsHome response DTOs; no device or storage implementation."""

from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class HomeChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    device_id: str
    on: bool | None = None
    brightness: int | None = None
    color: str | None = None


class HomeStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    device_id: str
    online: bool | None = None
    on: bool | None = None
    brightness: float | None = None
    color: str | None = None


class HomeCommand(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: UUID
    workspace_id: UUID = Field(validation_alias=AliasChoices("workspace_id", "household_id"))
    actor_id: UUID
    run_id: UUID | None = None
    thread_id: UUID | None = None
    device_name: str
    change: HomeChange
    status: str
    observed: HomeStatus | None = None
    verified: bool = False
    error: str | None = None
    created_at: Any = None
