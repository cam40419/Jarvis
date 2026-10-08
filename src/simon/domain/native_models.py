"""Project model enrollment and durable, shared inference resource authority."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import utc_now
from simon.domain.native_projects import NativeCommand, NativeModel, VersionedNativeCommand

ModelSlug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")]
Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Amount = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807, strict=True)]
Limit = Annotated[int, Field(ge=0, le=1_000_000_000_000, strict=True)]
Rate = Annotated[Decimal, Field(ge=0, le=1_000_000_000, allow_inf_nan=False)]

MODEL_MUTABLE_FIELDS = frozenset(
    {
        "label",
        "enabled",
        "version",
        "credential_revision",
        "qualification_status",
        "qualification_fingerprint",
        "qualification_usage_id",
        "qualification_error",
        "qualified_at",
        "updated_at",
    }
)
MODEL_USAGE_MUTABLE_FIELDS = frozenset(
    {
        "held_microusd",
        "charged_microusd",
        "status",
        "input_tokens",
        "output_tokens",
        "reported_model",
        "finished_at",
        "version",
        "error_code",
        "reconciliation_by",
        "reconciliation_at",
        "reconciliation_reason",
        "reconciliation_evidence",
    }
)


class ModelTemplate(NativeModel):
    """An administrator-approved transport declaration, never a qualification."""

    id: ModelSlug
    name: str = Field(min_length=1, max_length=200)
    workspace_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1000)
    endpoint: ModelEndpoint
    credential_required: bool
    data_policy: str = Field(default="", max_length=2000)
    license_note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def scoped_template(self) -> Self:
        if self.endpoint.id != self.id:
            raise ValueError("Template and endpoint identifiers must agree.")
        expected = "MODEL_PROJECT_KEY" if self.credential_required else None
        if self.endpoint.api_key_env != expected:
            raise ValueError("Model templates cannot refer to deployment credentials.")
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("Workspace bindings must be unique.")
        return self


class ProjectModel(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    template_id: ModelSlug
    label: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    version: int = Field(default=1, ge=1, strict=True)
    credential_revision: int = Field(default=0, ge=0, strict=True)
    qualification_status: Literal["unverified", "ready", "failed"] = "unverified"
    qualification_fingerprint: Fingerprint | None = None
    qualification_usage_id: UUID | None = None
    qualification_error: ModelSlug | None = None
    qualified_at: AwareDatetime | None = None
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("qualified_at")
    @classmethod
    def utc_qualification(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def valid_qualification(self) -> Self:
        if self.qualification_status == "ready" and (
            self.qualification_fingerprint is None or self.qualified_at is None
        ):
            raise ValueError("Ready models require a dated qualification fingerprint.")
        if self.qualification_status == "ready" and self.qualification_error is not None:
            raise ValueError("A ready model cannot carry a qualification error.")
        return self


class ProjectModelCredential(NativeModel):
    """Private immutable ciphertext; API responses must use an explicit public view."""

    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    model_id: UUID
    revision: int = Field(ge=1, strict=True)
    encrypted_secret: str = Field(min_length=1, max_length=32768, repr=False)
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ModelPolicyFields(NativeModel):
    lifetime_limit_microusd: Limit = 0
    daily_limit_microusd: Limit = 0
    monthly_limit_microusd: Limit = 0
    per_operation_limit_microusd: Limit = 0
    max_concurrent_calls: int = Field(default=2, ge=1, le=32, strict=True)
    paused: bool = False
    allow_paid: bool = False
    allow_cloud: bool = False
    planning_model_id: UUID | None = None
    review_model_id: UUID | None = None


class ModelResourcePolicy(ModelPolicyFields):
    workspace_id: UUID
    project_id: UUID | None = None
    version: int = Field(default=0, ge=0, strict=True)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def workspace_has_no_project_grants(self) -> Self:
        if self.project_id is None and (
            self.allow_paid
            or self.allow_cloud
            or self.planning_model_id is not None
            or self.review_model_id is not None
        ):
            raise ValueError("Cloud, paid access and model selection are project policies.")
        return self


class ModelUsage(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    operation_id: UUID
    phase: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    requested_by: UUID
    model_id: UUID | None = None
    model_version: int = Field(default=0, ge=0, strict=True)
    credential_revision: int = Field(default=0, ge=0, strict=True)
    template_id: str = Field(min_length=1, max_length=96)
    model: str = Field(min_length=1, max_length=256)
    reported_model: str | None = Field(default=None, min_length=1, max_length=256)
    endpoint_fingerprint: Fingerprint
    endpoint_snapshot: dict[str, Any] = Field(default_factory=dict)
    input_rate: Rate = Decimal(0)
    output_rate: Rate = Decimal(0)
    reserved_microusd: Amount
    held_microusd: Amount
    charged_microusd: Amount = 0
    status: Literal["reserved", "dispatched", "settled", "unknown", "reconciled", "released"] = (
        "reserved"
    )
    input_tokens: Amount | None = None
    output_tokens: Amount | None = None
    started_at: AwareDatetime = Field(default_factory=utc_now)
    deadline_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    version: int = Field(default=1, ge=1, strict=True)
    error_code: ModelSlug | None = None
    reconciliation_by: UUID | None = None
    reconciliation_at: AwareDatetime | None = None
    reconciliation_reason: str | None = Field(default=None, min_length=10, max_length=1000)
    reconciliation_evidence: str | None = Field(default=None, min_length=10, max_length=2000)

    @field_validator("started_at", "deadline_at", "finished_at", "reconciliation_at")
    @classmethod
    def utc_usage_times(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @field_validator("endpoint_snapshot")
    @classmethod
    def safe_snapshot(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            return value
        endpoint = ModelEndpoint.model_validate(value)
        # Environment names are placeholders only; no host credential references are persisted.
        if endpoint.api_key_env not in {None, "MODEL_PROJECT_KEY"} and not (
            endpoint.api_key_env and endpoint.api_key_env.startswith("MODEL_PROJECT_")
        ):
            raise ValueError("Usage snapshots cannot contain deployment credential references.")
        return endpoint.model_dump(mode="json")

    @model_validator(mode="after")
    def valid_accounting(self) -> Self:
        if self.deadline_at <= self.started_at:
            raise ValueError("Usage deadline must follow reservation time.")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("Usage cannot finish before it starts.")
        if self.held_microusd > self.reserved_microusd:
            raise ValueError("Held cost cannot exceed the original reservation.")
        if self.status in {"settled", "reconciled", "released"} and self.held_microusd:
            raise ValueError("Resolved usage cannot retain a reservation.")
        if self.status == "released" and self.charged_microusd:
            raise ValueError("Undispatched releases cannot contain a charge.")
        reconciliation = (
            self.reconciliation_by,
            self.reconciliation_at,
            self.reconciliation_reason,
            self.reconciliation_evidence,
        )
        if (self.status == "reconciled") != all(value is not None for value in reconciliation):
            raise ValueError("Reconciliation requires complete actor, time, reason and evidence.")
        if self.status != "reconciled" and any(value is not None for value in reconciliation):
            raise ValueError("Only reconciled usage may carry reconciliation evidence.")
        if self.reconciliation_at is not None and self.reconciliation_at < self.deadline_at:
            raise ValueError("Reconciliation cannot precede the dispatch deadline.")
        if self.model_id is None and (self.model_version or self.credential_revision):
            raise ValueError("Imported usage has no model or credential revision.")
        if self.model_id is not None and self.model_version < 1:
            raise ValueError("Project model usage requires its positive revision.")
        return self


class UsageTotals(NativeModel):
    # Aggregate sums can exceed an individual bigint even though each entry cannot.
    charged_lifetime: int = Field(default=0, ge=0, strict=True)
    charged_day: int = Field(default=0, ge=0, strict=True)
    charged_month: int = Field(default=0, ge=0, strict=True)
    held_microusd: int = Field(default=0, ge=0, strict=True)
    active_calls: int = Field(default=0, ge=0, strict=True)


class ModelCommand(NativeCommand):
    model_config = ConfigDict(hide_input_in_errors=True)


class EnrollProjectModel(ModelCommand):
    template_id: ModelSlug
    label: str = Field(min_length=1, max_length=200)
    credential: SecretStr | None = Field(default=None, min_length=1, max_length=4096)


class UpdateProjectModel(ModelCommand, VersionedNativeCommand):
    label: str = Field(min_length=1, max_length=200)
    enabled: bool
    credential: SecretStr | None = Field(default=None, min_length=1, max_length=4096)


class UpdateModelResourcePolicy(ModelPolicyFields, ModelCommand):
    expected_version: int = Field(ge=0, strict=True)


class ProbeProjectModel(ModelCommand, VersionedNativeCommand):
    pass


class ReconcileModelUsage(ModelCommand, VersionedNativeCommand):
    charged_microusd: Amount
    reason: str = Field(min_length=10, max_length=1000)
    evidence: str = Field(min_length=10, max_length=2000)
