from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from simon.domain.connected_tools import WebSource
from simon.domain.context import ContextPolicy, MemoryContext, SummaryContext
from simon.domain.model import AnswerLength, ModelRequest, ProfileName, ProfileSelection
from simon.domain.models import StrictModel, utc_now


class Thread(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    created_by: UUID
    title: str = Field(min_length=1, max_length=200)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class Message(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    thread_id: UUID
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    text: str
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ContextItem(StrictModel):
    source_message_id: UUID
    source_sequence: int
    role: Literal["user", "assistant"]
    text: str
    trust: Literal["untrusted"] = "untrusted"


class Run(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    thread_id: UUID
    actor_id: UUID
    correlation_id: UUID = Field(default_factory=uuid4)
    model_provider: Literal["local", "openai"] = "local"
    model_name: str = "deterministic-echo-v1"
    prompt_release: str = "test-runner-v1"
    model_request: ModelRequest | None = None
    provider_response_id: str | None = None
    provider_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    token_count_ms: int | None = None
    first_text_ms: int | None = None
    total_ms: int | None = None
    profile: ProfileSelection | None = None
    parent_run_id: UUID | None = None
    web_sources: tuple[WebSource, ...] = ()
    tool_calls: tuple[str, ...] = ()
    action_ids: tuple[UUID, ...] = ()
    capability_manifest: tuple[str, ...] = ()
    context: tuple[ContextItem, ...]
    memory_context: tuple[MemoryContext, ...] = ()
    summary_context: SummaryContext | None = None
    context_policy: ContextPolicy | None = None
    input_message_id: UUID
    output_message_id: UUID
    status: Literal["succeeded"] = "succeeded"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime = Field(default_factory=utc_now)


class RunEvent(StrictModel):
    run_id: UUID
    sequence: int = Field(ge=1)
    event_type: Literal["run.started", "message.created", "run.completed"]
    message_id: UUID | None = None


class ModelAttempt(StrictModel):
    run: Run
    user: Message
    household_id: UUID
    status: Literal["pending", "succeeded", "failed"] = "pending"
    error_code: str | None = None
    expires_at: AwareDatetime


class CreateThread(StrictModel):
    title: str = Field(min_length=1, max_length=200, pattern=r"\S")
    idempotency_key: str = Field(min_length=8, max_length=200)


class SubmitRun(StrictModel):
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    idempotency_key: str = Field(min_length=8, max_length=200)
    profile: ProfileName = "auto"
    answer_length: AnswerLength | Literal["auto"] = "normal"
    parent_run_id: UUID | None = None
    timezone: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_+./-]+$")
