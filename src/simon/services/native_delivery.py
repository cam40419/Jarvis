"""Immutable, permission-checked delivery snapshots of exact artifact revisions."""

import hashlib
import html
import io
import json
import re
import zipfile
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError as ModelValidationError

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext
from simon.domain.native_artifacts import NativeArtifactRevision
from simon.domain.native_delivery import (
    CreateDeliveryPacket,
    DeliveryComponent,
    DeliveryPreview,
    DeliveryReviewState,
    NativeDeliveryManifest,
    NativeDeliveryPacket,
    manifest_bytes,
)
from simon.domain.ports import Store
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK
from simon.services.native_artifacts import (
    MAX_REVIEW_ATTEMPTS,
    NativeArtifactService,
    revision_metadata,
)

MAX_PACKET_CHAIN = 100
if TYPE_CHECKING:
    from simon.services.native_acceptance import NativeAcceptanceService

MAX_PACKETS_PER_PROJECT = 1000
MAX_PREVIEW_BYTES = 48_000
BASE_LIMITATIONS = (
    "Source dependencies are conservative project snapshots; field-level impact coverage "
    "has not been established.",
    "Generated previews show escaped UTF-8 text only. Native application rendering, editing "
    "and reopening have not been validated.",
    "A packet preserves component checks and decisions; it does not approve a package or "
    "complete additional tasks.",
    "Exported copies cannot be recalled after download. Current permissions are checked "
    "when the packet is accessed in Simon.",
)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def export_path(revision: NativeArtifactRevision) -> str:
    """Never let display filenames introduce paths, devices or cross-platform aliases."""
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", revision.filename).strip(" .")
    if not name:
        name = "source"
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", name.split(".")[0]):
        name = "file-" + name
    if len(name.encode("utf-8")) > 180:
        extension = re.search(r"\.[a-zA-Z0-9]{1,16}$", name)
        suffix = extension.group() if extension else ""
        name = name.encode("utf-8")[: 180 - len(suffix)].decode("utf-8", errors="ignore")
        name = name.rstrip(" .") + suffix
    return f"files/{revision.id}/{name}"


def render_preview(revision: NativeArtifactRevision, raw: bytes) -> tuple[DeliveryPreview, bytes]:
    """A deterministic text view, never execution or a claimed native-format render."""
    supported = revision.media_type.startswith("text/") or revision.media_type in {
        "application/json",
        "application/xml",
        "image/svg+xml",
    }
    limitation = ""
    text = ""
    if supported:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            supported = False
            limitation = "Source is not valid UTF-8; use its original editable file."
    if not supported and not limitation:
        limitation = "This file format has no validated preview renderer; use the original file."
    if supported and len(raw) > MAX_PREVIEW_BYTES:
        text = raw[:MAX_PREVIEW_BYTES].decode("utf-8", errors="ignore")
        limitation = "Text preview is truncated at 48,000 bytes; the full original is included."
    content = (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; base-uri 'none'; form-action 'none'\">"
        "<title>Source text preview</title><h1>"
        + html.escape(revision.filename)
        + "</h1><p>Escaped source text; native application rendering is not verified.</p><p>"
        + html.escape(limitation)
        + "</p><p>Source revision: "
        + str(revision.id)
        + "<br>SHA-256: "
        + revision.sha256
        + "</p><pre>"
        + html.escape(text)
        + "</pre></html>"
    ).encode("utf-8")
    return (
        DeliveryPreview(
            revision_id=revision.id,
            sha256=revision.sha256,
            path=f"previews/{revision.id}.html",
            content_sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            renderer="simon.safe-text.v1",
            supported=supported,
            limitation=limitation,
        ),
        content,
    )


