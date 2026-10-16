"""Typed facts, declared impact and atomic, reviewable selective repair."""

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError as ModelValidationError

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, utc_now
from simon.domain.native_artifacts import NativeArtifactCheck
from simon.domain.native_dependencies import (
    DependencyBinding,
    DependencySelector,
    NativeDependencyDeclaration,
    NativeFact,
    NativeFactRevision,
    NativeImpactAssessment,
    NativeRepairProposal,
    semantic_hash,
    snapshot_hash,
)
from simon.domain.native_dependency_commands import (
    CommitNativeRepair,
    CreateImpactAssessment,
    DeclareNativeDependencies,
    PreviewNativeRepair,
    UpsertNativeFact,
)
from simon.domain.native_projects import NativeCommand
from simon.domain.ports import Store
from simon.services.dependency_graph import (
    MAX_FACTS,
    MAX_OUTPUTS,
    DependencyAuthority,
    artifact_text,
    artifact_value,
    contains,
    fact_value,
    overlaps,
    select,
    strict_json,
    tokens,
)
from simon.services.identity import IDENTITY_LOCK

if TYPE_CHECKING:
    from simon.services.native_artifacts import NativeArtifactService

__all__ = ["DependencyAuthority", "NativeDependencyService"]


class NativeDependencyService:
    def __init__(self, store: Store, artifacts: "NativeArtifactService") -> None:
        self.store, self.artifacts = store, artifacts
        self.authority = DependencyAuthority(store)

    def _disclose(self, ws: UUID, pid: UUID, revision_id: UUID) -> None:
        revision = self.store.artifact_revision(ws, pid, revision_id)
        if revision is None:
            raise NotFoundError("Output revision not found.")
        artifact = self.artifacts._artifact(ws, pid, revision.artifact_id)
        self.artifacts.assert_disclosable_revision(artifact, revision)

    @staticmethod
    def _namespace(actor: ActorContext, pid: UUID) -> str:
        return f"native_dependencies:{actor.workspace_id}:{pid}:{actor.actor_id}"

    @staticmethod
    def _commit_namespace(ws: UUID, pid: UUID) -> str:
        return f"native_dependency_commit:{ws}:{pid}"

    def _once(
        self,
        actor: ActorContext,
        pid: UUID,
        command: NativeCommand,
        kind: str,
        target: UUID,
        operation: Callable[[], UUID],
    ) -> UUID:
        result, _ = self.store.execute_once(
            self._namespace(actor, pid),
            command.idempotency_key,
            snapshot_hash(
                {
                    "kind": kind,
                    "target": str(target),
                    "command": command.model_dump(mode="json", exclude={"idempotency_key"}),
                }
            ),
            lambda: {"kind": kind, "id": str(operation())},
        )
        return UUID(result["id"])

    def _audit(self, actor: ActorContext, pid: UUID, event: str, identifier: UUID) -> None:
        self.artifacts._audit(actor, pid, "dependency_" + event, dependency_id=str(identifier))

    def operation_receipt(self, actor: ActorContext, pid: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid, write=True)
            result = self.store.command_receipt(self._namespace(actor, pid), key)
            if result is None:
                raise NotFoundError("Dependency operation not found.")
            return result

    def _fact(self, ws: UUID, pid: UUID, identifier: UUID) -> NativeFact:
        fact = self.store.fact(ws, pid, identifier)
        if fact is None:
            raise NotFoundError("Fact not found.")
        return fact

    def _fact_revision(self, ws: UUID, pid: UUID, identifier: UUID) -> NativeFactRevision:
        revision = self.store.fact_revision(ws, pid, identifier)
        if revision is None:
            raise NotFoundError("Fact revision not found.")
        return revision

    def _assessment(self, ws: UUID, pid: UUID, identifier: UUID) -> NativeImpactAssessment:
        value = self.store.impact_assessment(ws, pid, identifier)
        if value is None:
            raise NotFoundError("Impact assessment not found.")
        return value

    def _proposal(self, ws: UUID, pid: UUID, identifier: UUID) -> NativeRepairProposal:
        value = self.store.repair_proposal(ws, pid, identifier)
        if value is None:
            raise NotFoundError("Repair proposal not found.")
        return value

    def _can_manage(self, actor: ActorContext, pid: UUID) -> bool:
        try:
            self.artifacts._access(actor, pid, write=True)
            return True
        except (AuthorizationError, InvalidTransitionError):
            return False

    def view(self, actor: ActorContext, pid: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid)
            ws = actor.workspace_id
            facts = []
            for fact in self.authority.facts(ws, pid):
                revision = (
                    self._fact_revision(ws, pid, fact.current_revision_id)
                    if fact.current_revision_id
                    else None
                )
                disclosed = True
                if revision:
                    try:
                        self.authority.fact_sources(ws, pid, revision)
                    except AuthorizationError:
                        disclosed = False
                facts.append(
                    {
                        "fact": fact.model_dump(mode="json"),
                        "revision": revision.model_dump(mode="json")
                        if revision and disclosed
                        else None,
                        "disclosure": "available" if disclosed else "withheld",
                    }
                )
            outputs = []
            for artifact in self.authority.outputs(ws, pid):
                if artifact.current_revision_id is None:
                    continue
                output_revision = self.artifacts._revision(artifact)
                declaration = self.store.dependency_declaration(ws, pid, output_revision.id)
                disclosed = True
                try:
                    self._disclose(ws, pid, output_revision.id)
                except AuthorizationError:
                    disclosed = False
                outputs.append(
                    {
                        "artifact": artifact.model_dump(mode="json"),
                        "revision": output_revision.model_dump(
                            mode="json", exclude={"content_base64"}
                        ),
                        "declaration": declaration.model_dump(mode="json")
                        if declaration and disclosed
                        else None,
                        "disclosure": "available" if disclosed else "withheld",
                    }
                )
            return {
                "facts": facts,
                "outputs": outputs,
                "assessments": [
                    item.model_dump(mode="json", include={"id", "fact_id", "created_at"})
                    for item in self.store.impact_assessments(ws, pid, limit=100)
                ],
                "repairs": [
                    item.model_dump(mode="json", include={"id", "assessment_id", "created_at"})
                    for item in self.store.repair_proposals(ws, pid, limit=100)
                ],
                "can_manage": self._can_manage(actor, pid),
                "limits": {
                    "facts": MAX_FACTS,
                    "outputs": MAX_OUTPUTS,
                    "fact_revisions": 2000,
                    "dependencies_per_output": 32,
                    "repair_outputs": 10,
                    "proposal_bytes": 262144,
                },
            }

    def fact_detail(self, actor: ActorContext, pid: UUID, identifier: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid)
            fact = self._fact(actor.workspace_id, pid, identifier)
            revisions = self.store.fact_revisions(actor.workspace_id, pid, identifier, limit=2001)
            if len(revisions) > 2000:
                raise ValidationError("Fact history exceeds its bounded capacity.")
            try:
                for revision in revisions:
                    self.authority.fact_sources(actor.workspace_id, pid, revision)
            except AuthorizationError:
                return {
                    "fact": fact.model_dump(mode="json"),
                    "revisions": [],
                    "disclosure": "withheld",
                }
            return {
                "fact": fact.model_dump(mode="json"),
                "revisions": [revision.model_dump(mode="json") for revision in revisions],
                "disclosure": "available",
            }

    def save_fact(
        self, actor: ActorContext, pid: UUID, command: UpsertNativeFact
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid, write=True)
            ws = actor.workspace_id

            def save() -> UUID:
                facts = self.authority.facts(ws, pid)
                count = sum(
                    len(self.store.fact_revisions(ws, pid, fact.id, limit=2001)) for fact in facts
                )
                if count >= 2000:
                    raise ValidationError("Project fact revision capacity has been reached.")
                fact = self._fact(ws, pid, command.fact_id) if command.fact_id else None
                if fact is None:
                    if command.expected_version != 0 or len(facts) >= MAX_FACTS:
                        raise InvalidTransitionError(
                            "New fact requires version zero and available capacity."
                        )
                    if any(item.key == command.key for item in facts):
                        raise ValidationError("Fact key already exists.")
                    fact = NativeFact(
                        workspace_id=ws,
                        project_id=pid,
                        key=command.key,
                        kind=command.kind,
                        created_by=actor.actor_id,
                    )
                else:
                    self.artifacts.projects._version(fact.version, command.expected_version)
                    if (fact.key, fact.kind) != (command.key, command.kind):
                        raise ValidationError("A fact's key and kind are stable identities.")
                previous = (
                    self._fact_revision(ws, pid, fact.current_revision_id)
                    if fact.current_revision_id
                    else None
                )
                value = {
                    "value_type": command.value_type,
                    "value": command.value,
                    "unit": command.unit,
                    "sources": [binding.model_dump(mode="json") for binding in command.sources],
                }
                try:
                    revision = NativeFactRevision(
                        workspace_id=ws,
                        project_id=pid,
                        fact_id=fact.id,
                        version=previous.version + 1 if previous else 1,
                        parent_revision_id=previous.id if previous else None,
                        created_by=actor.actor_id,
                        semantic_sha256=semantic_hash(
                            command.value_type, command.value, command.unit
                        ),
                        sha256=snapshot_hash(value),
                        **value,
                    )
                except (ValueError, TypeError, RecursionError) as exc:
                    raise ValidationError(
                        "Fact must have a valid bounded value matching its type."
                    ) from exc
                self.authority.fact_sources(ws, pid, revision)
                for binding in revision.sources:
                    source = self.store.native_intake_source(ws, pid, binding.source_revision_id)
                    self.authority.validate_source_value(source, binding.selector, revision)
                if previous is None:
                    self.store.insert_fact(fact)
                self.store.insert_fact_revision(revision)
                updated = fact.model_copy(
                    update={
                        "version": fact.version + 1,
                        "current_revision_id": revision.id,
                        "updated_at": utc_now(),
                    }
                )
                self.store.update_fact(updated, fact.version)
                self._audit(actor, pid, "fact_saved", fact.id)
                return fact.id

            identifier = self._once(actor, pid, command, "fact", command.fact_id or pid, save)
            return self.fact_detail(actor, pid, identifier)

    def _validate_edges(
        self, ws: UUID, pid: UUID, revision: Any, edges: tuple[DependencyBinding, ...]
    ) -> None:
        for edge in edges:
            artifact_value(revision, edge.output_selector)
            if edge.input_kind == "fact":
                source = self._fact_revision(ws, pid, edge.revision_id)
                if source.fact_id != edge.input_id or source.sha256 != edge.sha256:
                    raise ValidationError(
                        "Fact dependency must identify its exact scoped revision."
                    )
                self.authority.fact_sources(ws, pid, source)
                fact_value(source, edge.input_selector)
            else:
                input_artifact = self.artifacts._artifact(ws, pid, edge.input_id)
                input_revision = self.artifacts._revision(input_artifact, edge.revision_id)
                if input_revision.sha256 != edge.sha256:
                    raise ValidationError("Output dependency must identify its exact content hash.")
                self._disclose(ws, pid, input_revision.id)
                artifact_value(input_revision, edge.input_selector)

    def declare(
        self, actor: ActorContext, pid: UUID, artifact_id: UUID, command: DeclareNativeDependencies
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid, write=True)
            ws = actor.workspace_id

            def save() -> UUID:
                artifact = self.artifacts._artifact(ws, pid, artifact_id)
                self.artifacts.projects._version(
                    artifact.version, command.expected_artifact_version
                )
                revision = self.artifacts._revision(artifact)
                if (revision.id, revision.sha256) != (command.revision_id, command.sha256):
                    raise InvalidTransitionError(
                        "Declare dependencies for the exact current output."
                    )
                self._disclose(ws, pid, revision.id)
                old = self.store.dependency_declaration(ws, pid, revision.id)
                version = old.version if old else 0
                self.artifacts.projects._version(version, command.expected_version)
                if version >= 100:
                    raise ValidationError("Dependency declaration history capacity reached.")
                self._validate_edges(ws, pid, revision, command.dependencies)
                declaration = NativeDependencyDeclaration(
                    workspace_id=ws,
                    project_id=pid,
                    artifact_id=artifact_id,
                    revision_id=revision.id,
                    sha256=revision.sha256,
                    version=version + 1,
                    coverage=command.coverage,
                    dependencies=command.dependencies,
                    notes=command.notes,
                    created_by=actor.actor_id,
                )
                self.store.insert_dependency_declaration(declaration, version)
                self._audit(actor, pid, "declared", declaration.id)
                return artifact_id

            self._once(actor, pid, command, "declaration", artifact_id, save)
            return self.view(actor, pid)

    def _graph_fingerprint(self, ws: UUID, pid: UUID) -> str:
        outputs = []
        for artifact in self.authority.outputs(ws, pid):
            declaration = (
                self.store.dependency_declaration(ws, pid, artifact.current_revision_id)
                if artifact.current_revision_id
                else None
            )
            task_ids = {artifact.task_id}
            if artifact.pending_repair_task_id:
                task_ids.add(artifact.pending_repair_task_id)
            if artifact.current_revision_id:
                task_ids.add(self.artifacts._revision(artifact).task_id)
            tasks = [self.store.native_task(ws, pid, task_id) for task_id in sorted(task_ids)]
            outputs.append(
                {
                    "artifact": artifact.model_dump(mode="json"),
                    "declaration": declaration.model_dump(mode="json") if declaration else None,
                    "tasks": [task.model_dump(mode="json") if task else None for task in tasks],
                }
            )
        project = self.store.native_project(ws, pid)
        return snapshot_hash(
            {
                "outputs": outputs,
                "facts": [fact.model_dump(mode="json") for fact in self.authority.facts(ws, pid)],
                "project": project.model_dump(mode="json") if project else None,
                "policy": self.artifacts._policy(ws, pid).model_dump(mode="json"),
            }
        )

    def _assessment_disclosable(self, ws: UUID, pid: UUID, value: NativeImpactAssessment) -> None:
        for rid in (value.from_revision_id, value.to_revision_id):
            self.authority.fact_sources(ws, pid, self._fact_revision(ws, pid, rid))
        for output in value.outputs:
            self._disclose(ws, pid, UUID(output["revision_id"]))

    def _assessment_current(self, ws: UUID, pid: UUID, value: NativeImpactAssessment) -> bool:
        return value.graph_fingerprint == self._graph_fingerprint(
            ws, pid
        ) and value.source_fingerprint == self.authority.source_fingerprint(ws, pid)

    def assessment_detail(self, actor: ActorContext, pid: UUID, identifier: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid)
            value = self._assessment(actor.workspace_id, pid, identifier)
            try:
                self._assessment_disclosable(actor.workspace_id, pid, value)
            except AuthorizationError:
                return {
                    "assessment": {"id": str(value.id)},
                    "disclosure": "withheld",
                    "current": False,
                }
            return {
                "assessment": value.model_dump(mode="json"),
                "disclosure": "available",
                "current": self._assessment_current(actor.workspace_id, pid, value),
            }

    def assess(
        self, actor: ActorContext, pid: UUID, command: CreateImpactAssessment
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid, write=True)
            ws = actor.workspace_id

            def create() -> UUID:
                if len(self.store.impact_assessments(ws, pid, limit=100)) >= 100:
                    raise ValidationError("Project impact assessment capacity reached.")
                fact = self._fact(ws, pid, command.fact_id)
                before = self._fact_revision(ws, pid, command.from_revision_id)
                after = self._fact_revision(ws, pid, command.to_revision_id)
                if (
                    before.fact_id != fact.id
                    or after.fact_id != fact.id
                    or fact.current_revision_id != after.id
                ):
                    raise InvalidTransitionError(
                        "Assess revisions of one fact against its current head."
                    )
                self.authority.fact_sources(ws, pid, before)
                self.authority.fact_sources(ws, pid, after)
                outputs: dict[UUID, dict[str, Any]] = {}
                declarations = {}
                affected: dict[UUID, dict[str, DependencySelector]] = {}
                pending: list[tuple[UUID, DependencySelector]] = []
                for artifact in self.authority.outputs(ws, pid):
                    if artifact.current_revision_id is None:
                        continue
                    revision = self.artifacts._revision(artifact)
                    self._disclose(ws, pid, revision.id)
                    declaration = self.store.dependency_declaration(ws, pid, revision.id)
                    declarations[artifact.id] = declaration
                    coverage = declaration.coverage if declaration else "unknown"
                    outputs[artifact.id] = {
                        "artifact_id": str(artifact.id),
                        "revision_id": str(revision.id),
                        "sha256": revision.sha256,
                        "artifact_version": artifact.version,
                        "declaration_id": str(declaration.id) if declaration else None,
                        "declaration_version": declaration.version if declaration else 0,
                        "coverage": coverage,
                        "status": "unaffected" if coverage == "complete" else "uncertain",
                        "reasons": []
                        if coverage == "complete"
                        else ["Missing or incomplete dependency coverage."],
                        "selectors": [],
                        "filename": revision.filename,
                    }
                    if declaration:
                        for edge in declaration.dependencies:
                            if edge.input_kind != "fact" or edge.input_id != fact.id:
                                continue
                            try:
                                pinned = self._fact_revision(ws, pid, edge.revision_id)
                                changed = fact_value(pinned, edge.input_selector) != fact_value(
                                    after, edge.input_selector
                                )
                            except ValidationError:
                                changed = True
                            if changed:
                                pending.append((artifact.id, edge.output_selector))
                while pending:
                    aid, region = pending.pop()
                    selected = affected.setdefault(aid, {})
                    key = snapshot_hash(region)
                    if key in selected:
                        continue
                    selected[key] = region
                    for dependent, declaration in declarations.items():
                        if declaration:
                            pending.extend(
                                (dependent, edge.output_selector)
                                for edge in declaration.dependencies
                                if (
                                    edge.input_kind == "artifact"
                                    and edge.input_id == aid
                                    and overlaps(edge.input_selector, region)
                                )
                            )
                uncertain = {
                    aid for aid, output in outputs.items() if output["coverage"] != "complete"
                }
                unresolved = list(uncertain)
                while unresolved:
                    upstream = unresolved.pop()
                    for aid, declaration in declarations.items():
                        if aid in uncertain or declaration is None:
                            continue
                        if any(
                            edge.input_kind == "artifact" and edge.input_id == upstream
                            for edge in declaration.dependencies
                        ):
                            uncertain.add(aid)
                            unresolved.append(aid)
                            outputs[aid]["status"] = "uncertain"
                            outputs[aid]["reasons"].append(
                                "An upstream output has incomplete dependency coverage."
                            )
                for aid, regions in affected.items():
                    outputs[aid]["status"] = "affected"
                    outputs[aid]["selectors"] = [
                        region.model_dump(mode="json") for region in regions.values()
                    ]
                    outputs[aid]["reasons"].append("Declared direct or transitive input changed.")
                assessment = NativeImpactAssessment(
                    workspace_id=ws,
                    project_id=pid,
                    fact_id=fact.id,
                    from_revision_id=before.id,
                    to_revision_id=after.id,
                    graph_fingerprint=self._graph_fingerprint(ws, pid),
                    source_fingerprint=self.authority.source_fingerprint(ws, pid),
                    outputs=tuple(outputs.values()),
                    created_by=actor.actor_id,
                )
                self.store.insert_impact_assessment(assessment)
                self._audit(actor, pid, "assessed", assessment.id)
                return assessment.id

            identifier = self._once(actor, pid, command, "assessment", command.fact_id, create)
            return self.assessment_detail(actor, pid, identifier)

    @staticmethod
    def _apply(text: str, replacements: Any, allowed: list[DependencySelector]) -> str:
        selectors = [item.selector for item in replacements]
        for index, selector in enumerate(selectors):
            if not any(contains(region, selector) for region in allowed):
                raise ValidationError("Repair replacement exceeds the declared affected region.")
            if any(overlaps(selector, other) for other in selectors[:index]):
                raise ValidationError("Repair replacements must identify disjoint regions.")
        if selectors[0].kind == "whole_file":
            replacement = replacements[0]
            if replacement.before != text or not isinstance(replacement.after, str):
                raise InvalidTransitionError(
                    "Whole-file repair requires exact before and after text."
                )
            return replacement.after
        value = strict_json(text)
        for replacement in replacements:
            selector = replacement.selector
            if semantic_hash("json", select(value, selector)) != semantic_hash(
                "json", replacement.before
            ):
                raise InvalidTransitionError("JSON repair before-value no longer matches.")
            path = tokens(selector)
            if not path:
                value = replacement.after
            else:
                parent = select(
                    value,
                    DependencySelector(kind="json_pointer", path=selector.path.rsplit("/", 1)[0]),
                )
                if isinstance(parent, list):
                    parent[int(path[-1])] = replacement.after
                else:
                    parent[path[-1]] = replacement.after
        try:
            return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValidationError("Repair must produce finite JSON data.") from exc

    def preview_repair(
        self, actor: ActorContext, pid: UUID, command: PreviewNativeRepair
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid, write=True)
            ws = actor.workspace_id

            def preview() -> UUID:
                if len(self.store.repair_proposals(ws, pid, limit=100)) >= 100:
                    raise ValidationError("Project repair proposal capacity reached.")
                assessment = self._assessment(ws, pid, command.assessment_id)
                self._assessment_disclosable(ws, pid, assessment)
                if not self._assessment_current(ws, pid, assessment):
                    raise InvalidTransitionError(
                        "Impact assessment changed; assess current heads again."
                    )
                indexed = {UUID(output["artifact_id"]): output for output in assessment.outputs}
                results = []
                for edit in command.edits:
                    impact = indexed.get(edit.artifact_id)
                    if impact is None or impact["status"] != "affected":
                        raise ValidationError(
                            "Select a declared affected output; resolve unknown coverage first."
                        )
                    artifact = self.artifacts._artifact(ws, pid, edit.artifact_id)
                    self.artifacts.projects._version(artifact.version, edit.expected_version)
                    revision = self.artifacts._revision(artifact)
                    if (revision.id, revision.sha256) != (edit.revision_id, edit.sha256):
                        raise InvalidTransitionError(
                            "Repair requires the current exact output revision."
                        )
                    before = artifact_text(revision)
                    after = self._apply(
                        before,
                        edit.replacements,
                        [DependencySelector.model_validate(item) for item in impact["selectors"]],
                    )
                    if before == after:
                        raise ValidationError("Each repaired output must contain an actual change.")
                    task = self.store.native_task(
                        ws, pid, artifact.pending_repair_task_id or artifact.task_id
                    )
                    if task is None or task.status == "cancelled":
                        raise InvalidTransitionError("Repair task is unavailable or cancelled.")
                    self.artifacts.assert_unshared_tasks(artifact, task)
                    results.append(
                        {
                            "artifact_id": str(artifact.id),
                            "revision_id": str(revision.id),
                            "artifact_version": artifact.version,
                            "task_id": str(task.id),
                            "task_version": task.version,
                            "declaration_version": impact["declaration_version"],
                            "filename": revision.filename,
                            "media_type": revision.media_type,
                            "before_text": before,
                            "after_text": after,
                            "before_sha256": revision.sha256,
                            "after_sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(),
                            "selectors": [
                                item.selector.model_dump(mode="json") for item in edit.replacements
                            ],
                        }
                    )
                edited = {str(edit.artifact_id) for edit in command.edits}
                plan = {
                    "assessment_id": str(assessment.id),
                    "graph_fingerprint": assessment.graph_fingerprint,
                    "source_fingerprint": assessment.source_fingerprint,
                    "rationale": command.rationale,
                    "edits": [edit.model_dump(mode="json") for edit in command.edits],
                    "outputs": results,
                    "remaining_stale": [
                        output["artifact_id"]
                        for output in assessment.outputs
                        if output["status"] != "unaffected" and output["artifact_id"] not in edited
                    ],
                }
                try:
                    proposal = NativeRepairProposal(
                        workspace_id=ws,
                        project_id=pid,
                        assessment_id=assessment.id,
                        sha256=snapshot_hash(plan),
                        plan=plan,
                        created_by=actor.actor_id,
                    )
                except ModelValidationError as exc:
                    raise ValidationError(
                        "Repair preview exceeds the 256 KiB plan capacity."
                    ) from exc
                self.store.insert_repair_proposal(proposal)
                self._audit(actor, pid, "repair_previewed", proposal.id)
                return proposal.id

            identifier = self._once(actor, pid, command, "repair", command.assessment_id, preview)
            return self.repair_detail(actor, pid, identifier)

    def repair_detail(self, actor: ActorContext, pid: UUID, identifier: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self.artifacts._access(actor, pid)
            ws = actor.workspace_id
            proposal = self._proposal(ws, pid, identifier)
            assessment = self._assessment(ws, pid, proposal.assessment_id)
            receipt = self.store.command_receipt(self._commit_namespace(ws, pid), str(proposal.id))
            try:
                self._assessment_disclosable(ws, pid, assessment)
                if receipt:
                    for rid in receipt["revision_ids"]:
                        self._disclose(ws, pid, UUID(rid))
            except AuthorizationError:
                return {
                    "proposal": {"id": str(proposal.id)},
                    "disclosure": "withheld",
                    "current": False,
                    "can_commit": False,
                    "reasons": ["Dependency evidence is no longer available."],
                    "committed_revision_ids": [],
                }
            current = self._assessment_current(ws, pid, assessment)
            return {
                "proposal": proposal.model_dump(mode="json"),
                "disclosure": "available",
                "current": current,
                "can_commit": current and receipt is None and self._can_manage(actor, pid),
                "reasons": []
                if current or receipt
                else ["Project authority changed; create a fresh assessment and repair preview."],
                "committed_revision_ids": receipt["revision_ids"] if receipt else [],
            }

    def commit_repair(
        self, actor: ActorContext, pid: UUID, identifier: UUID, command: CommitNativeRepair
    ) -> dict[str, Any]:
        with (
            self.store.transaction(IDENTITY_LOCK),
            self.store.transaction(actor.workspace_id),
            self.artifacts.usage.recovery.operation(),
        ):
            self.artifacts._access(actor, pid, write=True)
            ws = actor.workspace_id
            proposal = self._proposal(ws, pid, identifier)
            if (
                command.proposal_sha256 != proposal.sha256
                or snapshot_hash(proposal.plan) != proposal.sha256
            ):
                raise InvalidTransitionError("Commit must identify the exact reviewed repair plan.")
            assessment = self._assessment(ws, pid, proposal.assessment_id)
            self._assessment_disclosable(ws, pid, assessment)

            def commit() -> UUID:
                def publish() -> dict[str, Any]:
                    if not self._assessment_current(ws, pid, assessment):
                        raise InvalidTransitionError(
                            "Project authority changed; repair commit did not run."
                        )
                    previous = {}
                    published = {}
                    tasks = {}
                    for output in proposal.plan["outputs"]:
                        aid = UUID(output["artifact_id"])
                        artifact = self.artifacts._artifact(ws, pid, aid)
                        revision = self.artifacts._revision(artifact)
                        if (artifact.version, str(revision.id), revision.sha256) != (
                            output["artifact_version"],
                            output["revision_id"],
                            output["before_sha256"],
                        ):
                            raise InvalidTransitionError(
                                "A concurrent output edit prevented the entire repair."
                            )
                        task = self.store.native_task(ws, pid, UUID(output["task_id"]))
                        if (
                            task is None
                            or task.version != output["task_version"]
                            or task.status == "cancelled"
                        ):
                            raise InvalidTransitionError("Repair task authority changed.")
                        previous[aid] = self.store.dependency_declaration(ws, pid, revision.id)
                        tasks[task.id] = task
                    for task in tasks.values():
                        self.artifacts._task_status(task, "in_review")
                    for output in proposal.plan["outputs"]:
                        aid = UUID(output["artifact_id"])
                        artifact = self.artifacts._artifact(ws, pid, aid)
                        raw = output["after_text"].encode("utf-8")
                        if hashlib.sha256(raw).hexdigest() != output["after_sha256"]:
                            raise InvalidTransitionError(
                                "Repair content does not match its immutable preview."
                            )
                        published[aid] = self.artifacts._publish(
                            artifact,
                            tasks[UUID(output["task_id"])],
                            raw,
                            output["filename"],
                            output["media_type"],
                            actor_id=actor.actor_id,
                        )
                        repaired = self.artifacts._revision(published[aid])
                        self.store.insert_artifact_check(
                            NativeArtifactCheck(
                                workspace_id=ws,
                                project_id=pid,
                                revision_id=repaired.id,
                                sha256=repaired.sha256,
                                check_name="declared_repair_scope",
                                status="passed",
                                details={
                                    "assessment_id": str(assessment.id),
                                    "proposal_id": str(proposal.id),
                                    "proposal_sha256": proposal.sha256,
                                    "before_sha256": output["before_sha256"],
                                    "selectors": output["selectors"],
                                    "meaning": (
                                        "Exact preview and declared edit boundaries verified; "
                                        "semantic correctness requires review."
                                    ),
                                },
                            )
                        )
                    for aid, artifact in published.items():
                        old = previous[aid]
                        if old is None:
                            raise InvalidTransitionError(
                                "Repair dependency declaration is missing."
                            )
                        edges = []
                        regions = [
                            DependencySelector.model_validate(region)
                            for output in proposal.plan["outputs"]
                            if output["artifact_id"] == str(aid)
                            for region in output["selectors"]
                        ]
                        for edge in old.dependencies:
                            if edge.input_kind == "fact":
                                head = self._fact(ws, pid, edge.input_id)
                                assert head.current_revision_id is not None
                                source = self._fact_revision(ws, pid, head.current_revision_id)
                                pinned = self._fact_revision(ws, pid, edge.revision_id)
                                unchanged = fact_value(source, edge.input_selector) == fact_value(
                                    pinned, edge.input_selector
                                )
                                new_id, new_sha = source.id, source.sha256
                            else:
                                input_revision = self.artifacts._revision(
                                    self.artifacts._artifact(ws, pid, edge.input_id)
                                )
                                pinned_output = self.store.artifact_revision(
                                    ws, pid, edge.revision_id
                                )
                                assert pinned_output is not None
                                unchanged = semantic_hash(
                                    "json", artifact_value(input_revision, edge.input_selector)
                                ) == semantic_hash(
                                    "json", artifact_value(pinned_output, edge.input_selector)
                                )
                                new_id, new_sha = input_revision.id, input_revision.sha256
                            if not unchanged and not any(
                                contains(region, edge.output_selector) for region in regions
                            ):
                                edges.append(edge)
                                continue
                            edges.append(
                                edge.model_copy(update={"revision_id": new_id, "sha256": new_sha})
                            )
                        revision = self.artifacts._revision(artifact)
                        self._validate_edges(ws, pid, revision, tuple(edges))
                        declaration = NativeDependencyDeclaration(
                            workspace_id=ws,
                            project_id=pid,
                            artifact_id=aid,
                            revision_id=revision.id,
                            sha256=revision.sha256,
                            version=1,
                            coverage=old.coverage,
                            dependencies=tuple(edges),
                            notes=old.notes,
                            created_by=actor.actor_id,
                        )
                        self.store.insert_dependency_declaration(declaration, 0)
                    self._audit(actor, pid, "repair_committed", proposal.id)
                    return {
                        "kind": "commit",
                        "id": str(proposal.id),
                        "revision_ids": [
                            str(artifact.current_revision_id) for artifact in published.values()
                        ],
                    }

                self.store.execute_once(
                    self._commit_namespace(ws, pid), str(proposal.id), proposal.sha256, publish
                )
                return proposal.id

            self._once(actor, pid, command, "commit", identifier, commit)
            return self.repair_detail(actor, pid, identifier)
