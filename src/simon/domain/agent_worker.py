"""Bounded worker outcomes and tool provenance, independent of model providers."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from simon.domain.models import StrictModel

WorkerStatus = Literal["succeeded", "failed", "cancelled", "unknown"]


class WorkerToolRecord(StrictModel):
    tool_id: str
    invocation_id: UUID
    status: Literal["succeeded", "failed", "unknown"]
    output_sha256: str | None = None
    output_chars: int = Field(default=0, ge=0)


class WorkerResult(StrictModel):
    status: WorkerStatus
    output: str = Field(default="", max_length=1_000_000)
    error_code: str | None = None
    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    # None means at least one dispatched model call omitted its usage.
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    provenance: tuple[WorkerToolRecord, ...] = ()
