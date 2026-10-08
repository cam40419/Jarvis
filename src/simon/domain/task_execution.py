"""Durable run state separate from immutable planning snapshots."""

from typing import Any, Literal
from uuid import UUID

from simon.domain.artifacts import Artifact, DependencyArtifact
from simon.domain.models import StrictModel


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
