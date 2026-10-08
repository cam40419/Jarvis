"""Resumable evidence intake and bounded planning through native project authority."""

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from pydantic import ValidationError as ModelValidationError

from simon.adapters.model_endpoints import ModelEndpointError
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.model_routing import ModelEndpoint, TextGenerationResult
from simon.domain.models import ActorContext, utc_now
from simon.domain.native_agents import CreateNativeAgent, NativeActor, NativeAgent
from simon.domain.native_intake import (
    AnalyzeIntake,
    IntakeRun,
    IntakeSource,
    IntakeTask,
    NativeIntake,
    SourceUpload,
    StaffingProposal,
    UpdateNativeIntake,
)
from simon.domain.native_projects import (
    CreateNativeTask,
    NativeProject,
    NativeTask,
    TaskAssignment,
    VersionedNativeCommand,
)
from simon.domain.ports import Store
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK
from simon.services.intake_planner import IntakePlanner, IntakePlanningError, PreparedIntake
from simon.services.intake_sources import (
    MAX_SOURCE_BYTES,
    IntakeSourceBytes,
    extract_source,
    redact,
)
from simon.services.native_projects import NativeProjectService
from simon.services.native_teams import NativeTeamService


def configured_planner(
    path: Path | None, workspace_id: UUID, *, environ: Mapping[str, str] | None = None
) -> IntakePlanner:
    """Only an administrator file can bind endpoints/credentials to a workspace."""
    if path is None:
        return IntakePlanner(())
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("oversized catalog")
        rows = json.loads(path.read_bytes())
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("invalid catalog")
        endpoints = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"workspace_ids", "endpoint"}:
                raise ValueError("invalid entry")
            workspaces = TypeAdapter(list[UUID]).validate_python(row["workspace_ids"])
            endpoint = ModelEndpoint.model_validate(row["endpoint"])
            if workspace_id in workspaces:
                endpoints.append(endpoint)
        return IntakePlanner(endpoints, environ=environ)
    except (OSError, ValueError, TypeError, KeyError):
        raise ValidationError(
            "The administrator's intake model catalog is unavailable or invalid."
        ) from None


def source_metadata(source: IntakeSource) -> dict[str, Any]:
    return source.model_dump(mode="json", exclude={"text"})


