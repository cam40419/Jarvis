"""Public, path-free references to immutable project run outputs."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from simon.domain.models import StrictModel


class ProjectOutputCopy(StrictModel):
    root: str
    path: str
    revision: str
    bytes: int
    saved_at: datetime
    download_url: str


class ProjectOutput(StrictModel):
    id: UUID
    run_id: UUID
    plan_id: UUID
    task_id: str
    agent_id: str
    name: str
    media_type: str
    size: int
    sha256: str
    created_at: datetime
    download_url: str
    project_copy: ProjectOutputCopy | None = None


class ProjectOutputPage(StrictModel):
    project_id: UUID
    items: tuple[ProjectOutput, ...]
    next_cursor: str | None
    can_promote: bool
    promotion_blocked_reason: str | None


class PromoteProjectOutput(StrictModel):
    path: str | None = Field(default=None, min_length=1, max_length=1000)
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=180)
