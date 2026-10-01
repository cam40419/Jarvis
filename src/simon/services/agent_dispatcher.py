"""Concurrent DAG execution with durable claims and no automatic side-effect replay."""

from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Event
from typing import Any
from uuid import UUID, uuid5

from simon.adapters.application_tools import ApplicationToolTransport
from simon.adapters.cloud_storage_tools import BoxTransport, DropboxTransport, OneDriveTransport
from simon.adapters.environment_tools import EnvironmentCommandTransport
from simon.adapters.github_tools import GitHubTransport
from simon.adapters.mcp_http import MCPHttpTransport
from simon.adapters.model_endpoints import ModelEndpointClient
from simon.adapters.tool_transports import HttpJsonTransport, ToolHandler, TransportRegistry
from simon.adapters.webdav_tools import WebDAVTransport
from simon.domain.agent_platform import AgentProfile, AgentTaskSpec, AgentTeamPlan, PlannedAgentTask
from simon.domain.agent_runs import AgentRun, TaskExecution
from simon.domain.agent_worker import WorkerResult
from simon.domain.artifacts import Artifact, ArtifactError
from simon.domain.errors import DomainError
from simon.domain.execution import EnvironmentLease, ExecutionCapacityError, ExecutionError
from simon.domain.models import ActorContext, JobStatus, utc_now
from simon.services.agent_runs import DISPATCH_LOCK, RUN_KIND, AgentRunService
from simon.services.agent_worker import AgentWorker, WorkerCheckpointError
from simon.services.artifact_handoff import prepare_dependency_artifacts
from simon.services.artifacts import ArtifactStore

WorkerFactory = Callable[[AgentProfile, EnvironmentLease | None, ActorContext, UUID], AgentWorker]
TransportFactory = Callable[
    [ActorContext, UUID, Callable[[], ActorContext], EnvironmentLease | None],
    Mapping[str, ToolHandler],
]