class NativeIntakeService:
    def __init__(
        self,
        store: Store,
        projects: NativeProjectService,
        teams: NativeTeamService,
        source_bytes: IntakeSourceBytes,
        planner_factory: Callable[[UUID], IntakePlanner],
    ) -> None:
        self.store, self.projects, self.teams = store, projects, teams
        self.source_bytes, self.planner_factory = source_bytes, planner_factory

    def _access(
        self, actor: NativeActor, project_id: UUID, *, write: bool = False
    ) -> NativeProject:
        if not isinstance(actor, ActorContext):
            raise AuthorizationError("Intake requires a human project session.")
        project = self.projects._project(actor, project_id, write=write, owner=write)
        if write:
            self.projects._active(project)
        return project

    def _intake(self, project: NativeProject) -> NativeIntake:
        return self.store.native_intake(project.workspace_id, project.id) or NativeIntake(
            workspace_id=project.workspace_id, project_id=project.id, updated_at=project.created_at
        )

    def _audit(self, actor: ActorContext, project_id: UUID, event: str, **details: Any) -> None:
        self.projects.audit.record(
            event_type="native.intake." + event,
            actor=actor,
            resource_type="native_intake",
            resource_id=str(project_id),
            payload={"project_id": str(project_id), **details},
        )

    @staticmethod
    def _latest(sources: tuple[IntakeSource, ...]) -> tuple[IntakeSource, ...]:
        latest: dict[str, IntakeSource] = {}
        for source in sources:
            if (
                source.source_key not in latest
                or latest[source.source_key].revision < source.revision
            ):
                latest[source.source_key] = source
        return tuple(sorted(latest.values(), key=lambda source: source.source_key))

    def _runs(self, actor: ActorContext, project_id: UUID) -> tuple[IntakeRun, ...]:
        runs = self.store.native_intake_runs(actor.workspace_id, project_id)
        output = []
        for run in runs:
            if run.status == "running" and run.deadline_at <= utc_now():
                run = self._save_run(run, status="unknown", error_code="planning_interrupted")
                self._audit(actor, project_id, "interrupted", run_id=str(run.id))
            output.append(run)
        return tuple(output)

    @staticmethod
    def _spend(runs: tuple[IntakeRun, ...]) -> tuple[int, int]:
        return sum(r.charged_microusd for r in runs), sum(r.reserved_microusd for r in runs)

    def view(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._access(actor, project_id)
            manageable = False
            try:
                self._access(actor, project_id, write=True)
                manageable = True
            except (AuthorizationError, InvalidTransitionError):
                pass
            runs = self._runs(actor, project_id)
            spent, reserved = self._spend(runs)
            try:
                models = self.planner_factory(actor.workspace_id).options()
                model_error = None
            except ValidationError:
                models, model_error = (), "Administrator model configuration is unavailable."
            return {
                "intake": self._intake(project).model_dump(mode="json"),
                "sources": [
                    source_metadata(s)
                    for s in self.store.native_intake_sources(actor.workspace_id, project_id)
                ],
                "runs": [r.model_dump(mode="json") for r in runs[:20]],
                "models": models,
                "model_error": model_error,
                "can_manage": manageable,
                "spent_microusd": spent,
                "reserved_microusd": reserved,
            }

    def update(
        self, actor: ActorContext, project_id: UUID, command: UpdateNativeIntake
    ) -> NativeIntake:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._access(actor, project_id, write=True)

            def update() -> NativeIntake:
                current = self._intake(project)
                self.projects._version(current.version, command.expected_version)
                spent, reserved = self._spend(self._runs(actor, project_id))
                if command.budget_microusd < spent + reserved:
                    raise InvalidTransitionError(
                        "Planning allowance cannot be below settled and reserved usage."
                    )
                if command.endpoint_id is not None:
                    available = self.planner_factory(actor.workspace_id).options()
                    if not any(model["id"] == command.endpoint_id for model in available):
                        raise ValidationError("Choose an intake model available to this workspace.")
                result = NativeIntake(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    version=current.version + 1,
                    **command.model_dump(exclude={"idempotency_key", "expected_version"}),
                )
                self.store.save_native_intake(result, current.version)
                self._audit(actor, project_id, "updated", version=result.version)
                return result

            return self.projects._once(
                actor, f"project:{project_id}:intake", command, NativeIntake, update
            )

    def _touch(self, intake: NativeIntake) -> NativeIntake:
        updated = intake.model_copy(update={"version": intake.version + 1, "updated_at": utc_now()})
        self.store.save_native_intake(updated, intake.version)
        return updated

    def upload(self, actor: ActorContext, project_id: UUID, command: SourceUpload) -> IntakeSource:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._access(actor, project_id, write=True)

            def upload() -> IntakeSource:
                intake = self._intake(project)
                self.projects._version(intake.version, command.expected_version)
                try:
                    raw = base64.b64decode(command.content_base64, validate=True)
                except (ValueError, binascii.Error):
                    raise ValidationError("Source must contain valid base64 bytes.") from None
                if len(raw) > MAX_SOURCE_BYTES:
                    raise ValidationError("Source exceeds the 5 MiB limit.")
                sources = self.store.native_intake_sources(actor.workspace_id, project_id)
                keys = {s.source_key for s in sources}
                if (
                    len(sources) >= 2000
                    or len(keys | {command.source_key}) > 500
                    or sum(s.size_bytes for s in sources) + len(raw) > 100 * 1024 * 1024
                ):
                    raise ValidationError("Project intake source capacity has been reached.")
                extracted = extract_source(raw, command.filename, command.media_type)
                sha256 = self.source_bytes.put(actor.workspace_id, project_id, raw)
                result = IntakeSource(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    source_key=command.source_key,
                    filename=command.filename,
                    media_type=command.media_type,
                    revision=1
                    + max(
                        (s.revision for s in sources if s.source_key == command.source_key),
                        default=0,
                    ),
                    sha256=sha256,
                    size_bytes=len(raw),
                    text=extracted.text,
                    extraction_status=extracted.status,
                    redactions=extracted.redactions,
                    truncated=extracted.truncated,
                    created_by=actor.actor_id,
                )
                self.store.insert_native_intake_source(result)
                self._touch(intake)
                self._audit(
                    actor,
                    project_id,
                    "source_added",
                    source_id=str(result.id),
                    sha256=sha256,
                    revision=result.revision,
                )
                return result

            return self.projects._once(
                actor, f"project:{project_id}:source", command, IntakeSource, upload
            )

    def source(self, actor: ActorContext, project_id: UUID, source_id: UUID) -> IntakeSource:
        self._access(actor, project_id)
        source = self.store.native_intake_source(actor.workspace_id, project_id, source_id)
        if source is None or source.revoked_at is not None:
            raise NotFoundError("Source is unavailable or revoked.")
        return source

    def read_source(
        self, actor: ActorContext, project_id: UUID, source_id: UUID
    ) -> tuple[IntakeSource, bytes]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            source = self.source(actor, project_id, source_id)
            return source, self.source_bytes.read(
                actor.workspace_id, project_id, source.sha256, source.size_bytes
            )

    def revoke(
        self,
        actor: ActorContext,
        project_id: UUID,
        source_id: UUID,
        command: VersionedNativeCommand,
    ) -> NativeIntake:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._access(actor, project_id, write=True)

            def revoke() -> NativeIntake:
                intake = self._intake(project)
                self.projects._version(intake.version, command.expected_version)
                self.source(actor, project_id, source_id)
                self.store.revoke_native_intake_source(
                    actor.workspace_id, project_id, source_id, utc_now()
                )
                for run in self.store.native_intake_runs(actor.workspace_id, project_id):
                    if source_id in run.included_source_ids:
                        self._save_run(
                            run,
                            proposal=None,
                            review=None,
                            status="stale"
                            if run.status in {"running", "ready", "questions", "needs_revision"}
                            else run.status,
                            error_code="source_revoked",
                        )
                result = self._touch(intake)
                self._audit(actor, project_id, "source_revoked", source_id=str(source_id))
                return result

            return self.projects._once(
                actor,
                f"project:{project_id}:source:{source_id}:revoke",
                command,
                NativeIntake,
                revoke,
            )

    def _board(
        self, actor: ActorContext, project_id: UUID
    ) -> tuple[tuple[NativeAgent, ...], tuple[NativeTask, ...]]:
        agents: list[NativeAgent] = []
        tasks: list[NativeTask] = []
        for offset in range(0, 1001, 100):
            page = self.store.native_agents(actor.workspace_id, project_id, offset, 100)
            agents.extend(page)
            if len(page) < 100:
                break
        for offset in range(0, 1001, 100):
            page_tasks = self.store.native_tasks(actor.workspace_id, project_id, offset, 100)
            tasks.extend(page_tasks)
            if len(page_tasks) < 100:
                break
        if len(agents) > 1000 or len(tasks) > 1000:
            raise ValidationError(
                "This planning slice supports boards of up to 1,000 roles and tasks."
            )
        return tuple(agents), tuple(tasks)

    def _snapshot(
        self, actor: ActorContext, project: NativeProject
    ) -> tuple[str, tuple[NativeAgent, ...], tuple[NativeTask, ...], tuple[IntakeSource, ...]]:
        agents, tasks = self._board(actor, project.id)
        sources = self._latest(self.store.native_intake_sources(actor.workspace_id, project.id))
        policy = self.teams._policy(project)
        snapshot = {
            "project": project.model_dump(mode="json"),
            "intake": self._intake(project).model_dump(mode="json"),
            "roles": [(str(a.id), a.version) for a in agents],
            "tasks": [(str(t.id), t.version) for t in tasks],
            "sources": [(str(s.id), s.sha256, bool(s.revoked_at)) for s in sources],
            "policy": policy.model_dump(mode="json"),
        }
        return digest(snapshot), agents, tasks, sources

    def _context(
        self, actor: ActorContext, project: NativeProject, command: AnalyzeIntake
    ) -> tuple[dict[str, Any], str, tuple[UUID, ...], tuple[UUID, ...]]:
        snapshot, agents, tasks, sources = self._snapshot(actor, project)
        parsed = [s for s in sources if not s.revoked_at and s.extraction_status == "text"]
        if command.source_ids:
            selected = [s for s in parsed if s.id in command.source_ids]
            if len(selected) != len(command.source_ids) or len(set(command.source_ids)) != len(
                command.source_ids
            ):
                raise ValidationError(
                    "Select distinct current, parsed, unrevoked source revisions."
                )
        else:
            selected = parsed[:12]
        intake = self._intake(project)
        # Each evidence excerpt is explicit and stable; no statement claims full-file reading.
        context = {
            "project": {"name": project.name, "objective": project.objective},
            "intake": intake.model_dump(
                mode="json", include={"background", "outcomes", "constraints", "answers"}
            ),
            "sources": [
                {
                    "id": str(s.id),
                    "source_key": s.source_key,
                    "revision": s.revision,
                    "text": s.text[:3000],
                    "truncated": s.truncated or len(s.text) > 3000,
                }
                for s in selected
            ],
            "omitted_source_count": len(
                [s for s in sources if not s.revoked_at and s not in selected]
            ),
            "existing_roles": [
                a.model_dump(
                    mode="json",
                    include={
                        "id",
                        "role_key",
                        "name",
                        "instructions",
                        "success_criteria",
                        "status",
                    },
                )
                for a in agents
            ],
            "existing_tasks": [
                t.model_dump(
                    mode="json", include={"id", "title", "description", "status", "assignment"}
                )
                for t in tasks[:80]
            ],
            "omitted_task_count": max(0, len(tasks) - 80),
            "max_new_roles": min(
                3,
                max(
                    0,
                    self.teams._policy(project).max_active_agents
                    - sum(a.status == "active" for a in agents),
                ),
            ),
            "policy": (
                "Create/reuse roles and plan board work only. No execution, tool grants, "
                "model policy changes, publishing, or spending beyond these planning calls."
            ),
        }

        # Apply the same redaction to user answers, task/role descriptions and filenames.
        def clean(value: Any) -> Any:
            if isinstance(value, str):
                return redact(value)[0]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        context = clean(context)
        included = tuple(s.id for s in selected)
        omitted = tuple(s.id for s in sources if not s.revoked_at and s.id not in included)
        return context, snapshot, included, omitted

    def _save_run(self, run: IntakeRun, **changes: Any) -> IntakeRun:
        updated = run.model_copy(update={**changes, "version": run.version + 1})
        self.store.update_native_intake_run(updated, run.version)
        return updated

    def get_run(self, actor: ActorContext, project_id: UUID, run_id: UUID) -> IntakeRun:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self._runs(actor, project_id)
            run = self.store.native_intake_run(actor.workspace_id, project_id, run_id)
            if run is None:
                raise NotFoundError("Planning attempt not found.")
            return run

    def analyze(self, actor: ActorContext, project_id: UUID, command: AnalyzeIntake) -> IntakeRun:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._access(actor, project_id, write=True)
            request_digest = digest(command.model_dump(mode="json", exclude={"idempotency_key"}))
            existing = self.store.native_intake_run_by_key(
                actor.workspace_id, project_id, command.idempotency_key
            )
            if existing is not None:
                if (
                    existing.request_digest != request_digest
                    or existing.requested_by != actor.actor_id
                ):
                    raise IdempotencyConflictError(
                        "Planning key was already used with different input."
                    )
                return self.get_run(actor, project_id, existing.id)
            intake = self._intake(project)
            self.projects._version(intake.version, command.expected_version)
            if not intake.endpoint_id:
                raise ValidationError("Select a configured intake model before generating a plan.")
            runs = self._runs(actor, project_id)
            if any(r.status == "running" for r in runs):
                raise InvalidTransitionError(
                    "A planning attempt is already running for this project."
                )
            if len(runs) >= 1000:
                raise ValidationError("This project's planning history capacity has been reached.")
            planner = self.planner_factory(actor.workspace_id)
            context, snapshot, included, omitted = self._context(actor, project, command)
            prepared = planner.prepare(intake.endpoint_id, intake.allow_cloud, context)
            spent, reserved = self._spend(runs)
            if spent + reserved + prepared.reservation_microusd > intake.budget_microusd:
                raise InvalidTransitionError(
                    "Planning allowance cannot cover generation and independent review."
                )
            run = IntakeRun(
                workspace_id=actor.workspace_id,
                project_id=project_id,
                requested_by=actor.actor_id,
                idempotency_key=command.idempotency_key,
                request_digest=request_digest,
                intake_version=intake.version,
                snapshot_digest=snapshot,
                endpoint_id=prepared.endpoint_id,
                model=prepared.model,
                reserved_microusd=prepared.reservation_microusd,
                included_source_ids=included,
                omitted_source_ids=omitted,
                deadline_at=utc_now() + timedelta(minutes=5),
            )
            self.store.insert_native_intake_run(run)
            self._audit(
                actor,
                project_id,
                "started",
                run_id=str(run.id),
                endpoint_id=run.endpoint_id,
                reserved_microusd=run.reserved_microusd,
            )
        return self._generate(actor, run, planner, prepared)

    def _continue(self, actor: ActorContext, run: IntakeRun, prepared: PreparedIntake) -> None:
        # Recheck before the second paid call; no database locks span inference.
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._access(actor, run.project_id, write=True)
            current = self.get_run(actor, run.project_id, run.id)
            if (
                current.status != "running"
                or self._snapshot(actor, project)[0] != run.snapshot_digest
            ):
                raise InvalidTransitionError("Planning context changed or the attempt stopped.")
            current_planner = self.planner_factory(actor.workspace_id)
            if (
                current_planner.configuration_fingerprint(run.endpoint_id)
                != prepared.endpoint_fingerprint
            ):
                raise InvalidTransitionError("Planning model authorization changed.")

    def _generate(
        self, actor: ActorContext, run: IntakeRun, planner: IntakePlanner, prepared: PreparedIntake
    ) -> IntakeRun:
        results: list[TextGenerationResult] = []
        proposal, review = None, None
        status, error = "ready", None
        uncertain = False
        try:
            proposal, result = planner.generate(prepared)
            results.append(result)
            self._continue(actor, run, prepared)
            self._validate_proposal(actor, run, proposal)
            generated_charge = planner.charge(prepared, results)
            if generated_charge is None or generated_charge > run.reserved_microusd:
                # Do not compound missing or out-of-contract usage with another call.
                raise IntakePlanningError("provider_usage_unverified")
            review, result = planner.review(prepared, proposal)
            results.append(result)
            if not review.approved:
                status = "needs_revision"
            elif any(q.blocking for q in proposal.questions):
                status = "questions"
        except IntakePlanningError as exc:
            if exc.result is not None:
                results.append(exc.result)
            uncertain = exc.may_have_been_dispatched and exc.result is None
            status, error = ("unknown" if uncertain else "failed"), exc.code
        except ModelEndpointError as exc:
            uncertain = exc.may_have_been_dispatched
            status, error = ("unknown" if uncertain else "failed"), exc.code
        except (AuthorizationError, InvalidTransitionError, NotFoundError):
            status, error = "stale", "planning_context_changed"
        except (ValidationError, ModelValidationError):
            status, error = "needs_revision", "proposal_invalid"
        except Exception:
            # A process-local exception after dispatch cannot justify a free retry.
            status, error, uncertain = "unknown", "planning_interrupted", True
        charged = planner.charge(prepared, results)
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            current = self.store.native_intake_run(actor.workspace_id, run.project_id, run.id)
            assert current is not None
            authorised = True
            evidence_current = True
            try:
                project = self._access(actor, run.project_id, write=True)
                if self._snapshot(actor, project)[0] != run.snapshot_digest:
                    evidence_current = False
                    status, error = "stale", "planning_context_changed"
            except (AuthorizationError, NotFoundError, InvalidTransitionError):
                authorised = False
                evidence_current = False
                status, error = "stale", "planning_authority_changed"
            except ValidationError:
                evidence_current = False
                status, error = "stale", "planning_context_changed"
            if current.status in {"cancelled", "unknown"}:
                status = current.status
            # Missing usage or unknown dispatch retains the full conservative reservation.
            held = run.reserved_microusd if uncertain or charged is None else 0
            settled = 0 if held else (charged or 0)
            if not held and settled > run.reserved_microusd:
                if status not in {"cancelled", "stale", "unknown"}:
                    status = "needs_revision"
                error = "provider_usage_exceeded_reservation"
            current = self._save_run(
                current,
                status=status,
                error_code=error,
                reserved_microusd=held,
                charged_microusd=settled,
                proposal=proposal if evidence_current and status != "cancelled" else None,
                review=review if evidence_current and status != "cancelled" else None,
                finished_at=utc_now(),
                input_tokens=sum(r.input_tokens for r in results if r.input_tokens is not None)
                if not uncertain and results and all(r.input_tokens is not None for r in results)
                else None,
                output_tokens=sum(r.output_tokens for r in results if r.output_tokens is not None)
                if not uncertain and results and all(r.output_tokens is not None for r in results)
                else None,
            )
            self._audit(
                actor,
                run.project_id,
                "finished",
                run_id=str(run.id),
                status=current.status,
                charged_microusd=settled,
                reserved_microusd=held,
            )
            automatic = (
                authorised and current.status == "ready" and self._intake(project).auto_staff
            )
        # Settlement must survive denied reads and failed application; a known provider
        # charge must never disappear because its initiating owner lost access.
        if not authorised:
            raise AuthorizationError("Project owner authority changed during planning.")
        if automatic or current.status == "questions":
            try:
                with (
                    self.store.transaction(IDENTITY_LOCK),
                    self.store.transaction(actor.workspace_id),
                ):
                    project = self._access(actor, run.project_id, write=True)
                    current = self.get_run(actor, run.project_id, run.id)
                    if current.status == "ready":
                        current = self._apply(actor, project, current)
                    elif current.status == "questions":
                        assert current.proposal is not None
                        self._validate_proposal(actor, current, current.proposal)
                        decisions = self._decisions(actor, project, current)
                        current = self._save_run(current, applied_task_ids=decisions)
                        self._audit(
                            actor,
                            project.id,
                            "questions_recorded",
                            run_id=str(current.id),
                            task_ids=[str(item) for item in decisions],
                        )
            except (ValidationError, ModelValidationError, InvalidTransitionError) as exc:
                with (
                    self.store.transaction(IDENTITY_LOCK),
                    self.store.transaction(actor.workspace_id),
                ):
                    current = self.get_run(actor, run.project_id, run.id)
                    if current.status in {"ready", "questions"}:
                        stale = isinstance(exc, InvalidTransitionError)
                        current = self._save_run(
                            current,
                            status="stale" if stale else "needs_revision",
                            error_code="planning_context_changed" if stale else "proposal_invalid",
                        )
        return current

    def _validate_proposal(
        self, actor: ActorContext, run: IntakeRun, proposal: StaffingProposal
    ) -> None:
        project = self._access(actor, run.project_id, write=True)
        snapshot, agents, tasks, sources = self._snapshot(actor, project)
        if snapshot != run.snapshot_digest:
            raise InvalidTransitionError("Planning context changed. Generate a new plan.")
        source_map = {
            s.id: s for s in sources if not s.revoked_at and s.id in run.included_source_ids
        }
        for finding in proposal.findings:
            for evidence in finding.evidence:
                if (
                    evidence.source_id not in source_map
                    or evidence.quote not in source_map[evidence.source_id].text[:3000]
                ):
                    raise ValidationError("A finding does not quote the included source revision.")
        role_map = {a.id: a for a in agents}
        existing_keys = {a.role_key for a in agents}
        existing_names = {a.name.casefold() for a in agents}
        names: set[str] = set()
        new = [r for r in proposal.roles if r.action == "create"]
        if len(new) > min(
            3,
            self.teams._policy(project).max_active_agents
            - sum(a.status == "active" for a in agents),
        ):
            raise ValidationError("Proposal exceeds the available staffing allowance.")
        for role in proposal.roles:
            if role.name.casefold() in names:
                raise ValidationError("Proposal duplicates a responsibility name.")
            names.add(role.name.casefold())
            if role.action == "reuse":
                agent = role_map.get(role.agent_id) if role.agent_id else None
                if agent is None or agent.status != "active" or role.role_key != agent.role_key:
                    raise ValidationError("Reused roles must be active roles in this project.")
            elif role.role_key in existing_keys or role.name.casefold() in existing_names:
                raise ValidationError("Existing roles must be reused instead of duplicated.")
        proposal_keys = {r.role_key for r in proposal.roles}
        used: set[str] = set()
        existing_tasks = {t.id: t for t in tasks}
        titles = {t.title.casefold() for t in tasks}
        for task in proposal.tasks:
            for key in (task.role_key, task.review_role_key):
                if key is not None and key not in proposal_keys:
                    raise ValidationError("Task refers to a role outside this proposal.")
            if task.existing_task_id is not None:
                if task.existing_task_id not in existing_tasks:
                    raise ValidationError("Reused task must belong to this project.")
            else:
                if len(self._task_description(run, task)) > 8000:
                    raise ValidationError("Task and acceptance text exceed the native task limit.")
                if task.title.casefold() in titles:
                    raise ValidationError("Existing work must be reused instead of duplicated.")
                titles.add(task.title.casefold())
                used.update(k for k in (task.role_key, task.review_role_key) if k is not None)
        if any(r.role_key not in used for r in new):
            raise ValidationError("New roles must own or independently review concrete new work.")

    @staticmethod
    def _task_description(run: IntakeRun, task: IntakeTask) -> str:
        return (
            f"{task.description}\n\nAcceptance criteria:\n{task.acceptance}\n\n"
            f"Planning attempt: {run.id}. Independent review is required before acceptance."
        )

    def _human_assignment(
        self, actor: ActorContext, project: NativeProject, *, exclude: UUID | None = None
    ) -> TaskAssignment:
        people = self.projects.access(actor, project.id).members
        eligible = [p.actor_id for p in people if p.can_assign and p.actor_id != exclude]
        human = actor.actor_id if actor.actor_id in eligible else next(iter(eligible), None)
        return TaskAssignment(kind="human", actor_id=human) if human else TaskAssignment()

    def _decisions(
        self, actor: ActorContext, project: NativeProject, run: IntakeRun
    ) -> tuple[UUID, ...]:
        assert run.proposal is not None
        _, tasks = self._board(actor, project.id)
        assignment = self._human_assignment(actor, project)
        result = []
        for question in run.proposal.questions:
            marker = "Intake decision: " + digest({"key": question.key, "text": question.question})
            existing = next(
                (
                    t
                    for t in tasks
                    if t.status not in {"done", "cancelled"}
                    and marker in t.description.splitlines()
                ),
                None,
            )
            if existing is not None:
                result.append(existing.id)
                continue
            decision = self.projects.create_task(
                actor,
                project.id,
                CreateNativeTask(
                    title=("Decision: " + question.question)[:200],
                    description=(
                        f"{question.question}\n\nWhy this matters: {question.why}\n"
                        "Answer in project intake, then generate a revised plan. "
                        f"Planning attempt: {run.id}\n{marker}"
                    ),
                    assignment=assignment,
                    idempotency_key=f"intake:{run.id}:question:{question.key}",
                ),
            )
            result.append(decision.id)
        return tuple(result)

    def _apply(self, actor: ActorContext, project: NativeProject, run: IntakeRun) -> IntakeRun:
        if (
            run.status != "ready"
            or run.proposal is None
            or run.review is None
            or not run.review.approved
        ):
            raise InvalidTransitionError("Only reviewed, valid proposals can be applied.")
        self._validate_proposal(actor, run, run.proposal)
        roles: dict[str, UUID] = {}
        for role in run.proposal.roles:
            if role.action == "reuse":
                assert role.agent_id is not None
                roles[role.role_key] = role.agent_id
            else:
                agent = self.teams.create(
                    actor,
                    project.id,
                    CreateNativeAgent(
                        role_key=role.role_key,
                        name=role.name,
                        instructions=role.instructions,
                        success_criteria=role.success_criteria,
                        rationale=role.rationale,
                        idempotency_key=f"intake:{run.id}:role:{role.role_key}",
                    ),
                )
                roles[role.role_key] = agent.id
        human_assignment = self._human_assignment(actor, project)
        task_ids = []
        for task in run.proposal.tasks:
            if task.existing_task_id is not None:
                task_ids.append(task.existing_task_id)
                continue
            assignment = (
                TaskAssignment(kind="agent", agent_id=roles[task.role_key])
                if task.role_key
                else human_assignment
                if task.assignment == "human"
                else TaskAssignment()
            )
            created = self.projects.create_task(
                actor,
                project.id,
                CreateNativeTask(
                    title=task.title,
                    description=self._task_description(run, task),
                    assignment=assignment,
                    idempotency_key=f"intake:{run.id}:task:{task.key}",
                ),
            )
            task_ids.append(created.id)
            review_assignment = (
                TaskAssignment(kind="agent", agent_id=roles[task.review_role_key])
                if task.review_role_key
                else self._human_assignment(actor, project, exclude=assignment.actor_id)
            )
            review_task = self.projects.create_task(
                actor,
                project.id,
                CreateNativeTask(
                    title=("Review: " + task.title)[:200],
                    description=(
                        f"Independently inspect task {created.id} against these acceptance "
                        f"criteria:\n{task.acceptance}\n\nRecord evidence and revisions. "
                        "This board item does not itself approve publication "
                        "or certify an artifact."
                    ),
                    assignment=review_assignment,
                    idempotency_key=f"intake:{run.id}:review:{task.key}",
                ),
            )
            task_ids.append(review_task.id)
        task_ids.extend(self._decisions(actor, project, run))
        result = self._save_run(
            run,
            status="applied",
            applied_agent_ids=tuple(roles.values()),
            applied_task_ids=tuple(task_ids),
        )
        self._audit(
            actor,
            project.id,
            "applied",
            run_id=str(run.id),
            agent_ids=[str(i) for i in result.applied_agent_ids],
            task_ids=[str(i) for i in result.applied_task_ids],
        )
        return result

    def apply(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, command: VersionedNativeCommand
    ) -> IntakeRun:
        try:
            with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
                project = self._access(actor, project_id, write=True)

                def apply() -> IntakeRun:
                    run = self.get_run(actor, project_id, run_id)
                    self.projects._version(run.version, command.expected_version)
                    return self._apply(actor, project, run)

                self.projects._once(
                    actor, f"project:{project_id}:intake:{run_id}:apply", command, IntakeRun, apply
                )
                # Receipts prove application, but current revocation owns readable evidence.
                return self.get_run(actor, project_id, run_id)
        except InvalidTransitionError:
            # Preserve the failed command's 409 while retiring an outdated review.
            with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
                project = self._access(actor, project_id, write=True)
                run = self.get_run(actor, project_id, run_id)
                if (
                    run.status == "ready"
                    and self._snapshot(actor, project)[0] != run.snapshot_digest
                ):
                    self._save_run(run, status="stale", error_code="planning_context_changed")
                    self._audit(actor, project_id, "stale", run_id=str(run.id))
            raise

    def cancel(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, command: VersionedNativeCommand
    ) -> IntakeRun:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def cancel() -> IntakeRun:
                run = self.get_run(actor, project_id, run_id)
                self.projects._version(run.version, command.expected_version)
                if run.status == "applied":
                    raise InvalidTransitionError(
                        "Applied plans must be steered through the board and team."
                    )
                result = self._save_run(run, status="cancelled", finished_at=utc_now())
                self._audit(actor, project_id, "cancelled", run_id=str(run.id))
                return result

            self.projects._once(
                actor, f"project:{project_id}:intake:{run_id}:cancel", command, IntakeRun, cancel
            )
            return self.get_run(actor, project_id, run_id)
