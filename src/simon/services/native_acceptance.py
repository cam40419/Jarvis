"""Bounded local text/JSON package checks and exact owner package acceptance."""

import base64
import hashlib
import importlib.metadata
import platform
from copy import deepcopy
from typing import Any, cast
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as SchemaValidationError

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel
from simon.domain.native_acceptance import (
    PROFILE_NAME,
    PROFILE_VERSION,
    VALIDATOR_KEY,
    VALIDATOR_VERSION,
    AcceptDeliveryPacket,
    AssignDeliveryProfile,
    NativeDeliveryAcceptance,
    NativeDeliveryProfileAssignment,
    NativeDeliveryValidation,
    PackageCheck,
    ValidateDeliveryPacket,
)
from simon.domain.native_delivery import NativeDeliveryPacket
from simon.domain.native_dependencies import (
    bounded_snapshot,
    canonical_bytes,
    semantic_hash,
    snapshot_hash,
)
from simon.domain.native_projects import NativeCommand
from simon.domain.ports import Store
from simon.services.dependency_graph import select, strict_json, tokens
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES
from simon.services.native_delivery import MAX_PREVIEW_BYTES, NativeDeliveryService, render_preview

MAX_PACKAGE_HISTORY = 20
MAX_PACKAGE_EXPORT_BYTES = 4 * 1024 * 1024
SCHEMA_KEYWORDS = {
    "$schema",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "enum",
    "const",
    "minimum",
    "maximum",
    "title",
    "description",
}
PROFILE: dict[str, Any] = {
    "name": PROFILE_NAME,
    "version": PROFILE_VERSION,
    "roles": {
        "json_specification": {
            "count": 1,
            "media_types": ["application/json"],
            "extension": ".json",
        },
        "markdown_rationale": {
            "count": 1,
            "media_types": ["text/markdown", "text/plain"],
            "extension": ".md",
        },
        "schema_and_check_report": {
            "count": 1,
            "media_types": ["application/json"],
            "extension": ".json",
        },
        "derived_safe_preview": {"count": 3, "renderer": "simon.safe-text.v1"},
    },
    "source_count": 3,
    "dependency_coverage": "complete declarations required for all components",
    "maximum_source_bytes": MAX_PREVIEW_BYTES,
    "schema_report": {"schema_version": 1, "keys": ["schema_version", "schema"]},
    "schema_keywords": sorted(SCHEMA_KEYWORDS),
    "schema_nodes": 128,
    "schema_depth": 12,
    "validator_key": VALIDATOR_KEY,
    "validator_version": VALIDATOR_VERSION,
    "application": "Local UTF-8 text and JSON parsing/canonical serialization",
    "editing": "One existing JSON Pointer field; save, reopen, schema check and safe text render",
    "limitations": [
        "This profile verifies local editable UTF-8 text/JSON and escaped previews only.",
        "It does not validate native CAD, office, creative application, layout or visual quality.",
        "Schema constraints and declared dependency coverage remain owner-defined.",
    ],
}


def profile_catalog() -> dict[str, Any]:
    result = deepcopy(PROFILE)
    result["sha256"] = snapshot_hash(PROFILE)
    result["validator"] = {
        "profile": result["sha256"],
        "validator_key": VALIDATOR_KEY,
        "validator_version": VALIDATOR_VERSION,
        "jsonschema": importlib.metadata.version("jsonschema"),
        "python": platform.python_version(),
        "renderer": "simon.safe-text.v1",
    }
    result["validator_sha256"] = snapshot_hash(result["validator"])
    return result


def bounded_schema(schema: Any) -> None:
    visits = 0

    def inspect(value: Any, depth: int) -> None:
        nonlocal visits
        visits += 1
        if visits > 128 or depth > 12 or not isinstance(value, dict):
            raise ValidationError("Schema exceeds its supported structure or traversal bound.")
        if set(value) - SCHEMA_KEYWORDS:
            raise ValidationError(
                "Schema contains an unsupported keyword; references are disabled."
            )
        if (
            "$schema" in value
            and value["$schema"] != "https://json-schema.org/draft/2020-12/schema"
        ):
            raise ValidationError("Only the registered local JSON Schema dialect is supported.")
        if "properties" in value:
            properties = value["properties"]
            if not isinstance(properties, dict) or len(properties) > 64:
                raise ValidationError("Schema property inventory exceeds its bound.")
            for child in properties.values():
                inspect(child, depth + 1)
        for key in ("items", "additionalProperties"):
            if isinstance(value.get(key), dict):
                inspect(value[key], depth + 1)
        if isinstance(value.get("enum"), list) and len(value["enum"]) > 32:
            raise ValidationError("Schema enumeration exceeds its supported bound.")

    inspect(schema, 0)
    if (
        schema.get("type") != "object"
        or not schema.get("properties")
        or not schema.get("required")
        or schema.get("additionalProperties") is not False
    ):
        raise ValidationError(
            "The specification schema requires explicit object properties, required fields "
            "and additionalProperties:false."
        )
    Draft202012Validator.check_schema(schema)