class NativeDeliveryService:
    def __init__(self, store: Store, artifacts: NativeArtifactService) -> None:
        self.store, self.artifacts = store, artifacts
        self.projects = artifacts.projects
        self.acceptance: NativeAcceptanceService | None = None

    @staticmethod
    def _namespace(actor: ActorContext, project_id: UUID) -> str:
        return f"native_delivery:{actor.workspace_id}:{project_id}:{actor.actor_id}"

    def _packet(
        self, actor: ActorContext, project_id: UUID, packet_id: UUID
    ) -> NativeDeliveryPacket:
        packet = self.store.delivery_packet(actor.workspace_id, project_id, packet_id)
        if packet is None:
            raise NotFoundError("Delivery packet not found.")
        # Validate storage at the read boundary, including its canonical manifest checksum.
        return NativeDeliveryPacket.model_validate(packet.model_dump())

    def _source_authority(self, workspace_id: UUID, project_id: UUID) -> dict[str, Any]:
        return self.artifacts.source_authority(workspace_id, project_id)

    def _disclosure(self, packet: NativeDeliveryPacket) -> None:
        for identifier in packet.manifest.source_revision_ids:
            source = self.store.native_intake_source(
                packet.workspace_id, packet.project_id, identifier
            )
            if source is None or source.revoked_at is not None:
                raise AuthorizationError("Source access was revoked; packet content is withheld.")
        for component in packet.manifest.components:
            artifact = self.artifacts._artifact(
                packet.workspace_id, packet.project_id, component.artifact_id
            )
            revision = self.artifacts._revision(artifact, component.revision_id)
            self.artifacts.assert_disclosable_revision(artifact, revision)

    def _can_manage(self, actor: ActorContext, project_id: UUID) -> bool:
        try:
            self.artifacts._access(actor, project_id, write=True)
        except (AuthorizationError, InvalidTransitionError, NotFoundError):
            return False
        return True

    def _complete_component_dependencies(
        self, component: DeliveryComponent
    ) -> dict[str, Any] | None:
        if component.status_at_creation != "accepted" or not component.decisions:
            return None
        decision = component.decisions[-1]
        snapshot = decision.authority_snapshot.get("dependency_snapshot")
        if decision.decision != "approved" or not self.artifacts._complete_dependencies(snapshot):
            return None
        return snapshot

    def _applicability(self, packet: NativeDeliveryPacket) -> dict[str, Any]:
        reasons: list[str] = []
        accepted = True
        project = self.store.native_project(packet.workspace_id, packet.project_id)
        assert project is not None
        if project.version != packet.manifest.project_version or project.status != "active":
            reasons.append("Project requirements or state changed after this packet was captured.")
        if (
            self.artifacts._policy(packet.workspace_id, packet.project_id).version
            != packet.manifest.policy_version
        ):
            reasons.append("The review policy changed after this packet was captured.")
        dependencies = [
            self._complete_component_dependencies(component)
            for component in packet.manifest.components
        ]
        if all(snapshot is not None for snapshot in dependencies):
            for component, snapshot in zip(packet.manifest.components, dependencies, strict=True):
                assert snapshot is not None
                try:
                    self.artifacts.dependencies.assert_applicable(
                        packet.workspace_id, packet.project_id, component.revision_id, snapshot
                    )
                except (AuthorizationError, InvalidTransitionError, NotFoundError, ValidationError):
                    accepted = False
                    reasons.append(
                        f"Component {component.revision_id} dependencies changed; "
                        "reassessment is required."
                    )
        else:
            try:
                current_sources = self._source_authority(packet.workspace_id, packet.project_id)
            except ValidationError:
                accepted = False
                reasons.append(
                    "Source authority exceeds the supported inventory bound; "
                    "current applicability is unknown."
                )
            else:
                if current_sources["fingerprint"] != packet.manifest.source_fingerprint:
                    reasons.append(
                        "Project source evidence changed; affected components require reassessment."
                    )
        for component in packet.manifest.components:
            artifact = self.artifacts._artifact(
                packet.workspace_id, packet.project_id, component.artifact_id
            )
            task = self.artifacts._task(artifact)
            current_status = self.artifacts._status(artifact)
            if (
                artifact.current_revision_id != component.revision_id
                or artifact.version != component.artifact_version
            ):
                reasons.append(
                    f"Component {component.revision_id} has a newer revision or repair state."
                )
            elif (
                task.version != component.task_version
                or current_status != component.status_at_creation
            ):
                reasons.append(
                    f"Component {component.revision_id} has changed review or task state."
                )
            checks = self.store.artifact_checks(
                packet.workspace_id, packet.project_id, component.revision_id
            )
            decisions = self.store.artifact_decisions(
                packet.workspace_id, packet.project_id, component.revision_id
            )
            reviews = self._review_states(
                packet.workspace_id, packet.project_id, component.revision_id
            )
            if (
                checks != component.checks
                or decisions != component.decisions
                or reviews != component.review_states
            ):
                reasons.append(
                    f"Component {component.revision_id} has newer check, "
                    "review or decision evidence."
                )
            accepted = (
                accepted
                and artifact.current_revision_id == component.revision_id
                and current_status == "accepted"
            )
        return {"current": not reasons, "components_accepted": accepted, "reasons": reasons}

    def _review_states(
        self, workspace_id: UUID, project_id: UUID, revision_id: UUID
    ) -> tuple[DeliveryReviewState, ...]:
        return tuple(
            DeliveryReviewState(id=r.id, version=r.version, status=r.status, verdict=r.verdict)
            for r in self.store.artifact_reviews(
                workspace_id, project_id, revision_id, 0, MAX_REVIEW_ATTEMPTS
            )
        )

    def _detail(self, actor: ActorContext, packet: NativeDeliveryPacket) -> dict[str, Any]:
        self._disclosure(packet)
        return {
            "packet": packet.model_dump(mode="json"),
            "applicability": self._applicability(packet),
            "disclosure": "available",
            "successor_id": self._successor_id(packet),
            "can_manage": self._can_manage(actor, packet.project_id),
            "limitations": list(packet.manifest.limitations),
            "acceptance": self.acceptance.status(actor, packet.project_id, packet.id)
            if self.acceptance
            else {"assigned": False},
        }

    def _successor_id(self, packet: NativeDeliveryPacket) -> str | None:
        successor = self.store.delivery_packet_successor(
            packet.workspace_id, packet.project_id, packet.id
        )
        return str(successor.id) if successor else None

    def _summary(self, actor: ActorContext, packet: NativeDeliveryPacket) -> dict[str, Any]:
        self._disclosure(packet)
        return {
            "packet": {
                "id": str(packet.id),
                "sequence": packet.sequence,
                "created_at": packet.created_at.isoformat(),
                "previous_packet_id": str(packet.previous_packet_id)
                if packet.previous_packet_id
                else None,
                "manifest_sha256": packet.manifest_sha256,
                "manifest": {"title": packet.manifest.title},
            },
            "applicability": self._applicability(packet),
            "disclosure": "available",
            "successor_id": self._successor_id(packet),
            "can_manage": self._can_manage(actor, packet.project_id),
        }

    def detail(self, actor: ActorContext, project_id: UUID, packet_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, project_id)
            return self._detail(actor, self._packet(actor, project_id, packet_id))

    def view(
        self,
        actor: ActorContext,
        project_id: UUID,
        offset: int = 0,
        limit: int = 50,
        artifact_offset: int = 0,
        artifact_limit: int = 50,
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, project_id)
            self.projects._page(offset, limit)
            self.projects._page(artifact_offset, artifact_limit)
            items = []
            for packet in self.store.delivery_packets(
                actor.workspace_id, project_id, offset, limit
            ):
                try:
                    item = self._summary(actor, packet)
                except AuthorizationError:
                    item = {
                        "packet": {
                            "id": str(packet.id),
                            "sequence": packet.sequence,
                            "created_at": packet.created_at.isoformat(),
                            "manifest": {
                                "title": "Withheld delivery packet",
                                "components": [],
                                "previews": [],
                                "limitations": [],
                            },
                        },
                        "disclosure": "withheld",
                        "can_manage": False,
                        "applicability": {
                            "current": False,
                            "components_accepted": False,
                            "reasons": ["Source access was revoked; packet content is withheld."],
                        },
                        "limitations": [],
                    }
                items.append(item)
            candidates = []
            for artifact in self.store.artifacts(
                actor.workspace_id, project_id, offset=artifact_offset, limit=artifact_limit
            ):
                if not artifact.current_revision_id:
                    continue
                revision = self.artifacts._revision(artifact)
                try:
                    self.artifacts.assert_disclosable_revision(artifact, revision)
                except AuthorizationError:
                    continue
                candidates.append(
                    {
                        **artifact.model_dump(mode="json"),
                        "status": self.artifacts._status(artifact),
                        "current_revision": revision_metadata(revision),
                    }
                )
            return {
                "can_manage": self._can_manage(actor, project_id),
                "packets": {
                    "items": items,
                    "offset": offset,
                    "limit": limit,
                    "has_more": bool(
                        self.store.delivery_packets(
                            actor.workspace_id, project_id, offset + limit, 1
                        )
                    ),
                },
                "artifacts": {
                    "items": candidates,
                    "offset": artifact_offset,
                    "limit": artifact_limit,
                    "has_more": bool(
                        self.store.artifacts(
                            actor.workspace_id,
                            project_id,
                            offset=artifact_offset + artifact_limit,
                            limit=1,
                        )
                    ),
                },
            }

    def create(
        self, actor: ActorContext, project_id: UUID, command: CreateDeliveryPacket
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, project_id, write=True)

            def capture() -> dict[str, str]:
                if self.store.delivery_packets(
                    actor.workspace_id, project_id, MAX_PACKETS_PER_PROJECT - 1, 1
                ):
                    raise InvalidTransitionError(
                        "This project reached its 1,000 packet snapshot limit."
                    )
                previous = (
                    self._packet(actor, project_id, command.previous_packet_id)
                    if command.previous_packet_id
                    else None
                )
                if previous:
                    self._disclosure(previous)
                sequence = previous.sequence + 1 if previous else 1
                if sequence > MAX_PACKET_CHAIN:
                    raise InvalidTransitionError("The packet reached its 100-version limit.")
                project = self.store.native_project(actor.workspace_id, project_id)
                assert project is not None
                authority = self._source_authority(actor.workspace_id, project_id)
                components, previews, limitations = [], [], list(command.limitations)
                for requested in command.components:
                    artifact = self.artifacts._artifact(
                        actor.workspace_id, project_id, requested.artifact_id
                    )
                    revision, raw = self.artifacts.read_content(
                        actor, project_id, artifact.id, requested.revision_id
                    )
                    self.artifacts.assert_disclosable_revision(artifact, revision)
                    if (
                        artifact.current_revision_id != requested.revision_id
                        or revision.sha256 != requested.sha256
                    ):
                        raise InvalidTransitionError(
                            "Refresh changed components before capturing a delivery packet."
                        )
                    components.append(
                        DeliveryComponent(
                            **requested.model_dump(),
                            filename=revision.filename,
                            media_type=revision.media_type,
                            size_bytes=revision.size_bytes,
                            artifact_version=artifact.version,
                            task_version=self.artifacts._task(artifact).version,
                            status_at_creation=self.artifacts._status(artifact),
                            checks=self.store.artifact_checks(
                                actor.workspace_id, project_id, revision.id
                            ),
                            decisions=self.store.artifact_decisions(
                                actor.workspace_id, project_id, revision.id
                            ),
                            review_ids=tuple(
                                r.id
                                for r in self.store.artifact_reviews(
                                    actor.workspace_id,
                                    project_id,
                                    revision.id,
                                    0,
                                    MAX_REVIEW_ATTEMPTS,
                                )
                            ),
                            review_states=self._review_states(
                                actor.workspace_id, project_id, revision.id
                            ),
                        )
                    )
                    if requested.role == "source":
                        preview, _ = render_preview(revision, raw)
                        previews.append(preview)
                        if preview.limitation:
                            limitations.append(f"{revision.id}: {preview.limitation}")
                complete_dependencies = all(
                    self._complete_component_dependencies(component) is not None
                    for component in components
                )
                source_ids = authority["source_ids"]
                baseline_limits = BASE_LIMITATIONS
                if complete_dependencies:
                    identifiers: set[str] = set()
                    for component in components:
                        artifact = self.artifacts._artifact(
                            actor.workspace_id, project_id, component.artifact_id
                        )
                        revision = self.artifacts._revision(artifact, component.revision_id)
                        identifiers.update(self.artifacts.disclosure_source_ids(artifact, revision))
                    source_ids = sorted(identifiers)
                    baseline_limits = (
                        "Declared complete typed dependency coverage is rechecked for every "
                        "component. Original evidence remains subject to current disclosure "
                        "rights.",
                        *BASE_LIMITATIONS[1:],
                    )
                manifest = NativeDeliveryManifest(
                    title=command.title,
                    declared_limitations=command.limitations,
                    components=tuple(components),
                    previews=tuple(previews),
                    limitations=tuple(dict.fromkeys([*baseline_limits, *limitations])),
                    source_revision_ids=tuple(UUID(str(v)) for v in source_ids),
                    source_fingerprint=authority["fingerprint"],
                    project_version=project.version,
                    policy_version=self.artifacts._policy(actor.workspace_id, project_id).version,
                )
                packet = NativeDeliveryPacket(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    created_by=actor.actor_id,
                    previous_packet_id=command.previous_packet_id,
                    sequence=sequence,
                    manifest=manifest,
                    manifest_sha256=hashlib.sha256(manifest_bytes(manifest)).hexdigest(),
                )
                self.store.insert_delivery_packet(packet)
                self.artifacts._audit(
                    actor,
                    project_id,
                    "delivery_packet_created",
                    packet_id=str(packet.id),
                    manifest_sha256=packet.manifest_sha256,
                )
                return {"packet_id": str(packet.id)}

            try:
                receipt, _ = self.store.execute_once(
                    self._namespace(actor, project_id),
                    command.idempotency_key,
                    digest(command.model_dump(mode="json", exclude={"idempotency_key"})),
                    capture,
                )
            except ModelValidationError:
                raise ValidationError(
                    "Packet evidence exceeds the supported manifest bounds or contains invalid "
                    "records. Reduce the selected components and try again."
                ) from None
            return self._detail(actor, self._packet(actor, project_id, UUID(receipt["packet_id"])))

    def operation_receipt(self, actor: ActorContext, project_id: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, project_id, write=True)
            receipt = self.store.command_receipt(self._namespace(actor, project_id), key)
            if receipt is None:
                raise NotFoundError("Delivery packet operation not found.")
            return receipt

    def download(
        self, actor: ActorContext, project_id: UUID, packet_id: UUID
    ) -> tuple[NativeDeliveryPacket, bytes]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, project_id)
            packet = self._packet(actor, project_id, packet_id)
            self._disclosure(packet)
            files: dict[str, bytes] = {"manifest.json": manifest_bytes(packet.manifest)}
            paths: dict[str, str] = {}
            for component in packet.manifest.components:
                revision, raw = self.artifacts.read_content(
                    actor, project_id, component.artifact_id, component.revision_id
                )
                if (
                    revision.sha256 != component.sha256
                    or revision.size_bytes != component.size_bytes
                ):
                    raise InvalidTransitionError("Packet component integrity check failed.")
                paths[str(revision.id)] = export_path(revision)
                files[paths[str(revision.id)]] = raw
                if component.role == "source":
                    preview, rendered = render_preview(revision, raw)
                    expected = next(
                        (p for p in packet.manifest.previews if p.revision_id == revision.id), None
                    )
                    if preview != expected:
                        raise InvalidTransitionError(
                            "Packet renderer differs from its captured preview; export is withheld."
                        )
                    files[preview.path] = rendered
            files["export-paths.json"] = json_bytes(paths)
            if self.acceptance:
                files.update(self.acceptance.export_evidence(actor, project_id, packet_id))
            files["current-status.json"] = json_bytes(
                {
                    "packet_id": str(packet.id),
                    "manifest_sha256": packet.manifest_sha256,
                    "captured_at": packet.created_at.isoformat(),
                    "applicability": self._applicability(packet),
                    "disclosure": "authorized_at_download",
                    "limitations": packet.manifest.limitations,
                }
            )
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                for path, content in sorted(files.items()):
                    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, content)
            return packet, output.getvalue()
