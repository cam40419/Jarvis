"""Opt-in deliverable assessment; these records never authorize actions."""

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from simon.domain.models import StrictModel


class AgentCompletionContract(StrictModel):
    criteria: str = Field(min_length=1, max_length=4000)

    @field_validator("criteria")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A completion contract needs acceptance criteria")
        return value.strip()


class CompletionEvidence(StrictModel):
    source: Literal["candidate", "task_context"]
    excerpt: str = Field(min_length=1, max_length=1000)

    @field_validator("excerpt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Completion evidence needs a nonempty excerpt")
        return value


class CompletionEvidenceReference(StrictModel):
    """Model-selected ID resolved against server-numbered original evidence."""

    source: Literal["candidate", "task_context"]
    passage_id: str = Field(min_length=5, max_length=5, pattern=r"^[CT][0-9]{4}$")


class CompletionCheck(StrictModel):
    requirement: str = Field(min_length=1, max_length=600)
    kind: Literal["deliverable", "source_support", "saved_result", "verification"]
    status: Literal["satisfied", "missing", "unverified"]
    evidence: tuple[CompletionEvidence, ...] = Field(max_length=4)

    @field_validator("requirement")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A completion check needs a requirement")
        return value.strip()

    @model_validator(mode="after")
    def supported_check(self) -> Self:
        if self.status == "satisfied":
            if not self.evidence:
                raise ValueError("A satisfied requirement needs evidence")
            if self.kind != "deliverable" and not any(
                item.source == "task_context" for item in self.evidence
            ):
                raise ValueError("Source, save and verification claims need execution evidence")
        return self


class CompletionReview(StrictModel):
    status: Literal["complete", "partial", "not_delivered"]
    summary: str = Field(min_length=1, max_length=2000)
    checks: tuple[CompletionCheck, ...] = Field(min_length=1, max_length=16)

    @field_validator("summary")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A completion review needs an explanation")
        return value.strip()

    @model_validator(mode="after")
    def consistent_verdict(self) -> Self:
        unmet = any(item.status != "satisfied" for item in self.checks)
        if (self.status == "complete") == unmet:
            raise ValueError("Completion status must agree with its requirement checks")
        if not any(item.kind == "deliverable" for item in self.checks):
            raise ValueError("Completion must assess the requested deliverable")
        return self