class NativeAcceptanceService:
    def __init__(self, store: Store, delivery: NativeDeliveryService) -> None:
        self.store, self.delivery, self.artifacts = store, delivery, delivery.artifacts
        self.delivery.acceptance = self

    @staticmethod
    def _namespace(actor: ActorContext, pid: UUID, packet_id: UUID) -> str:
        return f"native_acceptance:{actor.workspace_id}:{pid}:{packet_id}:{actor.actor_id}"

    def _packet(
        self, actor: ActorContext, pid: UUID, packet_id: UUID, *, write: bool = False
    ) -> NativeDeliveryPacket:
        self.artifacts._access(actor, pid, write=write)
        packet = self.delivery._packet(actor, pid, packet_id)
        self.delivery._disclosure(packet)
        return packet

    def _assignment(self, packet: NativeDeliveryPacket) -> NativeDeliveryProfileAssignment | None:
        return self.store.delivery_profile_assignment(
            packet.workspace_id, packet.project_id, packet.id
        )

    def _history(self, packet: NativeDeliveryPacket, kind: str) -> tuple[Any, ...]:
        method = getattr(
            self.store,
            {
                "profile": "delivery_profile_assignments",
                "validation": "delivery_validations",
                "acceptance": "delivery_acceptances",
            }[kind],
        )
        values = method(
            packet.workspace_id, packet.project_id, packet.id, limit=MAX_PACKAGE_HISTORY + 1
        )
        if len(values) > MAX_PACKAGE_HISTORY:
            raise ValidationError("Package evidence history exceeds its supported capacity.")
        return cast(tuple[Any, ...], values)

    @staticmethod
    def _registered(assignment: NativeDeliveryProfileAssignment) -> None:
        catalog = profile_catalog()
        if (
            assignment.profile_name,
            assignment.profile_version,
            assignment.profile_sha256,
            assignment.validator_key,
            assignment.validator_version,
        ) != (PROFILE_NAME, PROFILE_VERSION, catalog["sha256"], VALIDATOR_KEY, VALIDATOR_VERSION):
            raise InvalidTransitionError(
                "Profile or validator is unknown or changed; assign the registered version again."
            )

    def _authority(
        self, packet: NativeDeliveryPacket, assignment: NativeDeliveryProfileAssignment
    ) -> dict[str, Any]:
        ws, pid = packet.workspace_id, packet.project_id
        self.delivery._disclosure(packet)
        catalog = profile_catalog()
        project = self.store.native_project(ws, pid)
        assert project is not None
        components = []
        for item in packet.manifest.components:
            artifact = self.artifacts._artifact(ws, pid, item.artifact_id)
            revision = self.artifacts._revision(artifact, item.revision_id)
            task = self.artifacts._task(artifact)
            dependency = self.artifacts.dependency_snapshot(revision)
            dependency_authority = (
                {
                    k: dependency[k]
                    for k in (
                        "coverage",
                        "fingerprint",
                        "ready",
                        "declaration_id",
                        "declaration_version",
                    )
                }
                if dependency
                else None
            )
            components.append(
                {
                    "artifact_id": str(artifact.id),
                    "revision_id": str(revision.id),
                    "sha256": revision.sha256,
                    "head": str(artifact.current_revision_id),
                    "artifact_version": artifact.version,
                    "task_version": task.version,
                    "status": self.artifacts._status(artifact),
                    "checks": [
                        {"id": str(c.id), "record_sha256": snapshot_hash(c)}
                        for c in self.store.artifact_checks(ws, pid, revision.id)
                    ],
                    "decisions": [
                        {"id": str(d.id), "record_sha256": snapshot_hash(d)}
                        for d in self.store.artifact_decisions(ws, pid, revision.id)
                    ],
                    "reviews": [
                        r.model_dump(mode="json")
                        for r in self.delivery._review_states(ws, pid, revision.id)
                    ],
                    "dependency_snapshot": dependency_authority,
                }
            )
        result = {
            "manifest_sha256": packet.manifest_sha256,
            "assignment_id": str(assignment.id),
            "assignment_version": assignment.version,
            "profile_sha256": catalog["sha256"],
            "validator_sha256": catalog["validator_sha256"],
            "project_version": project.version,
            "project_status": project.status,
            "policy_version": self.artifacts._policy(ws, pid).version,
            "source_authority": {
                "scope": "typed_dependencies",
                "components": [
                    {"revision_id": c["revision_id"], "dependency": c["dependency_snapshot"]}
                    for c in components
                ],
            }
            if all(
                self.delivery._complete_component_dependencies(c) is not None
                for c in packet.manifest.components
            )
            else self.delivery._source_authority(ws, pid),
            "successor_id": self.delivery._successor_id(packet),
            "components": components,
        }
        try:
            return cast(dict[str, Any], bounded_snapshot(result, 256 * 1024))
        except ValueError as exc:
            raise ValidationError(
                "Package authority exceeds its bounded validation capacity."
            ) from exc

    def _checks(
        self,
        actor: ActorContext,
        packet: NativeDeliveryPacket,
        assignment: NativeDeliveryProfileAssignment,
    ) -> tuple[tuple[PackageCheck, ...], dict[str, Any]]:
        checks: list[PackageCheck] = []
        evidence: dict[str, Any] = {"originals": [], "previews": [], "probe": None}
        roles = {binding.role: binding.revision_id for binding in assignment.roles}
        expected_roles = {"json_specification", "markdown_rationale", "schema_and_check_report"}
        components = {c.revision_id: c for c in packet.manifest.components}
        complete = (
            len(assignment.roles) == 3
            and set(roles) == expected_roles
            and len(set(roles.values())) == 3
            and set(roles.values()) == set(components)
            and all(c.role == "source" for c in components.values())
        )
        checks.append(
            PackageCheck(
                name="required_roles",
                status="passed" if complete else "failed",
                message="Exactly one JSON specification, Markdown rationale and JSON schema "
                "report original are required.",
            )
        )
        if not complete:
            return tuple(checks), evidence
        originals: dict[str, tuple[Any, bytes]] = {}
        for role, identifier in roles.items():
            component = components[identifier]
            revision, raw = self.artifacts.read_content(
                actor, packet.project_id, component.artifact_id, identifier
            )
            rule = PROFILE["roles"][role]
            valid = (
                revision.sha256 == component.sha256
                and len(raw) == component.size_bytes
                and 0 < len(raw) <= MAX_PREVIEW_BYTES
                and revision.media_type in rule["media_types"]
                and revision.filename.lower().endswith(rule["extension"])
            )
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                valid = False
            checks.append(
                PackageCheck(
                    name="original_" + role,
                    status="passed" if valid else "failed",
                    message="Original must match its manifest and supported complete "
                    "UTF-8 format/size.",
                )
            )
            originals[role] = (revision, raw)
            evidence["originals"].append(
                {
                    "role": role,
                    "revision_id": str(identifier),
                    "sha256": revision.sha256,
                    "size_bytes": len(raw),
                    "reopened_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        for identifier in roles.values():
            revision, raw = next(value for value in originals.values() if value[0].id == identifier)
            preview, content = render_preview(revision, raw)
            expected = next(
                (p for p in packet.manifest.previews if p.revision_id == identifier), None
            )
            valid = preview == expected and preview.supported and not preview.limitation
            checks.append(
                PackageCheck(
                    name="preview_" + str(identifier),
                    status="passed" if valid else "failed",
                    message="The safe preview must derive from the exact complete source "
                    "and renderer.",
                )
            )
            evidence["previews"].append(
                {
                    "revision_id": str(identifier),
                    "source_sha256": revision.sha256,
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )
        if any(c.status == "failed" for c in checks):
            return tuple(checks), evidence
        try:
            specification_revision, specification_raw = originals["json_specification"]
            specification = strict_json(specification_raw.decode("utf-8"))
            report = strict_json(originals["schema_and_check_report"][1].decode("utf-8"))
            if (
                not isinstance(report, dict)
                or set(report) != {"schema_version", "schema"}
                or type(report["schema_version"]) is not int
                or report["schema_version"] != 1
            ):
                raise ValidationError(
                    "Schema report must contain only schema_version:1 and the local schema; "
                    "passed claims are not accepted."
                )
            schema = report["schema"]
            bounded_schema(schema)
            validator = Draft202012Validator(schema)
            validator.validate(specification)
            checks.append(PackageCheck(name="local_schema", status="passed"))
            probe = assignment.probe
            original_value = select(specification, probe.selector)
            if semantic_hash("json", original_value) != semantic_hash("json", probe.before):
                raise ValidationError(
                    "The declared probe before-value does not match the exact source."
                )
            if semantic_hash("json", probe.before) == semantic_hash("json", probe.after):
                raise ValidationError("The probe must perform a real semantic edit.")
            edited = deepcopy(specification)
            path = tokens(probe.selector)
            target = edited
            for token in path[:-1]:
                target = target[int(token)] if isinstance(target, list) else target[token]
            key: int | str = int(path[-1]) if isinstance(target, list) else path[-1]
            target[key] = deepcopy(probe.after)
            edited_raw = canonical_bytes(edited)
            if len(edited_raw) > MAX_PREVIEW_BYTES:
                raise ValidationError(
                    "Edited source exceeds this profile's complete preview bound."
                )
            reopened = strict_json(edited_raw.decode("utf-8"))
            validator.validate(reopened)
            if semantic_hash("json", select(reopened, probe.selector)) != semantic_hash(
                "json", probe.after
            ):
                raise ValidationError("Edited field did not survive save/reopen.")
            target = reopened
            for token in path[:-1]:
                target = target[int(token)] if isinstance(target, list) else target[token]
            target[key] = original_value
            if canonical_bytes(reopened) != canonical_bytes(specification):
                raise ValidationError("The edit changed unrelated JSON data.")
            edited_sha = hashlib.sha256(edited_raw).hexdigest()
            edited_revision = specification_revision.model_copy(
                update={
                    "sha256": edited_sha,
                    "size_bytes": len(edited_raw),
                    "content_base64": base64.b64encode(edited_raw).decode(),
                }
            )
            edited_preview, rendered = render_preview(edited_revision, edited_raw)
            if (
                not edited_preview.supported
                or edited_preview.limitation
                or edited_preview.sha256 != edited_sha
            ):
                raise ValidationError("Edited source has no complete safe rendered preview.")
            evidence["probe"] = {
                "source_revision_id": str(specification_revision.id),
                "source_sha256": specification_revision.sha256,
                "edited_source_sha256": edited_sha,
                "edited_source_size_bytes": len(edited_raw),
                "reopened_edited_sha256": edited_sha,
                "edited_preview_sha256": hashlib.sha256(rendered).hexdigest(),
                "edited_preview_size_bytes": len(rendered),
                "selector": probe.selector.model_dump(mode="json"),
                "unrelated_data_preserved": True,
                "published": False,
            }
            checks.append(PackageCheck(name="edit_save_reopen_render", status="passed"))
        except (
            ValidationError,
            SchemaError,
            SchemaValidationError,
            ValueError,
            KeyError,
            TypeError,
            RecursionError,
        ) as exc:
            checks.append(
                PackageCheck(
                    name="local_schema"
                    if not any(c.name == "local_schema" for c in checks)
                    else "edit_save_reopen_render",
                    status="failed",
                    message=str(exc)[:2000],
                )
            )
        return tuple(checks), evidence

    def _once(
        self,
        actor: ActorContext,
        packet: NativeDeliveryPacket,
        command: NativeCommand,
        kind: str,
        operation: Any,
    ) -> UUID:
        value, _ = self.store.execute_once(
            self._namespace(actor, packet.project_id, packet.id),
            command.idempotency_key,
            snapshot_hash(
                {
                    "kind": kind,
                    "command": command.model_dump(mode="json", exclude={"idempotency_key"}),
                }
            ),
            lambda: {"kind": kind, "id": str(operation())},
        )
        return UUID(value["id"])

    def assign(
        self, actor: ActorContext, pid: UUID, packet_id: UUID, command: AssignDeliveryProfile
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            packet = self._packet(actor, pid, packet_id, write=True)

            def save() -> UUID:
                if (command.profile_name, command.profile_version, command.validator_version) != (
                    PROFILE_NAME,
                    PROFILE_VERSION,
                    VALIDATOR_VERSION,
                ):
                    raise ValidationError("Unknown delivery profile or validator version.")
                previous = self._assignment(packet)
                if (previous.version if previous else 0) != command.expected_version:
                    raise InvalidTransitionError(
                        "Profile assignment changed; refresh its current version."
                    )
                if len(self._history(packet, "profile")) >= MAX_PACKAGE_HISTORY:
                    raise ValidationError("Package profile history capacity reached.")
                catalog = profile_catalog()
                value = NativeDeliveryProfileAssignment(
                    workspace_id=actor.workspace_id,
                    project_id=pid,
                    packet_id=packet.id,
                    manifest_sha256=packet.manifest_sha256,
                    created_by=actor.actor_id,
                    version=command.expected_version + 1,
                    profile_name=command.profile_name,
                    profile_version=command.profile_version,
                    profile_sha256=catalog["sha256"],
                    profile_snapshot=deepcopy(PROFILE),
                    validator_key=VALIDATOR_KEY,
                    validator_version=command.validator_version,
                    roles=command.roles,
                    probe=command.probe,
                )
                self.store.insert_delivery_profile_assignment(value, command.expected_version)
                self.export_evidence(actor, pid, packet.id)
                self.artifacts._audit(
                    actor,
                    pid,
                    "delivery_profile_assigned",
                    packet_id=str(packet.id),
                    assignment_id=str(value.id),
                )
                return value.id

            identifier = self._once(actor, packet, command, "profile", save)
            return {**self.detail(actor, pid, packet_id), "operation_id": str(identifier)}

    def _target(
        self, packet: NativeDeliveryPacket, command: ValidateDeliveryPacket
    ) -> NativeDeliveryProfileAssignment:
        assignment = self._assignment(packet)
        if assignment is None or (assignment.id, assignment.version, packet.manifest_sha256) != (
            command.assignment_id,
            command.expected_profile_version,
            command.manifest_sha256,
        ):
            raise InvalidTransitionError(
                "Validation requires the current exact packet and profile assignment."
            )
        self._registered(assignment)
        if command.validator_version != VALIDATOR_VERSION:
            raise ValidationError("Unknown package validator version.")
        return assignment

    def validate(
        self, actor: ActorContext, pid: UUID, packet_id: UUID, command: ValidateDeliveryPacket
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            packet = self._packet(actor, pid, packet_id, write=True)

            def run() -> UUID:
                assignment = self._target(packet, command)
                if len(self._history(packet, "validation")) >= MAX_PACKAGE_HISTORY:
                    raise ValidationError("Package validation history capacity reached.")
                authority = self._authority(packet, assignment)
                checks, evidence = self._checks(actor, packet, assignment)
                value = NativeDeliveryValidation(
                    workspace_id=actor.workspace_id,
                    project_id=pid,
                    packet_id=packet.id,
                    manifest_sha256=packet.manifest_sha256,
                    created_by=actor.actor_id,
                    assignment_id=assignment.id,
                    profile_version=assignment.version,
                    profile_sha256=assignment.profile_sha256,
                    validator_key=VALIDATOR_KEY,
                    validator_version=VALIDATOR_VERSION,
                    validator_sha256=profile_catalog()["validator_sha256"],
                    validator_snapshot=profile_catalog()["validator"],
                    passed=all(c.status == "passed" for c in checks),
                    checks=checks,
                    evidence=evidence,
                    authority=authority,
                    authority_sha256=snapshot_hash(authority),
                    limitations=tuple(PROFILE["limitations"]),
                )
                self.store.insert_delivery_validation(value)
                self.export_evidence(actor, pid, packet.id)
                self.artifacts._audit(
                    actor,
                    pid,
                    "delivery_validated",
                    packet_id=str(packet.id),
                    validation_id=str(value.id),
                    passed=value.passed,
                )
                return value.id

            identifier = self._once(actor, packet, command, "validate", run)
            return {**self.detail(actor, pid, packet_id), "operation_id": str(identifier)}

    def _ready(
        self,
        packet: NativeDeliveryPacket,
        assignment: NativeDeliveryProfileAssignment,
        validation: NativeDeliveryValidation | None,
    ) -> list[str]:
        reasons: list[str] = []
        try:
            self._registered(assignment)
            authority = self._authority(packet, assignment)
            applicability = self.delivery._applicability(packet)
            reasons.extend(applicability["reasons"])
            if not applicability["components_accepted"]:
                reasons.append("Every component requires its current exact approval.")
            if any(
                self.delivery._complete_component_dependencies(c) is None
                for c in packet.manifest.components
            ):
                reasons.append(
                    "This profile requires complete declared dependency coverage "
                    "for every component."
                )
            if authority["successor_id"] is not None:
                reasons.append("A newer packet version superseded this package.")
            if validation is None or not validation.passed:
                reasons.append("The current profile requires a passed deterministic validation.")
            elif (
                validation.assignment_id != assignment.id
                or validation.profile_version != assignment.version
                or validation.authority_sha256 != snapshot_hash(authority)
                or validation.validator_sha256 != profile_catalog()["validator_sha256"]
            ):
                reasons.append("Validation authority changed; revalidate the current package.")
        except (AuthorizationError, InvalidTransitionError, ValidationError, NotFoundError):
            reasons.append("Profile, source or dependency authority is no longer applicable.")
        return list(dict.fromkeys(reasons))

    def accept(
        self, actor: ActorContext, pid: UUID, packet_id: UUID, command: AcceptDeliveryPacket
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            packet = self._packet(actor, pid, packet_id, write=True)

            def save() -> UUID:
                assignment = self._target(packet, command)
                validations = self._history(packet, "validation")
                validation = validations[-1] if validations else None
                reasons = self._ready(packet, assignment, validation)
                if (
                    validation is None
                    or validation.id != command.validation_id
                    or validation.authority_sha256 != command.authority_sha256
                    or reasons
                ):
                    raise InvalidTransitionError(
                        "Package acceptance is blocked: "
                        + "; ".join(reasons or ["Validation identity changed."])
                    )
                checks, evidence = self._checks(actor, packet, assignment)
                if checks != validation.checks or evidence != validation.evidence:
                    raise InvalidTransitionError(
                        "Deterministic validation evidence no longer reproduces."
                    )
                if len(self._history(packet, "acceptance")) >= MAX_PACKAGE_HISTORY:
                    raise ValidationError("Package acceptance history capacity reached.")
                value = NativeDeliveryAcceptance(
                    workspace_id=actor.workspace_id,
                    project_id=pid,
                    packet_id=packet.id,
                    manifest_sha256=packet.manifest_sha256,
                    created_by=actor.actor_id,
                    assignment_id=assignment.id,
                    profile_version=assignment.version,
                    validation_id=validation.id,
                    authority_sha256=validation.authority_sha256,
                    owner_authority=self.artifacts._owner_authority(actor, pid),
                    feedback=command.feedback,
                )
                self.store.insert_delivery_acceptance(value)
                self.export_evidence(actor, pid, packet.id)
                self.artifacts._audit(
                    actor,
                    pid,
                    "delivery_accepted",
                    packet_id=str(packet.id),
                    acceptance_id=str(value.id),
                )
                return value.id

            identifier = self._once(actor, packet, command, "accept", save)
            return {**self.detail(actor, pid, packet_id), "operation_id": str(identifier)}

    def status(self, actor: ActorContext, pid: UUID, packet_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid)
            packet = self.delivery._packet(actor, pid, packet_id)
            assignment = self._assignment(packet)
            try:
                self.delivery._disclosure(packet)
            except AuthorizationError:
                return {
                    "assigned": assignment is not None,
                    "disclosure": "withheld",
                    "can_manage": False,
                    "can_accept": False,
                    "assignment": None,
                    "latest_validation": None,
                    "latest_acceptance": None,
                    "applicability": {
                        "current": False,
                        "accepted": False,
                        "reasons": ["Source access was revoked; package evidence is withheld."],
                    },
                }
            if assignment is None:
                return {
                    "assigned": False,
                    "profile": profile_catalog(),
                    "disclosure": "available",
                    "assignment": None,
                    "latest_validation": None,
                    "latest_acceptance": None,
                    "can_manage": self.delivery._can_manage(actor, pid),
                    "can_accept": False,
                    "applicability": {
                        "current": False,
                        "accepted": False,
                        "reasons": ["Assign a registered package profile."],
                    },
                }
            validations, acceptances = (
                self._history(packet, "validation"),
                self._history(packet, "acceptance"),
            )
            validation, acceptance = (
                (validations[-1] if validations else None),
                (acceptances[-1] if acceptances else None),
            )
            reasons = self._ready(packet, assignment, validation)
            accepted = acceptance is not None and not reasons
            if accepted:
                assert acceptance is not None and validation is not None
                if (
                    acceptance.assignment_id != assignment.id
                    or acceptance.validation_id != validation.id
                    or acceptance.authority_sha256 != validation.authority_sha256
                ):
                    accepted = False
                    reasons.append(
                        "The prior owner acceptance binds older profile or validation evidence."
                    )
                else:
                    membership = self.artifacts.projects._membership(
                        packet.workspace_id, acceptance.created_by
                    )
                    try:
                        if membership is None:
                            raise AuthorizationError("Accepting owner lost access.")
                        owner = ActorContext(
                            actor_id=acceptance.created_by,
                            workspace_id=packet.workspace_id,
                            channel=Channel.API,
                            scopes=ROLE_SCOPES[membership.role],
                        )
                        if (
                            self.artifacts._owner_authority(owner, pid)
                            != acceptance.owner_authority
                        ):
                            raise AuthorizationError("Accepting owner authority changed.")
                    except (AuthorizationError, NotFoundError, InvalidTransitionError):
                        accepted = False
                        reasons.append("The accepting owner's project authority changed.")
            can_manage = self.delivery._can_manage(actor, pid)
            return {
                "assigned": True,
                "profile": profile_catalog(),
                "disclosure": "available",
                "assignment": assignment.model_dump(mode="json"),
                "latest_validation": validation.model_dump(mode="json") if validation else None,
                "latest_acceptance": acceptance.model_dump(mode="json") if acceptance else None,
                "can_manage": can_manage,
                "can_accept": can_manage and not self._ready(packet, assignment, validation),
                "applicability": {"current": not reasons, "accepted": accepted, "reasons": reasons},
            }

    def detail(self, actor: ActorContext, pid: UUID, packet_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            result = self.status(actor, pid, packet_id)
            if result["disclosure"] != "available":
                return {**result, "assignments": [], "validations": [], "acceptances": []}
            packet = self._packet(actor, pid, packet_id)
            return {
                **result,
                "packet": packet.model_dump(mode="json"),
                "assignments": [
                    v.model_dump(mode="json") for v in self._history(packet, "profile")
                ],
                "validations": [
                    v.model_dump(mode="json") for v in self._history(packet, "validation")
                ],
                "acceptances": [
                    v.model_dump(mode="json") for v in self._history(packet, "acceptance")
                ],
            }

    def operation_receipt(
        self, actor: ActorContext, pid: UUID, packet_id: UUID, key: str
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._packet(actor, pid, packet_id, write=True)
            value = self.store.command_receipt(self._namespace(actor, pid, packet_id), key)
            if value is None:
                raise NotFoundError("Package acceptance operation not found.")
            return value

    def export_evidence(self, actor: ActorContext, pid: UUID, packet_id: UUID) -> dict[str, bytes]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            packet = self._packet(actor, pid, packet_id)
            assignment = self._assignment(packet)
            if assignment is None:
                return {}
            result = {
                "package/profile.json": canonical_bytes(assignment.profile_snapshot),
                "package/current-status.json": canonical_bytes(self.status(actor, pid, packet_id)),
            }
            for kind in ("profile", "validation", "acceptance"):
                for item in self._history(packet, kind):
                    result[f"package/{kind}/{item.id}.json"] = canonical_bytes(item)
                    if kind == "profile":
                        result[f"package/profile-definitions/{item.profile_sha256}.json"] = (
                            canonical_bytes(item.profile_snapshot)
                        )
                    elif kind == "validation":
                        result[f"package/validator-definitions/{item.validator_sha256}.json"] = (
                            canonical_bytes(item.validator_snapshot)
                        )
            if sum(len(raw) for raw in result.values()) > MAX_PACKAGE_EXPORT_BYTES:
                raise ValidationError("Package acceptance evidence exceeds the 4 MiB export bound.")
            return result
