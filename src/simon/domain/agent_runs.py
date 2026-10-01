"""Durable run state separate from immutable planning snapshots."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from simon.domain.artifacts import Artifact, DependencyArtifact
from simon.domain.models import JobStatus, StrictModel


class StartAgentRun(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=180)
    model_budget_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class TaskExecution(StrictModel):
    id: str
    agent_id: str
    status: Literal[
        "queued", "running", "succeeded", "failed", "cancelled", "blocked", "unknown"
    ] = "queued"
    output: str = ""
    error_code: str | None = None
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_reserved_usd: float | None = None
    artifacts: tuple[Artifact, ...] = ()
    input_artifacts: tuple[DependencyArtifact, ...] = ()
    environment_lease_id: UUID | None = None
    environment_id: str | None = None
    events: tuple[dict[str, Any], ...] = ()


class AgentRun(StrictModel):
    id: UUID
    plan_id: UUID
    workspace_id: UUID
    actor_id: UUID
    status: JobStatus = JobStatus.QUEUED
    version: int = 1
    cancel_requested: bool = False
    execution_started: bool = False
    reserved_slots: int = 0
    model_reserved_usd: float | None = None
    model_budget_usd: float | None = None
    executor_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tasks: tuple[TaskExecution, ...]


class ReconcileAgentRun(StrictModel):
    expected_version: int = Field(ge=1)
    # An operator must stop the original dispatcher before releasing its reservation.
    worker_stopped: Literal[True]