class AgentDispatcher:
    def __init__(
        self,
        runs: AgentRunService,
        *,
        artifacts: ArtifactStore | None = None,
        worker_factory: WorkerFactory | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.runs, self.platform = runs, runs.platform
        self.artifacts = artifacts or ArtifactStore(self.platform.state_dir / "artifacts")
        self.worker_factory = worker_factory or self._worker
        self.transport_factory = transport_factory

    def _worker(
        self,
        profile: AgentProfile,
        lease: EnvironmentLease | None,
        actor: ActorContext,
        run_id: UUID,
    ) -> AgentWorker:
        executor_id = self.runs.view(self.runs.job(run_id)).executor_id

        def revalidate() -> ActorContext:
            job = self.runs.job(run_id)
            state = self.runs.view(job)
            if (
                state.cancel_requested
                or state.status != JobStatus.RUNNING
                or state.executor_id != executor_id
            ):
                raise WorkerCheckpointError("cancelled")
            if lease is not None:
                live_lease = self.platform.environments.get(lease.id)
                if (
                    live_lease.status != "active"
                    or live_lease.plan != lease.plan
                    or live_lease.fencing_token != lease.fencing_token
                ):
                    raise WorkerCheckpointError("environment_lease_changed")
            return self.runs.live_actor(job)

        transports = TransportRegistry()
        transports.register(
            "http",
            HttpJsonTransport(
                timeout_seconds=min(30, profile.timeout_seconds),
                environ=self.platform._environ,
            ),
        )
        transports.register(
            "mcp",
            MCPHttpTransport(
                timeout_seconds=min(30, profile.timeout_seconds),
                environ=self.platform._environ,
                before_call=revalidate,
            ),
        )
        transports.register(
            "github",
            GitHubTransport(
                timeout_seconds=min(30, profile.timeout_seconds),
                environ=self.platform._environ,
            ),
        )
        transports.register(
            "webdav",
            WebDAVTransport(
                timeout_seconds=min(30, profile.timeout_seconds),
                environ=self.platform._environ,
            ),
        )
        for name, transport_type in (
            ("dropbox", DropboxTransport),
            ("box", BoxTransport),
            ("onedrive", OneDriveTransport),
        ):
            transports.register(
                name,
                transport_type(
                    timeout_seconds=min(30, profile.timeout_seconds),
                    environ=self.platform._environ,
                ),
            )
        if lease is not None:
            if lease.definition.kind == "machine":
                transports.register(
                    "application",
                    ApplicationToolTransport(
                        self.platform.environments,
                        lease,
                        actor_id=actor.actor_id,
                        run_id=run_id,
                        max_timeout_seconds=min(300, profile.timeout_seconds),
                    ),
                )
            transports.register(
                "environment",
                EnvironmentCommandTransport(
                    self.platform.environments,
                    lease,
                    actor_id=actor.actor_id,
                    run_id=run_id,
                    max_timeout_seconds=min(60, profile.timeout_seconds),
                ),
            )
            from simon.adapters.browser_tools import BrowserToolTransport
            from simon.adapters.cad_tools import CadToolTransport
            from simon.adapters.generative_tools import GenerativeToolTransport
            from simon.adapters.git_tools import GitToolTransport
            from simon.adapters.pcb_tools import PCBToolTransport
            from simon.adapters.processing_tools import ProcessingToolTransport

            transports.register(
                "git",
                GitToolTransport(
                    self.platform.environments,
                    lease,
                    actor_id=actor.actor_id,
                    run_id=run_id,
                    max_timeout_seconds=min(60, profile.timeout_seconds),
                ),
            )
            transports.register(
                "processing",
                ProcessingToolTransport(
                    self.platform.environments,
                    lease,
                    actor_id=actor.actor_id,
                    run_id=run_id,
                    max_timeout_seconds=min(60, profile.timeout_seconds),
                ),
            )
            transports.register(
                "browser",
                BrowserToolTransport(
                    self.platform.environments,
                    lease,
                    actor_id=actor.actor_id,
                    run_id=run_id,
                    max_timeout_seconds=min(60, profile.timeout_seconds),
                ),
            )
            transports.register(
                "cad",
                CadToolTransport(
                    self.platform.environments,
                    lease,
                    actor_id=actor.actor_id,
                    run_id=run_id,
                    max_timeout_seconds=min(300, profile.timeout_seconds),
                ),
            )
            transports.register(
                "pcb",
                PCBToolTransport(
                    self.platform.environments,
                    lease,
                    actor_id=actor.actor_id,
                    run_id=run_id,
                    max_timeout_seconds=min(120, profile.timeout_seconds),
                ),
            )
            if lease.definition.kind == "docker":
                transports.register(
                    "generative",
                    GenerativeToolTransport(
                        lease,
                        actor=actor,
                        run_id=run_id,
                        revalidate=revalidate,
                        environ=self.platform._environ,
                        timeout_seconds=min(120, profile.timeout_seconds),
                    ),
                )
        if self.transport_factory is not None:
            handlers = self.transport_factory(actor, run_id, revalidate, lease)
            for transport, handler in handlers.items():
                transports.register(transport, handler)
        return AgentWorker(
            ModelEndpointClient(
                self.platform.manifest.models,
                environ=self.platform._environ,
                timeout_seconds=min(120, profile.timeout_seconds),
            ),
            self.platform.tools,
            transports,
        )

    def tick(self) -> AgentRun | None:
        for job in self.runs.store.jobs_all(RUN_KIND, 100):
            claimed = self.runs.claim(job.id)
            if claimed is not None:
                return self._execute(claimed)
        return None

    def execute(self, identifier: UUID) -> AgentRun | None:
        claimed = self.runs.claim(identifier)
        return self._execute(claimed) if claimed is not None else None

    def _execute(self, claimed: AgentRun) -> AgentRun:
        assert claimed.executor_id is not None
        executor_id = claimed.executor_id
        job = self.runs.job(claimed.id)
        plan_job = self.runs.store.get_job(claimed.plan_id)
        assert plan_job is not None
        plan = AgentTeamPlan.model_validate(plan_job.input["plan"])
        specs = {
            item["id"]: AgentTaskSpec.model_validate(item)
            for item in plan_job.input["request"]["tasks"]
        }
        assignments = {task.id: task for task in plan.tasks}
        pending = set(assignments)
        active: dict[Future[None], str] = {}
        resource_counts: dict[str, int] = {}
        try:
            self.runs.live_actor(job)
            with ThreadPoolExecutor(
                max_workers=claimed.reserved_slots, thread_name_prefix="simon-agent"
            ) as pool:
                while pending or active:
                    state = self.runs.view(self.runs.job(claimed.id))
                    records = {item.id: item for item in state.tasks}
                    uncertain = any(item.status == "unknown" for item in records.values())
                    for name in tuple(task.id for task in plan.tasks if task.id in pending):
                        task = assignments[name]
                        if state.cancel_requested or uncertain:
                            self._skip(claimed, name, "cancelled", "run_stopped")
                            pending.remove(name)
                            continue
                        dependencies = [records[key] for key in task.depends_on]
                        if any(
                            item.status in {"failed", "blocked", "cancelled", "unknown"}
                            for item in dependencies
                        ):
                            self._skip(claimed, name, "blocked", "dependency_failed")
                            pending.remove(name)
                            continue
                        if any(item.status != "succeeded" for item in dependencies):
                            continue
                        if len(active) >= claimed.reserved_slots:
                            break
                        resource = task.environment.environment_id if task.environment else None
                        if resource is not None:
                            definition = self.platform.environments.definitions[resource]
                            capacity = definition.max_concurrency
                            if resource_counts.get(resource, 0) >= capacity:
                                continue
                        outputs = {item.id: item.output for item in dependencies}
                        if not self._reserve_task(claimed, task):
                            continue
                        active[pool.submit(self._task, claimed, task, specs[name], outputs)] = name
                        pending.remove(name)
                        if resource is not None:
                            resource_counts[resource] = resource_counts.get(resource, 0) + 1
                    if active:
                        finished, _waiting = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                        for future in finished:
                            name = active.pop(future)
                            resource_plan = assignments[name].environment
                            if resource_plan is not None:
                                resource_counts[resource_plan.environment_id] -= 1
                            future.result()
                    elif pending:
                        # Other runs may own the shared machine/container capacity.
                        Event().wait(0.2)
        except Exception:
            # Never requeue a claimed run: a process/callback failure may follow a dispatched call.
            return self.runs.update(
                claimed.id,
                lambda state: state.model_copy(
                    update={
                        "status": JobStatus.NEEDS_HUMAN,
                        "finished_at": utc_now(),
                        "reserved_slots": 0,
                        "tasks": tuple(
                            item.model_copy(
                                update={
                                    "status": "unknown" if item.status == "running" else "blocked",
                                    "error_code": "dispatch_interrupted",
                                }
                            )
                            if item.status in {"queued", "running"}
                            else item
                            for item in state.tasks
                        ),
                    }
                ),
                executor_id=executor_id,
            )

        def finish(state: AgentRun) -> AgentRun:
            statuses = {item.status for item in state.tasks}
            status = (
                JobStatus.NEEDS_HUMAN
                if "unknown" in statuses
                else JobStatus.CANCELLED
                if state.cancel_requested
                else JobStatus.FAILED
                if statuses != {"succeeded"}
                else JobStatus.SUCCEEDED
            )
            return state.model_copy(
                update={"status": status, "reserved_slots": 0, "finished_at": utc_now()}
            )

        return self.runs.update(claimed.id, finish, executor_id=executor_id)

    def _skip(self, run: AgentRun, name: str, status: str, reason: str) -> None:
        assert run.executor_id is not None
        self.runs.task_update(
            run.id,
            name,
            lambda item: item.model_copy(
                update={
                    "status": status,
                    "error_code": reason,
                }
            ),
            executor_id=run.executor_id,
        )

    def _reserve_task(self, run: AgentRun, task: PlannedAgentTask) -> bool:
        assert run.executor_id is not None
        with self.runs.store.transaction(DISPATCH_LOCK):
            state = self.runs.view(self.runs.job(run.id))
            if state.cancel_requested:
                return False
            if task.environment is not None:
                identifier = task.environment.environment_id
                capacity = self.platform.environments.definitions[identifier].max_concurrency
                used = sum(
                    record.status == "running" and record.environment_id == identifier
                    for job in self.runs.store.jobs_all(RUN_KIND, 129, status="running")
                    for record in self.runs.view(job).tasks
                )
                if used >= capacity:
                    return False
            self.runs.task_update(
                run.id,
                task.id,
                lambda item: item.model_copy(update={"status": "running"}),
                executor_id=run.executor_id,
            )
            return True

    def _task(
        self,
        run: AgentRun,
        planned: PlannedAgentTask,
        spec: AgentTaskSpec,
        dependencies: dict[str, str],
    ) -> None:
        assert run.executor_id is not None
        executor_id = run.executor_id
        lease = None
        attempt = uuid5(run.id, planned.id)
        result = WorkerResult(status="failed", error_code="task_not_started")
        published: tuple[Artifact, ...] = ()
        try:
            job = self.runs.job(run.id)
            if self.runs.view(job).cancel_requested:
                raise WorkerCheckpointError("cancelled")
            actor = self.runs.live_actor(job)
            profile = self.runs.profile(actor, planned.agent_id, run.plan_id)
            project_id = self.platform.get(actor, run.plan_id).project_id
            current = self.platform._task(actor, run.plan_id, spec, profile, project_id)
            if current.blocked_reasons or current.model != planned.model:
                raise WorkerCheckpointError("assignment_no_longer_available")
            task = planned.model_copy(update={"attempt_id": attempt})
            if task.environment is not None:
                request = task.environment.request.model_copy(update={"attempt_id": attempt})
                lease = self.platform.environments.allocate(
                    request,
                    environment_id=task.environment.environment_id,
                )
                if lease.status != "active":
                    raise ExecutionError("The execution resource has an unresolved lease")
                task = task.model_copy(update={"environment": lease.plan})
                lease_id = lease.id
                self.runs.task_update(
                    run.id,
                    task.id,
                    lambda item: item.model_copy(
                        update={
                            "environment_lease_id": lease_id,
                        }
                    ),
                    executor_id=executor_id,
                )

            def cancelled() -> bool:
                state = self.runs.view(self.runs.job(run.id))
                return state.cancel_requested or state.executor_id != run.executor_id

            def checkpoint(event: dict[str, Any]) -> None:
                # Preserve completion receipts even when cancellation/access changes
                # during an in-flight call. The next dispatch still rechecks authority.
                if event.get("event") in {"model_dispatch", "tool_dispatch"}:
                    if cancelled():
                        raise WorkerCheckpointError("cancelled")
                    live = self.runs.live_actor(self.runs.job(run.id))
                    self.platform.tools.resolve(
                        task.tool_ids,
                        scopes=live.scopes & profile.tool_scopes,
                        environment_capabilities=lease.definition.capabilities if lease else (),
                    )
                    if lease is not None:
                        self.platform.environments.heartbeat(
                            lease.id,
                            attempt_id=attempt,
                            fencing_token=lease.fencing_token,
                        )
                self.runs.task_update(
                    run.id,
                    task.id,
                    lambda item: item.model_copy(
                        update={
                            "steps": max(item.steps, event["step"])
                            if type(event.get("step")) is int
                            else item.steps,
                            "tool_calls": item.tool_calls + (event.get("event") == "tool_dispatch"),
                            "events": (
                                *item.events,
                                {**event, "occurred_at": utc_now().isoformat()},
                            )[-128:],
                        }
                    ),
                    executor_id=executor_id,
                )

            saved_plan = self.platform.get(actor, run.plan_id)
            context_id = saved_plan.context_id
            context_name = next(
                (
                    context.name
                    for context in self.platform.manifest.contexts
                    if context.id == context_id
                ),
                "",
            )

            def authorize_inputs() -> None:
                if cancelled():
                    raise WorkerCheckpointError("cancelled")
                live = self.runs.live_actor(self.runs.job(run.id))
                if (live.actor_id, live.household_id) != (actor.actor_id, actor.household_id):
                    raise WorkerCheckpointError("assignment_no_longer_available")
                if lease is not None:
                    current_lease = self.platform.environments.get(lease.id)
                    if (
                        current_lease.status != "active"
                        or current_lease.plan != lease.plan
                        or current_lease.fencing_token != lease.fencing_token
                    ):
                        raise WorkerCheckpointError("environment_lease_changed")

            records = {item.id: item for item in self.runs.view(self.runs.job(run.id)).tasks}
            inputs = prepare_dependency_artifacts(
                self.artifacts,
                actor=actor,
                run_id=run.id,
                dependencies=tuple(records[name] for name in planned.depends_on),
                task_ids={item.id: item.task_id for item in saved_plan.tasks},
                workspace=(
                    lease.plan.workspace_path
                    if lease is not None and lease.definition.kind == "docker"
                    else None
                ),
                revalidate=authorize_inputs,
            )
            if inputs:
                self.runs.task_update(
                    run.id,
                    task.id,
                    lambda item: item.model_copy(update={"input_artifacts": inputs}),
                    executor_id=executor_id,
                )
            result = self.worker_factory(profile, lease, actor, run.id).execute(
                actor=actor,
                run_id=run.id,
                task=task,
                spec=spec,
                profile=profile,
                dependency_outputs=dependencies,
                dependency_artifacts=inputs,
                context_name=context_name,
                environment_capabilities=lease.definition.capabilities if lease else frozenset(),
                cancelled=cancelled,
                checkpoint=checkpoint,
            )
            if result.status == "succeeded":
                if cancelled():
                    result = result.model_copy(
                        update={"status": "cancelled", "output": "", "error_code": "cancelled"}
                    )
                else:
                    # Artifact publication is owned by the controller, never a model-provided path.
                    self.runs.live_actor(self.runs.job(run.id))
                    file_artifacts: tuple[Artifact, ...] = ()
                    if result.artifact_paths:
                        if lease is None or lease.definition.kind != "docker":
                            raise WorkerCheckpointError("artifact_workspace_unavailable")
                        self.platform.environments.release(
                            lease.id,
                            attempt_id=attempt,
                            fencing_token=lease.fencing_token,
                        )
                        lease = lease.model_copy(update={"status": "released"})
                        if cancelled():
                            raise WorkerCheckpointError("cancelled")
                        self.runs.live_actor(self.runs.job(run.id))
                        file_artifacts = self.artifacts.publish_workspace_files(
                            workspace=lease.plan.workspace_path,
                            paths=result.artifact_paths,
                            workspace_id=actor.household_id,
                            actor_id=actor.actor_id,
                            run_id=run.id,
                            task_id=task.task_id,
                        )
                    published = (
                        self.artifacts.publish_text(
                            workspace_id=actor.household_id,
                            actor_id=actor.actor_id,
                            run_id=run.id,
                            task_id=task.task_id,
                            text=result.output,
                            name="answer.json" if profile.output_format == "json" else "answer.txt",
                            media_type=(
                                "application/json"
                                if profile.output_format == "json"
                                else "text/plain"
                            ),
                        ),
                        *file_artifacts,
                    )
        except ArtifactError:
            result = WorkerResult(
                status="failed", error_code="artifact_integrity_or_handoff_failed"
            )
        except ExecutionCapacityError:
            result = WorkerResult(status="failed", error_code="environment_capacity_unavailable")
        except ExecutionError:
            result = WorkerResult(status="unknown", error_code="environment_outcome_unknown")
        except WorkerCheckpointError as error:
            result = WorkerResult(
                status="cancelled" if error.code == "cancelled" else "failed", error_code=error.code
            )
        except DomainError:
            result = WorkerResult(
                status="failed", error_code="authorization_or_configuration_changed"
            )
        except Exception:
            result = WorkerResult(status="unknown", error_code="worker_interrupted")
        finally:
            if lease is None and planned.environment is not None:
                try:
                    lease = self.platform.environments.get_for_attempt(attempt)
                    if lease is not None:
                        recovered_id = lease.id
                        self.runs.task_update(
                            run.id,
                            planned.id,
                            lambda item: item.model_copy(
                                update={
                                    "environment_lease_id": recovered_id,
                                }
                            ),
                            executor_id=executor_id,
                        )
                except Exception:
                    # A journal/DB failure must not suppress cleanup when ownership
                    # was recovered successfully before recording the reference.
                    result = result.model_copy(
                        update={
                            "status": "unknown",
                            "error_code": "lease_reference_not_persisted",
                        }
                    )
            if lease is not None and lease.status != "released":
                try:
                    self.platform.environments.release(
                        lease.id,
                        attempt_id=lease.plan.request.attempt_id,
                        fencing_token=lease.fencing_token,
                    )
                except Exception:
                    result = result.model_copy(
                        update={"status": "unknown", "error_code": "environment_cleanup_unknown"}
                    )

        def finish(item: TaskExecution) -> TaskExecution:
            return item.model_copy(
                update={
                    "status": (
                        "blocked"
                        if result.error_code == "environment_capacity_unavailable"
                        else result.status
                    ),
                    "output": result.output,
                    "error_code": result.error_code,
                    "steps": result.steps,
                    "tool_calls": result.tool_calls,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "artifacts": published,
                }
            )

        self.runs.task_update(run.id, planned.id, finish, executor_id=run.executor_id)
