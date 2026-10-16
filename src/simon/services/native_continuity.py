"""Permission-aware continuity snapshots and native checkpoint succession."""

import base64
import binascii
import hashlib
import io
import json
import zipfile
from typing import Any, cast
from uuid import UUID

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_continuity import (
    MAX_CONTINUITY_EXPORT_BYTES,
    ContinuityArtifactRef,
    ContinuityContextChangedError,
    ContinuityRunRef,
    CreateContinuityHandoff,
    NativeContinuityHandoff,
    NativeContinuityManifest,
    ResumeContinuityHandoff,
    VerifyContinuityBundle,
    continuity_manifest_bytes,
)
from simon.domain.native_execution import (
    ExecutionCommand,
    NativeExecutionRun,
    NativeExecutionRunner,
)
from simon.domain.ports import Store
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES
from simon.services.intake_sources import redact
from simon.services.native_artifacts import NativeArtifactService
from simon.services.native_delivery import NativeDeliveryService, export_path
from simon.services.native_dependencies import NativeDependencyService
from simon.services.native_execution import NativeExecutionService
from simon.services.native_projects import NativeProjectService
from simon.services.native_storage import NativeStorageService
from simon.services.native_workflows import (
    NativeWorkflowService,
    public_policy,
    public_run,
    public_runner,
)

AUTHORITY_ERRORS = (AuthorizationError, InvalidTransitionError, NotFoundError, ValidationError)
LIMITATIONS = (
    "This handoff supports the same Simon project and enrolled native workers only; "
    "foreign runtime import is blocked.",
    "A handoff transfers no credentials, membership, owner role, execution grant "
    "or provider authority.",
    "Source bodies and provider prompts are omitted. Source dependencies for execution "
    "context are conservative project references.",
    "Unknown effects and held liabilities require explicit reconciliation; "
    "completed checkpoints are retained without redispatch.",
    "Exported copies cannot be recalled. Every subsequent platform read, export and "
    "continuation rechecks current authority.",
)


