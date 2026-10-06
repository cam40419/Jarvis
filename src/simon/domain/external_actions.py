"""Exact, reviewable external commitments and their durable authorization records."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.models import StrictModel, utc_now

ActionKind = Literal["purchase", "booking", "reservation", "call"]
ActionStatus = Literal[
    "pending",
    "executing",
    "accepted",
    "succeeded",
    "failed",
    "unknown",
    "cancelled",
    "expired",
    "resolved",
]


class ActionMoney(StrictModel):
    amount_minor: int = Field(ge=0, le=100_000_000, strict=True)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ActionLineItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=150)
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(ge=1, le=100, strict=True)


class PurchaseDetails(StrictModel):
    quote_id: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9_.:-]+$")
    items: tuple[ActionLineItem, ...] = Field(min_length=1, max_length=20)
    total: ActionMoney
    fulfillment: str = Field(min_length=1, max_length=1000)


class ReservationDetails(StrictModel):
    quote_id: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9_.:-]+$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    party_size: int = Field(ge=1, le=100, strict=True)
    location: str = Field(min_length=1, max_length=1000)
    total: ActionMoney

    @model_validator(mode="after")
    def ordered_times(self) -> Self:
        if self.end_at <= self.start_at or self.end_at - self.start_at > timedelta(days=90):
            raise ValueError("Reservation end must follow its start by at most 90 days")
        return self


class CallDetails(StrictModel):
    message: str = Field(min_length=1, max_length=1500)
    max_duration_seconds: int = Field(default=120, ge=10, le=300, strict=True)

    @field_validator("message")
    @classmethod
    def printable_message(cls, value: str) -> str:
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("Call text contains unsupported control characters")
        return value


class ExternalActionDraft(StrictModel):
    kind: ActionKind
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    recipient_id: str = Field(min_length=1, max_length=150)
    recipient_label: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=300)
    terms: str = Field(min_length=1, max_length=4000)
    purchase: PurchaseDetails | None = None
    reservation: ReservationDetails | None = None
    call: CallDetails | None = None

    @model_validator(mode="after")
    def matching_details(self) -> Self:
        expected = "reservation" if self.kind in {"booking", "reservation"} else self.kind
        for key in ("purchase", "reservation", "call"):
            if (getattr(self, key) is not None) != (key == expected):
                raise ValueError("The action must contain exactly its kind's detail object")
        if self.kind == "call":
            import re

            if not re.fullmatch(r"\+[1-9][0-9]{7,14}", self.recipient_id):
                raise ValueError("Calls require a complete E.164 phone number")
        return self


class ExternalProviderDefinition(StrictModel):
    """Operator configuration. Secrets are environment references, never proposal fields."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["twilio", "gateway"]
    enabled: bool = False
    workspace_id: UUID
    actor_ids: frozenset[UUID] = Field(min_length=1, max_length=100)
    endpoint: str | None = None
    credential_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,160}$")
    account_sid: str | None = None
    from_number: str | None = None
    merchant_names: dict[str, str] = Field(default_factory=dict, max_length=100)
    call_recipients: frozenset[str] = frozenset()


class ProposeExternalAction(StrictModel):
    draft: ExternalActionDraft
    idempotency_key: str = Field(min_length=8, max_length=200)


class ReviewExternalAction(StrictModel):
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReconcileExternalAction(ReviewExternalAction):
    reported_outcome: Literal["completed", "not_completed"]
    evidence: str = Field(min_length=10, max_length=1500)
    note: str = Field(min_length=10, max_length=1500)

    @field_validator("evidence", "note")
    @classmethod
    def meaningful_record(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 10:
            raise ValueError(
                "Describe provider evidence and reconciliation in at least 10 characters"
            )
        return value


class ManualActionResolution(StrictModel):
    actor_id: UUID
    recorded_at: AwareDatetime
    reported_outcome: Literal["completed", "not_completed"]
    evidence: str
    note: str


class ExternalActionProposal(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    actor_id: UUID
    run_id: UUID | None = None
    idempotency_key: str = Field(min_length=8, max_length=200)
    draft: ExternalActionDraft
    draft_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_fingerprint: str
    provider_name: str
    provider_kind: Literal["twilio", "gateway", "unconfigured"]
    from_number: str | None = None
    status: ActionStatus = "pending"
    blockers: tuple[str, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=30))
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    claimed_at: AwareDatetime | None = None
    provider_id: str | None = None
    provider_status: str | None = None
    outcome: str | None = None
    error: str | None = None
    manual_resolution: ManualActionResolution | None = None


class ExternalQuoteRequest(StrictModel):
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    kind: Literal["purchase", "booking", "reservation"]
    recipient_id: str = Field(min_length=1, max_length=150)
    items: tuple[ActionLineItem, ...] = Field(default=(), max_length=20)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    party_size: int = Field(default=1, ge=1, le=100, strict=True)
    fulfillment: str = Field(default="", max_length=1000)


class ExternalProviderReceipt(StrictModel):
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    status: Literal["accepted", "succeeded", "failed"]
    provider_status: str = Field(min_length=1, max_length=100)
    outcome: str = Field(min_length=1, max_length=1000)
