"""Bounded intake evidence, planning proposals and durable planning attempts."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from simon.domain.models import utc_now
from simon.domain.native_projects import NativeCommand, NativeModel

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Answer = Annotated[str, StringConstraints(max_length=2000)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Amount = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807, strict=True)]

# Attempt context and dispatch identity cannot change when a result is settled.
INTAKE_RUN_MUTABLE_FIELDS = frozenset(
    {
        "version",
        "status",
        "reserved_microusd",
        "charged_microusd",
        "error_code",
        "proposal",
        "review",
        "applied_agent_ids",
        "applied_task_ids",
        "input_tokens",
        "output_tokens",
        "finished_at",
    }
)


class IntakeFields(NativeModel):
    background: str = Field(default="", max_length=6000)
    outcomes: str = Field(default="", max_length=6000)
    constraints: str = Field(default="", max_length=6000)
    answers: dict[Slug, Answer] = Field(default_factory=dict, max_length=12)
    endpoint_id: str | None = Field(default=None, min_length=1, max_length=96)
    allow_cloud: bool = False
    auto_staff: bool = True
    budget_microusd: int = Field(default=0, ge=0, le=1_000_000_000, strict=True)

    @field_validator("answers")
    @classmethod
    def valid_answers(cls, values: dict[str, str]) -> dict[str, str]:
        for value in values.values():
            cls.valid_text(value)
        return values


class NativeIntake(IntakeFields):
    workspace_id: UUID
    project_id: UUID
    version: int = Field(default=0, ge=0, strict=True)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class UpdateNativeIntake(IntakeFields, NativeCommand):
    expected_version: int = Field(ge=0, strict=True)


class SourceFields(NativeModel):
    source_key: str = Field(min_length=1, max_length=240)
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(min_length=1, max_length=100)

    @field_validator("source_key")
    @classmethod
    def relative_label(cls, value: str) -> str:
        if (
            "\\" in value
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Source keys must be relative display labels.")
        return value

    @field_validator("filename")
    @classmethod
    def simple_filename(cls, value: str) -> str:
        if any(character in value for character in ("/", "\\", "\r", "\n")) or value in {".", ".."}:
            raise ValueError("Filename must be a single display name.")
        return value


class IntakeSource(SourceFields):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    revision: int = Field(ge=1, strict=True)
    sha256: Digest
    size_bytes: int = Field(ge=0, le=5 * 1024 * 1024, strict=True)
    text: str = Field(default="", max_length=20000)
    extraction_status: Literal["text", "unparsed"]
    redactions: int = Field(default=0, ge=0, strict=True)
    truncated: bool = False
    revoked_at: AwareDatetime | None = None
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def revocation_time(self) -> Self:
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("Revocation cannot precede source creation.")
        return self


class Citation(NativeModel):
    source_id: UUID
    quote: str = Field(min_length=1, max_length=500)


class Finding(NativeModel):
    kind: Literal["fact", "assumption", "conflict"]
    statement: str = Field(min_length=1, max_length=1200)
    evidence: tuple[Citation, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def grounded(self) -> Self:
        if self.kind != "assumption" and not self.evidence:
            raise ValueError("Facts and conflicts must cite evidence.")
        return self


class Question(NativeModel):
    key: Slug
    question: str = Field(min_length=1, max_length=500)
    why: str = Field(min_length=1, max_length=500)
    blocking: bool


class StaffingRole(NativeModel):
    role_key: Slug
    action: Literal["reuse", "create"]
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=8000)
    success_criteria: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=2000)
    agent_id: UUID | None = None
    need: Literal["reuse", "specialist", "parallel_capacity", "independent_review"]
    reuse_assessment: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid_action(self) -> Self:
        if self.action == "reuse":
            if self.agent_id is None or self.need != "reuse":
                raise ValueError("Reused roles must name an agent and a reuse need.")
        elif self.agent_id is not None or self.need == "reuse":
            raise ValueError("New roles cannot name an existing agent or a reuse need.")
        return self


class IntakeTask(NativeModel):
    key: Slug
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    acceptance: str = Field(min_length=1, max_length=2000)
    assignment: Literal["agent", "human", "pool"]
    role_key: Slug | None = None
    review_role_key: Slug | None = None
    existing_task_id: UUID | None = None

    @model_validator(mode="after")
    def valid_assignment(self) -> Self:
        if (self.assignment == "agent") != (self.role_key is not None):
            raise ValueError("Only agent assignments must name a role.")
        if self.review_role_key is not None and self.review_role_key == self.role_key:
            raise ValueError("The reviewer must be independent of the task's maker.")
        return self


class StaffingProposal(NativeModel):
    summary: str = Field(min_length=1, max_length=4000)
    next_milestone: str = Field(min_length=1, max_length=2000)
    findings: tuple[Finding, ...] = Field(default=(), max_length=16)
    questions: tuple[Question, ...] = Field(default=(), max_length=6)
    roles: tuple[StaffingRole, ...] = Field(default=(), max_length=12)
    tasks: tuple[IntakeTask, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        for keys in (
            [question.key for question in self.questions],
            [role.role_key for role in self.roles],
            [task.key for task in self.tasks],
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("Proposal question, role and task keys must be unique.")
        return self


class ProposalReview(NativeModel):
    approved: bool
    issues: tuple[Annotated[str, StringConstraints(min_length=1, max_length=1000)], ...] = Field(
        default=(), max_length=8
    )

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if self.approved == bool(self.issues):
            raise ValueError("Approved reviews have no issues; rejected reviews explain issues.")
        for issue in self.issues:
            self.valid_text(issue)
        return self


class IntakeRun(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    requested_by: UUID
    idempotency_key: str = Field(min_length=8, max_length=200)
    request_digest: Digest
    version: int = Field(default=1, ge=1, strict=True)
    status: Literal[
        "running",
        "ready",
        "questions",
        "needs_revision",
        "applied",
        "stale",
        "failed",
        "unknown",
        "cancelled",
    ] = "running"
    intake_version: int = Field(ge=1, strict=True)
    snapshot_digest: Digest
    endpoint_id: str = Field(min_length=1, max_length=96)
    model: str = Field(min_length=1, max_length=256)
    reserved_microusd: Amount = 0
    charged_microusd: Amount = 0
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,95}$")
    proposal: StaffingProposal | None = None
    review: ProposalReview | None = None
    included_source_ids: tuple[UUID, ...] = Field(default=(), max_length=12)
    omitted_source_ids: tuple[UUID, ...] = ()
    applied_agent_ids: tuple[UUID, ...] = Field(default=(), max_length=12)
    applied_task_ids: tuple[UUID, ...] = Field(default=(), max_length=40)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    started_at: AwareDatetime = Field(default_factory=utc_now)
    deadline_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @field_validator("started_at", "deadline_at", "finished_at")
    @classmethod
    def utc_times(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def valid_times(self) -> Self:
        if self.deadline_at <= self.started_at:
            raise ValueError("Planning deadline must follow its start.")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("Planning cannot finish before it starts.")
        return self


class AnalyzeIntake(NativeCommand):
    expected_version: int = Field(ge=1, strict=True)
    source_ids: tuple[UUID, ...] = Field(default=(), max_length=12)


class SourceUpload(SourceFields, NativeCommand):
    content_base64: str = Field(max_length=7 * 1024 * 1024)
    expected_version: int = Field(ge=1, strict=True)
