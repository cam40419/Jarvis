"""Typed input revisions, exact output dependencies and immutable repair evidence."""

import hashlib
import json
import math
import re
from decimal import Decimal
from typing import Any, Literal, Self, cast
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from simon.domain.models import utc_now
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeModel

FactValueType = Literal["string", "number", "boolean", "json"]
MAX_FACT_VALUE_BYTES = 8 * 1024
MAX_DEPENDENCY_SNAPSHOT_BYTES = 256 * 1024


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def snapshot_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _numeric(value: int | float | Decimal) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Fact numbers require a numeric value.")
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("Fact numbers must be finite.")
    if not decimal:
        return "0"
    sign, digits_tuple, exponent = decimal.as_tuple()
    digits = list(digits_tuple)
    assert isinstance(exponent, int)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return ("-" if sign else "") + "".join(str(d) for d in digits) + "e" + str(exponent)


def _semantic_json(value: Any) -> Any:
    # Tag every JSON type so a user object cannot collide with a normalized number.
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, (int, float, Decimal)):
        return ["number", _numeric(value)]
    if isinstance(value, str):
        NativeModel.valid_text(value)
        return ["string", value]
    if isinstance(value, (tuple, list)):
        return ["array", [_semantic_json(item) for item in value]]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key in value:
            NativeModel.valid_text(key)
        return ["object", [[key, _semantic_json(value[key])] for key in sorted(value)]]
    raise ValueError("Fact values must contain only JSON data.")


def semantic_hash(value_type: FactValueType, value: Any, unit: str = "") -> str:
    """Normalize numeric rendering without Decimal context rounding or boolean aliases."""
    normalized = _numeric(value) if value_type == "number" else _semantic_json(value)
    return snapshot_hash({"value_type": value_type, "value": normalized, "unit": unit})


def bounded_snapshot(value: Any, maximum: int) -> Any:
    raw = canonical_bytes(value)
    if len(raw) > maximum:
        raise ValueError("Dependency snapshot exceeds its byte limit.")
    result = json.loads(raw)
    _semantic_json(result)  # Validate nested Unicode and reject unsupported structures.
    return result


class DependencySelector(NativeModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    kind: Literal["whole_file", "json_pointer"] = "whole_file"
    path: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def valid_path(self) -> Self:
        if self.kind == "whole_file" and self.path:
            raise ValueError("Whole-file dependencies cannot identify a field path.")
        if self.kind == "json_pointer" and (
            (self.path and not self.path.startswith("/")) or re.search(r"~(?![01])", self.path)
        ):
            raise ValueError("Dependency fields require a valid JSON Pointer.")
        return self


class FactSourceBinding(NativeModel):
    source_revision_id: UUID
    sha256: Fingerprint
    selector: DependencySelector = Field(default_factory=DependencySelector)


class NativeFact(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    kind: Literal["fact", "requirement"] = "fact"
    version: int = Field(default=1, ge=1, strict=True)
    current_revision_id: UUID | None = None
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class NativeFactRevision(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    fact_id: UUID
    version: int = Field(ge=1, strict=True)
    parent_revision_id: UUID | None = None
    value_type: FactValueType
    value: Any
    unit: str = Field(default="", max_length=64)
    sources: tuple[FactSourceBinding, ...] = Field(default=(), max_length=16)
    semantic_sha256: Fingerprint
    sha256: Fingerprint
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("value")
    @classmethod
    def bounded_value(cls, value: Any) -> Any:
        return bounded_snapshot(value, MAX_FACT_VALUE_BYTES)

    @model_validator(mode="after")
    def typed_value_and_parent(self) -> Self:
        if (self.version == 1) != (self.parent_revision_id is None):
            raise ValueError("Fact revision ancestry must match its version.")
        if self.parent_revision_id == self.id:
            raise ValueError("Fact revisions cannot identify themselves as their parent.")
        if (
            (self.value_type == "string" and not isinstance(self.value, str))
            or (self.value_type == "boolean" and not isinstance(self.value, bool))
            or (
                self.value_type == "number"
                and (
                    type(self.value) not in {int, float}
                    or (isinstance(self.value, float) and not math.isfinite(self.value))
                )
            )
        ):
            raise ValueError("Fact value does not match its declared type.")
        bounded_snapshot(self.value, MAX_FACT_VALUE_BYTES)
        identities = {
            (item.source_revision_id, item.selector.kind, item.selector.path)
            for item in self.sources
        }
        if len(identities) != len(self.sources):
            raise ValueError("Fact source bindings must be distinct.")
        body = {
            "value_type": self.value_type,
            "value": self.value,
            "unit": self.unit,
            "sources": [item.model_dump(mode="json") for item in self.sources],
        }
        if self.semantic_sha256 != semantic_hash(self.value_type, self.value, self.unit):
            raise ValueError("Fact semantic hash does not match its typed value.")
        if self.sha256 != snapshot_hash(body):
            raise ValueError("Fact snapshot hash does not match its value and evidence.")
        return self


class DependencyBinding(NativeModel):
    input_kind: Literal["fact", "artifact"]
    input_id: UUID
    revision_id: UUID
    sha256: Fingerprint
    input_selector: DependencySelector = Field(default_factory=DependencySelector)
    output_selector: DependencySelector = Field(default_factory=DependencySelector)


class NativeDependencyDeclaration(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    artifact_id: UUID
    revision_id: UUID
    sha256: Fingerprint
    version: int = Field(ge=1, strict=True)
    coverage: Literal["complete", "partial", "unknown"]
    dependencies: tuple[DependencyBinding, ...] = Field(default=(), max_length=32)
    notes: str = Field(default="", max_length=2000)
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def distinct_dependencies(self) -> Self:
        values = [canonical_bytes(item) for item in self.dependencies]
        if len(set(values)) != len(values):
            raise ValueError("Output dependencies must be distinct.")
        return self


class NativeImpactAssessment(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    fact_id: UUID
    from_revision_id: UUID
    to_revision_id: UUID
    graph_fingerprint: Fingerprint
    source_fingerprint: Fingerprint
    outputs: tuple[dict[str, Any], ...]
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("outputs")
    @classmethod
    def bounded_outputs(cls, value: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        return tuple(bounded_snapshot(value, MAX_DEPENDENCY_SNAPSHOT_BYTES))


class NativeRepairProposal(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    assessment_id: UUID
    sha256: Fingerprint
    plan: dict[str, Any]
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("plan")
    @classmethod
    def bounded_plan(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], bounded_snapshot(value, MAX_DEPENDENCY_SNAPSHOT_BYTES))

    @model_validator(mode="after")
    def exact_plan_hash(self) -> Self:
        if self.sha256 != snapshot_hash(self.plan):
            raise ValueError("Repair proposal hash does not match its immutable plan.")
        return self
