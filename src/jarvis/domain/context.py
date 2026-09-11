from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from jarvis.domain.models import StrictModel, utc_now


class ExplicitMemory(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    created_by: UUID
    subject: str
    content: str
    scope: Literal["household"] = "household"
    kind: Literal["explicit"] = "explicit"
    sensitivity: Literal["personal"] = "personal"
    source: Literal["user_confirmed"] = "user_confirmed"
    accepted: bool = True
    created_at: AwareDatetime = Field(default_factory=utc_now)


class CreateMemory(StrictModel):
    subject: str = Field(min_length=1, max_length=200, pattern=r"\S")
    content: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    idempotency_key: str = Field(min_length=8, max_length=200)


class MemoryContext(StrictModel):
    source_memory_id: UUID
    subject: str
    text: str
    trust: Literal["untrusted"] = "untrusted"


class SummaryContext(StrictModel):
    method: Literal["bounded-excerpts-v1"] = "bounded-excerpts-v1"
    source_message_ids: tuple[UUID, ...]
    source_sequences: tuple[int, ...]
    text: str
    trust: Literal["untrusted"] = "untrusted"


class ContextPolicy(StrictModel):
    version: Literal["context-v1"] = "context-v1"
    estimator: Literal["utf8-bytes-v1"] = "utf8-bytes-v1"
    budget: int = 24000
    reserved: int = 4096
    used: int
    omitted_messages: int
    memory_candidates: int
    omitted_memories: int