def safe_text(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {key: safe_text(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_text(item) for item in value]
    return value


def continuity_binding(store: Store, run: NativeExecutionRun) -> dict[str, Any] | None:
    events = store.execution_events(run.workspace_id, run.project_id, run.id, 0, 1001)
    if len(events) > 1000:
        raise ValidationError("Continuity authority exceeds the supported execution event bound.")
    return next(
        (event.details for event in reversed(events) if event.kind == "continuity_authorized"), None
    )


def fact_fingerprint(
    store: Store, artifacts: NativeArtifactService, workspace_id: UUID, project_id: UUID
) -> str:
    service = NativeDependencyService(store, artifacts)
    values = []
    for fact in service.authority.facts(workspace_id, project_id):
        revision = (
            store.fact_revision(workspace_id, project_id, fact.current_revision_id)
            if fact.current_revision_id
            else None
        )
        values.append(
            {
                "fact": fact.model_dump(mode="json"),
                "revision_sha256": revision.sha256 if revision else None,
            }
        )
    return digest(values)


def unresolved_effects(store: Store, run: NativeExecutionRun) -> bool:
    return any(
        step.status in {"dispatched", "unknown"}
        or (
            step.usage_id
            and (usage := store.model_usage(run.workspace_id, run.project_id, step.usage_id))
            and usage.status in {"reserved", "dispatched", "unknown"}
        )
        for step in store.execution_steps(run.workspace_id, run.project_id, run.id)
    )


def assert_handoff_disclosable(
    store: Store, artifacts: NativeArtifactService, handoff: NativeContinuityHandoff
) -> None:
    for source_id in handoff.manifest.source_revision_ids:
        source = store.native_intake_source(handoff.workspace_id, handoff.project_id, source_id)
        if source is None or source.revoked_at is not None:
            raise AuthorizationError("Source access was revoked; handoff content is withheld.")
    for ref in handoff.manifest.artifacts:
        artifact = artifacts._artifact(handoff.workspace_id, handoff.project_id, ref.artifact_id)
        revision = artifacts._revision(artifact, ref.revision_id)
        if revision.sha256 != ref.sha256:
            raise InvalidTransitionError("Captured artifact evidence changed.")
        artifacts.assert_disclosable_revision(artifact, revision)
    for captured in handoff.manifest.snapshot.get("delivery_packets", []):
        packet = store.delivery_packet(
            handoff.workspace_id, handoff.project_id, UUID(captured["packet_id"])
        )
        if packet is None or packet.manifest_sha256 != captured["manifest_sha256"]:
            raise InvalidTransitionError("Captured delivery evidence changed.")
        NativeDeliveryService(store, artifacts)._disclosure(packet)
    dependencies = NativeDependencyService(store, artifacts)
    for captured in handoff.manifest.snapshot.get("facts", []):
        if revision := captured.get("revision"):
            fact_revision = store.fact_revision(
                handoff.workspace_id, handoff.project_id, UUID(revision["id"])
            )
            if fact_revision is None:
                raise InvalidTransitionError("Captured fact evidence is unavailable.")
            dependencies.authority.fact_sources(
                handoff.workspace_id, handoff.project_id, fact_revision
            )
    for captured in handoff.manifest.snapshot.get("dependency_assessments", []):
        assessment = store.impact_assessment(
            handoff.workspace_id, handoff.project_id, UUID(captured["assessment"]["id"])
        )
        if assessment is None:
            raise InvalidTransitionError("Captured dependency assessment is unavailable.")
        dependencies._assessment_disclosable(handoff.workspace_id, handoff.project_id, assessment)


def assert_continuity_run(
    store: Store,
    projects: NativeProjectService,
    artifacts: NativeArtifactService,
    run: NativeExecutionRun,
    runner: NativeExecutionRunner | None = None,
) -> None:
    binding = continuity_binding(store, run)
    if binding is None:
        return
    handoff = store.continuity_handoff(
        run.workspace_id, run.project_id, UUID(binding["handoff_id"])
    )
    if handoff is None or str(handoff.recipient_id) != binding["recipient_id"]:
        raise AuthorizationError("Continuity authority is no longer available.")
    membership = projects._membership(run.workspace_id, handoff.recipient_id)
    if membership is None:
        raise AuthorizationError("The continuity recipient no longer has access.")
    recipient = ActorContext(
        actor_id=handoff.recipient_id,
        workspace_id=run.workspace_id,
        scopes=ROLE_SCOPES[membership.role],
        channel=Channel.API,
    )
    projects._active(projects._project(recipient, run.project_id, write=True, owner=True))
    assert_handoff_disclosable(store, artifacts, handoff)
    assert_handoff_current(store, artifacts, handoff)
    if runner is not None and (
        str(runner.id) != binding["runner_id"] or runner.issued_by != handoff.recipient_id
    ):
        raise AuthorizationError("This checkpoint belongs to the authorized successor worker.")


def assert_handoff_current(
    store: Store, artifacts: NativeArtifactService, handoff: NativeContinuityHandoff
) -> None:
    project = store.native_project(handoff.workspace_id, handoff.project_id)
    if (
        project is None
        or project.version != handoff.manifest.project_version
        or project.status != "active"
    ):
        raise ContinuityContextChangedError(
            "Project context changed; capture a new handoff before continuing."
        )
    if (
        artifacts.source_authority(handoff.workspace_id, handoff.project_id)["fingerprint"]
        != handoff.manifest.snapshot["source_fingerprint"]
    ):
        raise ContinuityContextChangedError(
            "Source context changed; capture a new handoff before continuing."
        )
    if (
        fact_fingerprint(store, artifacts, handoff.workspace_id, handoff.project_id)
        != handoff.manifest.snapshot["fact_fingerprint"]
    ):
        raise ContinuityContextChangedError(
            "Fact context changed; capture a new handoff before continuing."
        )
    for captured in handoff.manifest.snapshot["artifacts"]:
        artifact = artifacts._artifact(
            handoff.workspace_id, handoff.project_id, UUID(captured["artifact"]["id"])
        )
        if str(artifact.current_revision_id) != captured["artifact"]["current_revision_id"]:
            raise ContinuityContextChangedError(
                "An output changed; capture a new handoff before continuing."
            )
        if captured["status"] == "accepted" and artifacts._status(artifact) != "accepted":
            raise ContinuityContextChangedError(
                "Captured accepted work requires current review authority."
            )
        if snapshot := captured.get("dependency_snapshot"):
            assert artifact.current_revision_id is not None
            try:
                artifacts.dependencies.assert_applicable(
                    handoff.workspace_id, handoff.project_id, artifact.current_revision_id, snapshot
                )
            except InvalidTransitionError as exc:
                raise ContinuityContextChangedError(str(exc)) from exc


class NativeContinuityService:
    def __init__(
        self,
        store: Store,
        execution: NativeExecutionService,
        delivery: NativeDeliveryService | None = None,
        storage: NativeStorageService | None = None,
    ) -> None:
        self.store, self.execution = store, execution
        self.projects, self.artifacts = execution.projects, execution.artifacts
        self.delivery = delivery or NativeDeliveryService(store, self.artifacts)
        self.storage = storage

    def _storage_entitlement(self, actor: ActorContext, project_id: UUID) -> dict[str, Any] | None:
        if self.storage is not None:
            return cast(dict[str, Any], self.storage.view(actor, project_id)["entitlement"])
        current = self.store.storage_entitlement(actor.workspace_id)
        if current is None:
            return None
        value = current.model_dump(mode="json")
        # Isolated fixtures without installation account authority fail closed
        # for administrator notes; production uses the established storage view.
        if self.projects._workspace(actor).role != "owner":
            value.update(reason="", issued_by=None)
        return value

    @staticmethod
    def _namespace(actor: ActorContext, project_id: UUID) -> str:
        return f"native_continuity:{actor.workspace_id}:{project_id}:{actor.actor_id}"

    def _recipient(self, workspace_id: UUID, project_id: UUID, actor_id: UUID) -> ActorContext:
        membership = self.projects._membership(workspace_id, actor_id)
        if (
            membership is None
            or self.store.native_project_member(workspace_id, project_id, actor_id) is None
        ):
            raise AuthorizationError("Choose an active, explicitly shared project recipient.")
        actor = ActorContext(
            actor_id=actor_id,
            workspace_id=workspace_id,
            scopes=ROLE_SCOPES[membership.role],
            channel=Channel.API,
        )
        self.projects._project(actor, project_id)
        return actor

    def _handoff(
        self, actor: ActorContext, project_id: UUID, handoff_id: UUID
    ) -> NativeContinuityHandoff:
        self.execution._access(actor, project_id)
        handoff = self.store.continuity_handoff(actor.workspace_id, project_id, handoff_id)
        if handoff is None or actor.actor_id not in {handoff.created_by, handoff.recipient_id}:
            raise NotFoundError("Project handoff not found.")
        self._recipient(actor.workspace_id, project_id, handoff.recipient_id)
        return NativeContinuityHandoff.model_validate(handoff.model_dump())

    def _can_manage(self, actor: ActorContext, project_id: UUID) -> bool:
        try:
            self.execution._access(actor, project_id, write=True)
        except AUTHORITY_ERRORS:
            return False
        return True

    def _snapshot(
        self, actor: ActorContext, project_id: UUID, title: str
    ) -> NativeContinuityManifest:
        project = self.projects.get_project(actor, project_id)
        sources = self.store.native_intake_sources(actor.workspace_id, project_id)
        if len(sources) > 2000:
            raise ValidationError("Continuity supports at most 2,000 source revision references.")
        artifacts = self.store.artifacts(actor.workspace_id, project_id, offset=0, limit=101)
        runs = self.store.execution_runs(actor.workspace_id, project_id, 0, 101)
        tasks = self.store.native_tasks(actor.workspace_id, project_id, 0, 201)
        usages = self.store.model_usage_entries(actor.workspace_id, project_id, 0, 501)
        if len(artifacts) > 100 or len(runs) > 100 or len(tasks) > 200 or len(usages) > 500:
            raise ValidationError(
                "The project exceeds the supported handoff inventory; "
                "no evidence was silently omitted."
            )
        dependency_service = NativeDependencyService(self.store, self.artifacts)
        facts = dependency_service.view(actor, project_id)["facts"]
        if any(value["disclosure"] != "available" for value in facts):
            raise AuthorizationError("Current fact evidence is withheld.")
        assessments = self.store.impact_assessments(actor.workspace_id, project_id, 0, 101)
        proposals = self.store.repair_proposals(actor.workspace_id, project_id, 0, 101)
        packets = self.store.delivery_packets(actor.workspace_id, project_id, 0, 21)
        if len(assessments) > 100 or len(proposals) > 100 or len(packets) > 20:
            raise ValidationError(
                "Dependency or delivery history exceeds this handoff's supported bound."
            )
        assessment_values = []
        for assessment in assessments:
            value = dependency_service.assessment_detail(actor, project_id, assessment.id)
            if value["disclosure"] != "available":
                raise AuthorizationError("Historical dependency evidence is withheld.")
            assessment_values.append(value)
        proposal_values = []
        for proposal in proposals:
            proposal_assessment = self.store.impact_assessment(
                actor.workspace_id, project_id, proposal.assessment_id
            )
            if proposal_assessment is None:
                raise InvalidTransitionError("Repair assessment is unavailable.")
            dependency_service._assessment_disclosable(
                actor.workspace_id, project_id, proposal_assessment
            )
            proposal_values.append(proposal.model_dump(mode="json"))
        packet_values = []
        for packet in packets:
            value = self.delivery.detail(actor, project_id, packet.id)
            packet_values.append(
                {
                    "packet_id": str(packet.id),
                    "manifest_sha256": packet.manifest_sha256,
                    "manifest": packet.manifest.model_dump(mode="json"),
                    "applicability": value["applicability"],
                    "acceptance": value["acceptance"],
                    "profile_assignments": [
                        item.model_dump(mode="json")
                        for item in self.store.delivery_profile_assignments(
                            actor.workspace_id, project_id, packet.id, 0, 101
                        )
                    ],
                    "validations": [
                        item.model_dump(mode="json")
                        for item in self.store.delivery_validations(
                            actor.workspace_id, project_id, packet.id, 0, 101
                        )
                    ],
                    "package_decisions": [
                        item.model_dump(mode="json")
                        for item in self.store.delivery_acceptances(
                            actor.workspace_id, project_id, packet.id, 0, 101
                        )
                    ],
                }
            )
        refs: list[ContinuityArtifactRef] = []
        outputs: list[dict[str, Any]] = []
        for artifact in artifacts:
            value = self.artifacts.detail(actor, project_id, artifact.id)
            for revision in self.store.artifact_revisions(
                actor.workspace_id, project_id, artifact.id, 0, 101
            ):
                self.artifacts.assert_disclosable_revision(artifact, revision)
                refs.append(
                    ContinuityArtifactRef(
                        artifact_id=artifact.id, revision_id=revision.id, sha256=revision.sha256
                    )
                )
            outputs.append(
                {
                    key: value[key]
                    for key in (
                        "artifact",
                        "status",
                        "revisions",
                        "checks",
                        "decisions",
                        "dependency_snapshot",
                    )
                }
            )
        if len(refs) > 200:
            raise ValidationError("Continuity supports at most 200 exact artifact revisions.")
        traces: list[dict[str, Any]] = []
        for run in runs:
            checkpoints, effects = [], []
            for step in self.store.execution_steps(actor.workspace_id, project_id, run.id):
                checkpoints.append(
                    step.model_dump(mode="json", exclude={"request", "result"})
                    | {"result_digest": digest(step.result)}
                )
                if step.kind == "reference":
                    receipt = self.execution.reference.lookup(
                        actor.workspace_id, project_id, step.operation_id
                    )
                    effects.append(
                        {
                            "operation_id": str(step.operation_id),
                            "status": step.status,
                            "receipt_digest": digest(receipt) if receipt else None,
                        }
                    )
            traces.append(
                {
                    key: public_run(run)[key]
                    for key in (
                        "id",
                        "task_id",
                        "status",
                        "version",
                        "step_number",
                        "applied_step",
                        "attempt",
                        "error_code",
                        "deadline_at",
                    )
                }
                | {
                    "checkpoints": checkpoints,
                    "effects": effects,
                    "waits": [
                        wait.model_dump(mode="json", exclude={"question", "response"})
                        for wait in self.store.execution_waits(
                            actor.workspace_id, project_id, run.id
                        )
                    ],
                }
            )
        usage_values = [
            usage.model_dump(
                mode="json",
                include={
                    "id",
                    "operation_id",
                    "phase",
                    "model_id",
                    "status",
                    "reserved_microusd",
                    "held_microusd",
                    "charged_microusd",
                    "error_code",
                    "started_at",
                    "deadline_at",
                    "finished_at",
                },
            )
            for usage in usages
        ]
        policy = self.store.model_resource_policy(actor.workspace_id, project_id)
        snapshot = {
            "project": project.model_dump(mode="json"),
            "source_fingerprint": self.artifacts.source_authority(actor.workspace_id, project_id)[
                "fingerprint"
            ],
            "dependency_assessments": assessment_values,
            "facts": facts,
            "fact_fingerprint": fact_fingerprint(
                self.store, self.artifacts, actor.workspace_id, project_id
            ),
            "repair_proposals": proposal_values,
            "delivery_packets": packet_values,
            "omitted_sources": [
                {
                    "source_revision_id": str(source.id),
                    "reason": "Previously revoked and not included in captured execution context.",
                }
                for source in sources
                if source.revoked_at and not runs
            ],
            "artifacts": outputs,
            "pending_tasks": [
                task.model_dump(mode="json")
                for task in tasks
                if task.status not in {"done", "cancelled"}
            ],
            "runs": traces,
            "costs": {
                "charged_microusd": sum(u.charged_microusd for u in usages),
                "held_microusd": sum(u.held_microusd for u in usages),
                "entries": usage_values,
            },
            "storage": {
                "usage": self.store.storage_usage(actor.workspace_id),
                "charges": [
                    charge.model_dump(mode="json")
                    for charge in self.store.storage_charges(actor.workspace_id, project_id)
                ],
            },
            "grants": {
                "execution_policy": public_policy(
                    self.execution._policy(actor.workspace_id, project_id)
                ),
                "runners": [
                    public_runner(runner)
                    for runner in self.store.execution_runners(actor.workspace_id, project_id)
                ],
                "model_resource_policy": policy.model_dump(mode="json") if policy else None,
            },
            "remaining_decisions": [
                {"artifact_id": value["artifact"]["id"], "status": value["status"]}
                for value in outputs
                if value["status"] != "accepted"
            ],
            "supported_resume_actions": [
                "continue an expired/queued native checkpoint",
                "retry definitively reconciled failed work",
                "inspect completed checkpoint without redispatch",
            ],
        }
        return NativeContinuityManifest(
            title=title,
            project_version=project.version,
            source_revision_ids=tuple(
                sorted(source.id for source in sources if runs or not source.revoked_at)
            ),
            artifacts=tuple(refs),
            runs=tuple(
                ContinuityRunRef(
                    run_id=run.id,
                    version=run.version,
                    step_number=run.step_number,
                    applied_step=run.applied_step,
                )
                for run in runs
            ),
            snapshot=safe_text(snapshot),
            limitations=LIMITATIONS,
        )

    def create(
        self, actor: ActorContext, project_id: UUID, command: CreateContinuityHandoff
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.execution._access(actor, project_id, write=True)
            self._recipient(actor.workspace_id, project_id, command.recipient_id)

            def capture() -> dict[str, Any]:
                if self.store.continuity_handoffs(actor.workspace_id, project_id, 999, 1):
                    raise ValidationError("This project's handoff history is full.")
                manifest = self._snapshot(actor, project_id, command.title)
                handoff = NativeContinuityHandoff(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    created_by=actor.actor_id,
                    recipient_id=command.recipient_id,
                    manifest=manifest,
                    manifest_sha256=hashlib.sha256(continuity_manifest_bytes(manifest)).hexdigest(),
                )
                assert_handoff_disclosable(self.store, self.artifacts, handoff)
                self.store.insert_continuity_handoff(handoff)
                # Admission promises a usable export. Its final archive (including
                # current status and ZIP framing) must fit before commit/receipt.
                self.download(actor, project_id, handoff.id)
                self.projects.audit.record(
                    event_type="native.continuity.captured",
                    actor=actor,
                    resource_type="continuity_handoff",
                    resource_id=str(handoff.id),
                    payload={
                        "project_id": str(project_id),
                        "recipient_id": str(command.recipient_id),
                        "manifest_sha256": handoff.manifest_sha256,
                    },
                )
                return {"kind": "create", "id": str(handoff.id)}

            receipt, _ = self.store.execute_once(
                self._namespace(actor, project_id),
                command.idempotency_key,
                digest(
                    {
                        "kind": "create",
                        "command": command.model_dump(mode="json", exclude={"idempotency_key"}),
                    }
                ),
                capture,
            )
            return self.detail(actor, project_id, UUID(receipt["id"]))

    def _actions(
        self, actor: ActorContext, handoff: NativeContinuityHandoff
    ) -> list[dict[str, Any]]:
        actions = []
        for ref in handoff.manifest.runs:
            run = self.execution._run(actor.workspace_id, handoff.project_id, ref.run_id)
            reason = ""
            action = "retry" if run.status in {"failed", "unknown"} else "continue"
            if (
                not self._can_manage(actor, handoff.project_id)
                or actor.actor_id != handoff.recipient_id
            ):
                reason = "Only the current recipient with project owner authority can resume."
            elif not self.execution.recovery.is_active():
                reason = "Installation recovery is quarantined; continuation is blocked."
            elif run.status == "running" and run.lease_until and run.lease_until > utc_now():
                reason = "The current worker lease is still active."
            elif run.status in {"stale", "cancelled", "waiting"}:
                reason = (
                    "Current work requires reauthorization or its existing reconciliation control."
                )
            elif run.status != "completed":
                try:
                    NativeWorkflowService._authority(self.execution, run)
                    assert_handoff_current(self.store, self.artifacts, handoff)
                except AUTHORITY_ERRORS as exc:
                    reason = str(exc)
            if unresolved_effects(self.store, run):
                reason = "Reconcile unknown provider effects before continuing."
            actions.append(
                {
                    "run_id": str(run.id),
                    "action": action,
                    "expected_run_version": run.version,
                    "eligible": not reason,
                    "reason": reason,
                }
            )
        return actions

    def detail(self, actor: ActorContext, project_id: UUID, handoff_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            handoff = self._handoff(actor, project_id, handoff_id)
            reasons: list[str] = []
            allowed = True
            try:
                assert_handoff_disclosable(self.store, self.artifacts, handoff)
            except AUTHORITY_ERRORS as exc:
                allowed = False
                reasons.append(str(exc))
            if allowed:
                try:
                    assert_handoff_current(self.store, self.artifacts, handoff)
                except AUTHORITY_ERRORS as exc:
                    reasons.append(str(exc))
            public = (
                handoff.model_dump(mode="json")
                if allowed
                else handoff.model_dump(mode="json", exclude={"manifest"})
            )
            actions = self._actions(actor, handoff) if allowed else []
            result = {
                "handoff": public,
                "disclosure_allowed": allowed,
                "current": allowed and not reasons,
                "reasons": reasons,
                "can_export": allowed,
                "can_resume": allowed and any(action["eligible"] for action in actions),
                "actions": actions,
                "runners": [
                    public_runner(runner)
                    for runner in self.store.execution_runners(actor.workspace_id, project_id)
                    if runner.issued_by == actor.actor_id
                ]
                if allowed
                else [],
                "current_resources": {
                    "costs": self.store.model_usage_totals(
                        actor.workspace_id, project_id, utc_now()
                    ).model_dump(mode="json"),
                    "storage_usage": self.store.storage_usage(actor.workspace_id),
                    "storage_entitlement": self._storage_entitlement(actor, project_id),
                    "recovery_active": self.execution.recovery.is_active(),
                }
                if allowed
                else None,
            }
            if allowed:
                lengths = {
                    "manifest.json": len(continuity_manifest_bytes(handoff.manifest)),
                    "current-status.json": len(
                        json.dumps(
                            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
                    ),
                }
                for ref in handoff.manifest.artifacts:
                    artifact = self.artifacts._artifact(
                        actor.workspace_id, project_id, ref.artifact_id
                    )
                    revision = self.artifacts._revision(artifact, ref.revision_id)
                    lengths[export_path(revision)] = revision.size_bytes
                # ZIP_STORED with generated entries has fixed local/central
                # headers; files are bounded far below ZIP64 thresholds.
                size = (
                    sum(lengths.values())
                    + sum(76 + 2 * len(path.encode("utf-8")) for path in lengths)
                    + 22
                )
                result["can_export"] = size <= MAX_CONTINUITY_EXPORT_BYTES
            return result

    def view(
        self, actor: ActorContext, project_id: UUID, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        self.projects._page(offset, limit)
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.execution._access(actor, project_id)
            rows = self.store.continuity_handoffs(actor.workspace_id, project_id, offset, limit)
            items = []
            for row in rows:
                if actor.actor_id not in {row.created_by, row.recipient_id}:
                    continue
                try:
                    value = self.detail(actor, project_id, row.id)
                except AUTHORITY_ERRORS:
                    continue
                metadata = row.model_dump(mode="json", exclude={"manifest"})
                items.append(
                    metadata
                    | {
                        "title": row.manifest.title
                        if value["disclosure_allowed"]
                        else "Withheld handoff",
                        **{key: value[key] for key in ("disclosure_allowed", "current", "reasons")},
                    }
                )
            access = self.projects.access(actor, project_id)
            return {
                "items": items,
                "has_more": bool(
                    self.store.continuity_handoffs(
                        actor.workspace_id, project_id, offset + limit, 1
                    )
                ),
                "can_manage": self._can_manage(actor, project_id),
                "recipients": [
                    person.model_dump(mode="json") for person in access.members if person.active
                ],
            }

    def operation_receipt(self, actor: ActorContext, project_id: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.execution._access(actor, project_id, write=True)
            receipt = self.store.command_receipt(self._namespace(actor, project_id), key)
            if receipt is None:
                raise NotFoundError("Handoff operation not found.")
            handoff = self._handoff(actor, project_id, UUID(receipt["id"]))
            assert_handoff_disclosable(self.store, self.artifacts, handoff)
            return receipt

    def download(
        self, actor: ActorContext, project_id: UUID, handoff_id: UUID
    ) -> tuple[NativeContinuityHandoff, bytes]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            handoff = self._handoff(actor, project_id, handoff_id)
            assert_handoff_disclosable(self.store, self.artifacts, handoff)
            files = {
                "manifest.json": continuity_manifest_bytes(handoff.manifest),
                "current-status.json": json.dumps(
                    self.detail(actor, project_id, handoff_id),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            }
            for ref in handoff.manifest.artifacts:
                revision, raw = self.artifacts.read_content(
                    actor, project_id, ref.artifact_id, ref.revision_id
                )
                files[export_path(revision)] = raw
            if sum(len(raw) for raw in files.values()) > MAX_CONTINUITY_EXPORT_BYTES:
                raise ValidationError("Handoff originals exceed the supported 32MiB export bound.")
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for path, raw in sorted(files.items()):
                    info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, raw)
            content = stream.getvalue()
            if len(content) > MAX_CONTINUITY_EXPORT_BYTES:
                raise ValidationError("Handoff archive exceeds its supported 32MiB byte size.")
            return handoff, content

    def verify(
        self,
        actor: ActorContext,
        project_id: UUID,
        handoff_id: UUID,
        command: VerifyContinuityBundle,
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            handoff, expected = self.download(actor, project_id, handoff_id)
            try:
                raw = base64.b64decode(command.content_base64, validate=True)
            except (ValueError, binascii.Error):
                raise ValidationError("Handoff bundle must use valid base64.") from None
            if len(raw) > MAX_CONTINUITY_EXPORT_BYTES:
                raise ValidationError("Handoff bundle exceeds the supported byte size.")
            try:
                with (
                    zipfile.ZipFile(io.BytesIO(raw)) as archive,
                    zipfile.ZipFile(io.BytesIO(expected)) as original,
                ):
                    expected_files = {name: original.read(name) for name in original.namelist()}
                    if len(archive.infolist()) != len(expected_files) or set(
                        archive.namelist()
                    ) != set(expected_files):
                        raise ValidationError(
                            "Handoff files changed or unsupported foreign content was supplied."
                        )
                    if (
                        sum(item.file_size for item in archive.infolist())
                        > MAX_CONTINUITY_EXPORT_BYTES
                    ):
                        raise ValidationError("Handoff expanded size exceeds its supported bound.")
                    if any(
                        item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or item.flag_bits & 1
                        for item in archive.infolist()
                    ):
                        raise ValidationError(
                            "Encrypted or unsupported handoff compression is blocked."
                        )
                    if any(
                        archive.read(name) != value
                        for name, value in expected_files.items()
                        if name != "current-status.json"
                    ):
                        raise ValidationError(
                            "Handoff manifest or exact editable files changed; import is blocked."
                        )
            except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError):
                raise ValidationError("Invalid or unsupported handoff archive.") from None
            return {
                "valid": True,
                "manifest_sha256": handoff.manifest_sha256,
                "runtime": "native",
                "foreign_runtime_import": "blocked",
                "current_status_trusted": False,
                "verification_scope": "immutable_manifest_and_originals",
            }

    def resume(
        self,
        actor: ActorContext,
        project_id: UUID,
        handoff_id: UUID,
        command: ResumeContinuityHandoff,
    ) -> dict[str, Any]:
        with (
            self.store.transaction(IDENTITY_LOCK),
            self.store.transaction(actor.workspace_id),
            self.execution.recovery.operation(),
        ):
            self.execution._access(actor, project_id, write=True)
            handoff = self._handoff(actor, project_id, handoff_id)
            if actor.actor_id != handoff.recipient_id:
                raise AuthorizationError(
                    "Only the named current recipient can authorize continuation."
                )
            assert_handoff_disclosable(self.store, self.artifacts, handoff)
            if handoff.manifest_sha256 != command.manifest_sha256:
                raise InvalidTransitionError(
                    "The handoff manifest changed; continuation is blocked."
                )
            if command.run_id not in {ref.run_id for ref in handoff.manifest.runs}:
                raise ValidationError("This run is not part of the exact handoff.")

            def authorize() -> dict[str, Any]:
                run = self.execution._run(actor.workspace_id, project_id, command.run_id)
                self.projects._version(run.version, command.expected_run_version)
                runner = self.store.execution_runner(
                    actor.workspace_id, project_id, command.runner_id
                )
                if runner is None or runner.issued_by != actor.actor_id:
                    raise AuthorizationError(
                        "Enroll a successor worker under the current recipient's authority."
                    )
                self.execution._check_runner(runner)
                if run.status == "completed":
                    return {
                        "kind": "resume",
                        "id": str(handoff.id),
                        "run_id": str(run.id),
                        "status": "already_completed",
                        "redispatched": False,
                    }
                assert_handoff_current(self.store, self.artifacts, handoff)
                if unresolved_effects(self.store, run):
                    raise InvalidTransitionError(
                        "Reconcile unknown provider effects before resuming."
                    )
                if run.status == "running" and run.lease_until and run.lease_until > utc_now():
                    raise InvalidTransitionError("The current worker lease is still active.")
                # Stage the replacement binding in this transaction before the
                # ordinary authority gate. Any failure rolls it and its receipt back.
                self.execution._event(
                    run,
                    "continuity_authorized",
                    handoff_id=str(handoff.id),
                    recipient_id=str(actor.actor_id),
                    runner_id=str(runner.id),
                )
                self.execution._authority(run)
                if command.action == "retry":
                    self.execution.retry(
                        actor,
                        project_id,
                        run.id,
                        ExecutionCommand(
                            expected_version=run.version,
                            idempotency_key="continuity-retry-" + digest(command.idempotency_key),
                        ),
                    )
                else:
                    if run.status == "running" and run.lease_until and run.lease_until > utc_now():
                        raise InvalidTransitionError("The current worker lease is still active.")
                    self.execution._recover(actor.workspace_id, project_id)
                    run = self.execution._run(actor.workspace_id, project_id, run.id)
                    if run.status != "queued":
                        raise InvalidTransitionError(
                            "This run requires its existing wait or reconciliation control."
                        )
                return {
                    "kind": "resume",
                    "id": str(handoff.id),
                    "run_id": str(run.id),
                    "status": "authorized",
                    "redispatched": False,
                }

            receipt, replayed = self.store.execute_once(
                self._namespace(actor, project_id),
                command.idempotency_key,
                digest(
                    {
                        "kind": "resume",
                        "handoff_id": str(handoff_id),
                        "command": command.model_dump(mode="json", exclude={"idempotency_key"}),
                    }
                ),
                authorize,
            )
            return receipt | {
                "handoff_id": str(handoff.id),
                "replayed": replayed,
                "run": public_run(
                    self.execution._run(actor.workspace_id, project_id, command.run_id)
                ),
                "next_action": "Use the enrolled native successor worker to claim and continue.",
            }
