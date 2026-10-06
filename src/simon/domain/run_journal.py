"""Owner-scoped, immutable project execution records with explicit candidate status."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from simon.domain.artifacts import Artifact
from simon.domain.models import StrictModel

JournalKind = Literal["context", "model_response", "candidate", "evidence", "review"]


class JournalEntry(StrictModel):
    id: UUID
    project_id: UUID | None
    run_id: UUID
    task_id: str
    agent_id: str
    kind: JournalKind
    step: int = Field(ge=0, le=30)
    title: str = Field(max_length=240)
    status: Literal["recorded", "draft", "partial", "accepted"] = "recorded"
    artifact: Artifact
    redacted: bool = False


class JournalPage(StrictModel):
    project_id: UUID
    run_id: UUID
    items: tuple[JournalEntry, ...]
    next_offset: int | None


class JournalRead(StrictModel):
    entry: JournalEntry
    pointer: str
    offset: int
    next_offset: int | None
    total_chars: int
    text: str
    value_sha256: str
    eof: bool
