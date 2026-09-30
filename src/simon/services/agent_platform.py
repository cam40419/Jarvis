"""Compile reusable teams into durable, reviewable plans without executing them."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from dotenv import dotenv_values
from pydantic import ValidationError as PydanticError

from simon.adapters.execution_backends import DockerBackend, MachineBackend
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    AgentTeamPlan,
    PlannedAgentTask,
    PlanTeamRequest,
    PlatformManifest,
)
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from simon.domain.execution import EnvironmentRequest, ExecutionError
from simon.domain.model_routing import RoutingRequest
from simon.domain.models import ActorContext, Job, JobStatus
from simon.domain.ports import Store
from simon.domain.tool_catalog import ToolCatalogError
from simon.services.agent_prompts import render_agent_prompt
from simon.services.audit import AuditService
from simon.services.canonical import canonical_json, digest
from simon.services.execution import EnvironmentManager
from simon.services.model_router import ModelRouter, ModelRoutingError
from simon.services.tool_catalog import ToolCatalog, builtin_tool_templates

PLAN_KIND = "platform.plan"


def stable_configuration(value: Any) -> Any:
    """Canonicalize sets before JSON conversion; preserve ordered task/argument lists."""
    if isinstance(value, dict):
        return {key: stable_configuration(child) for key, child in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((stable_configuration(child) for child in value), key=canonical_json)
    if isinstance(value, list | tuple):
        return [stable_configuration(child) for child in value]
    if isinstance(value, UUID | Path):
        return str(value)
    return value


def platform_credentials() -> dict[str, str]:
    import os

    return {
        **{key: value for key, value in dotenv_values(".env").items() if value is not None},
        **os.environ,
    }


def load_manifest(path: Path | None) -> PlatformManifest:
    if path is None:
        return PlatformManifest()
    try:
        with path.open("rb") as source:
            content = source.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("oversized manifest")
        return PlatformManifest.model_validate_json(content)
    except (OSError, ValueError) as error:
        # Pydantic errors can include input values; never echo configuration contents.
        raise ValidationError("Agent platform manifest is invalid or unavailable") from error


class AgentPlatformService:
    def __init__(
        self,
        store: Store,
        manifest: PlatformManifest,
        *,
        state_dir: Path,
        environ: Mapping[str, str] | None = None,
        available_transports: Sequence[str] = ("http", "environment"),
    ) -> None:
        self.store, self.manifest = store, manifest
        self.state_dir = state_dir
        self.audit = AuditService(store)
        self._environ = platform_credentials() if environ is None else environ
        self.models = ModelRouter(manifest.models, environ=self._environ)
        self.tools = ToolCatalog(manifest.tools, available_transports=available_transports)
        self.environments = EnvironmentManager(
            list(manifest.environments),
            state_path=state_dir / "environment-leases.sqlite3",
            workspace_root=state_dir / "workspaces",
            backends={"docker": DockerBackend(), "machine": MachineBackend(environ=self._environ)},
        )

    @staticmethod
    def authorize(actor: ActorContext, *, write: bool = False) -> None:
        if ("jobs:write" if write else "jobs:read") not in actor.scopes:
            raise AuthorizationError("Agent platform permission required")

    def _context(self, actor: ActorContext, context_id: str | None) -> None:
        if context_id is None:
            return
        if not any(
            context.id == context_id
            and context.workspace_id == actor.household_id
            and (not context.actor_ids or actor.actor_id in context.actor_ids)
            for context in self.manifest.contexts
        ):
            raise NotFoundError("Work context not found")

    def catalog(self, actor: ActorContext) -> dict[str, Any]:
        self.authorize(actor)
        teams = tuple(
            team for team in self.manifest.teams
            if not team.allowed_workspace_ids or actor.household_id in team.allowed_workspace_ids
        )
        agent_ids = {agent_id for team in teams for agent_id in team.agent_ids}
        return {
            "version": self.manifest.version,
            "configured": bool(teams),
            "max_parallel": self.manifest.max_parallel,
            "teams": [team.model_dump(mode="json") for team in teams],
            "agents": [
                agent.model_dump(mode="json") for agent in self.manifest.agents
                if agent.id in agent_ids
            ],
            "contexts": [
                context.model_dump(mode="json") for context in self.manifest.contexts
                if context.workspace_id == actor.household_id
                and (not context.actor_ids or actor.actor_id in context.actor_ids)
            ],
            "models": [
                {
                    "id": item.id, "model": item.model, "provider": item.provider,
                    "local": item.local, "tier": item.tier,
                    "capabilities": sorted(item.capabilities), "enabled": item.enabled,
                }
                for item in self.manifest.models
            ],
            "environments": [
                {
                    "id": item.id, "kind": item.kind, "os": item.os,
                    "capabilities": sorted(item.capabilities), "enabled": item.enabled,
                    "max_concurrency": item.max_concurrency,
                }
                for item in self.manifest.environments
            ],
            "tools": [
                {
                    "id": tool.id, "categories": sorted(tool.categories),
                    "capabilities": sorted(tool.capabilities), "transport": tool.transport,
                    "side_effect": tool.side_effect,
                }
                for tool in self.tools.discover(scopes=actor.scopes)
            ],
            "tool_templates": [tool.model_dump(mode="json") for tool in builtin_tool_templates()],
            "execution_enabled": False,
        }

    def _task(
        self, actor: ActorContext, plan_id: UUID, task: AgentTaskSpec, profile: AgentProfile
    ) -> PlannedAgentTask:
        blocked: list[str] = []
        render_agent_prompt(profile, task, {key: "" for key in task.depends_on})
        task_id = uuid5(plan_id, task.id)
        attempt_id = uuid5(task_id, "initial-attempt")
        selected_tools = task.tool_ids if task.tool_ids is not None else profile.tool_ids
        if not set(selected_tools) <= set(profile.tool_ids):
            raise AuthorizationError("Task tools exceed the selected agent's grant")
        if task.environment_id and task.environment_id not in profile.environment_ids:
            raise AuthorizationError("Task environment exceeds the selected agent's grant")
        if profile.privacy == "local_only" and task.privacy == "allow_cloud":
            raise AuthorizationError("Task cannot widen the agent's local-only policy")
        definitions = {tool.id: tool for tool in self.manifest.tools}
        if any(
            definitions[key].credential_env
            and not self._environ.get(definitions[key].credential_env or "", "").strip()
            for key in selected_tools
        ):
            blocked.append("A selected tool is missing its configured credential")
        needed = frozenset(
            requirement for tool_id in selected_tools
            for requirement in definitions[tool_id].environment_capabilities
        )
        environment = None
        choices = (task.environment_id,) if task.environment_id else profile.environment_ids
        for identifier in choices:
            definition = next(
                item for item in self.manifest.environments if item.id == identifier
            )
            if definition.credential_env and not self._environ.get(
                definition.credential_env, ""
            ).strip():
                continue
            try:
                environment = self.environments.plan(
                    EnvironmentRequest(
                        workspace_id=actor.household_id, agent_id=profile.id,
                        task_id=task_id, attempt_id=attempt_id, capabilities=needed,
                    ),
                    environment_id=identifier,
                )
                break
            except ExecutionError:
                continue
        if choices and environment is None:
            blocked.append("No permitted execution environment satisfies this task")
        if needed and environment is None and not choices:
            blocked.append("The selected tools require a configured execution environment")
        environment_capabilities = next(
            (item.capabilities for item in self.manifest.environments
             if environment is not None and item.id == environment.environment_id),
            frozenset(),
        )
        try:
            self.tools.resolve(
                selected_tools, scopes=actor.scopes & profile.tool_scopes,
                environment_capabilities=environment_capabilities,
            )
        except (ToolCatalogError, AuthorizationError) as error:
            blocked.append(str(error))
        required_model = profile.model_capabilities | task.model_capabilities
        if selected_tools:
            required_model |= {"tools"}
        model = None
        try:
            model = self.models.route(
                RoutingRequest(
                    depth=task.depth if task.depth is not None else profile.depth,
                    importance=(
                        task.importance if task.importance is not None else profile.importance
                    ),
                    required_capabilities=required_model,
                    privacy=task.privacy or profile.privacy,
                    model_override=task.model_override or profile.model_override,
                    input_tokens=task.input_tokens,
                    output_tokens=min(task.output_tokens, profile.max_output_tokens),
                    budget_usd=task.budget_usd,
                )
            )
        except ModelRoutingError as error:
            blocked.append(str(error))
        return PlannedAgentTask(
            id=task.id, agent_id=profile.id, task_id=task_id, attempt_id=attempt_id,
            objective=task.objective, depends_on=task.depends_on,
            tool_ids=tuple(selected_tools), model=model, environment=environment,
            blocked_reasons=tuple(blocked),
        )

    def plan(self, actor: ActorContext, request: PlanTeamRequest) -> AgentTeamPlan:
        self.authorize(actor, write=True)
        self._context(actor, request.context_id)
        team = next((team for team in self.manifest.teams if team.id == request.team_id), None)
        if team is None or (
            team.allowed_workspace_ids and actor.household_id not in team.allowed_workspace_ids
        ):
            raise NotFoundError("Team template not found")
        if not {task.agent_id for task in request.tasks} <= set(team.agent_ids):
            raise AuthorizationError("Task agent is not part of the selected team")
        identifier = uuid5(
            actor.actor_id, f"{actor.household_id}:platform:{request.idempotency_key}"
        )
        fingerprint = digest(stable_configuration(request.model_dump(mode="python")))
        existing = self.store.get_job(identifier)
        if existing is not None:
            if existing.input_digest != fingerprint:
                raise IdempotencyConflictError("Idempotency key was used for a different plan")
            return self.get(actor, identifier)
        limit = min(
            self.manifest.max_parallel, team.max_parallel,
            request.max_parallel or self.manifest.max_parallel,
        )
        profiles = {profile.id: profile for profile in self.manifest.agents}
        tasks = tuple(
            self._task(actor, identifier, task, profiles[task.agent_id]) for task in request.tasks
        )
        waves = self._waves(tasks, limit)
        plan = AgentTeamPlan(
            id=identifier, workspace_id=actor.household_id, actor_id=actor.actor_id,
            team_id=team.id, team_version=team.version, context_id=request.context_id,
            manifest_digest=digest(stable_configuration(self.manifest.model_dump(mode="python"))),
            max_parallel=limit,
            state="blocked" if any(t.blocked_reasons for t in tasks) else "planned",
            tasks=tasks, waves=waves,
        )
        job = Job(
            id=identifier, household_id=actor.household_id, created_by=actor.actor_id,
            kind=PLAN_KIND,
            idempotency_key=digest({
                "actor": str(actor.actor_id), "key": request.idempotency_key,
            }),
            input={
                "request": request.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "configuration": {
                    "team": team.model_dump(mode="json"),
                    "agents": [profiles[key].model_dump(mode="json")
                               for key in sorted({task.agent_id for task in tasks})],
                    "models": [item.model_dump(mode="json") for item in self.manifest.models
                               if item.id in {task.model.endpoint_id for task in tasks
                                              if task.model is not None}],
                    "environments": [
                        item.model_dump(mode="json") for item in self.manifest.environments
                        if item.id in {task.environment.environment_id for task in tasks
                                       if task.environment is not None}
                    ],
                    "tools": [item.model_dump(mode="json") for item in self.manifest.tools
                              if item.id in {key for task in tasks for key in task.tool_ids}],
                },
            },
            input_digest=fingerprint, status=JobStatus.WAITING,
        )
        with self.store.transaction(actor.household_id):
            saved, created = self.store.create_job(job)
            if created:
                self.audit.record(
                    event_type="platform.plan.created", actor=actor,
                    resource_type="platform.plan", resource_id=str(identifier),
                    payload={"team_id": team.id, "state": plan.state, "tasks": len(tasks)},
                )
        return AgentTeamPlan.model_validate(saved.input["plan"])

    def _waves(
        self, tasks: tuple[PlannedAgentTask, ...], limit: int
    ) -> tuple[tuple[str, ...], ...]:
        remaining = {task.id: task for task in tasks}
        done: set[str] = set()
        waves: list[tuple[str, ...]] = []
        capacities = {item.id: item.max_concurrency for item in self.manifest.environments}
        while remaining:
            ready: list[str] = []
            resources: dict[str, int] = {}
            for name, task in remaining.items():
                if task.blocked_reasons or not set(task.depends_on) <= done:
                    continue
                environment_id = task.environment.environment_id if task.environment else None
                if environment_id:
                    if resources.get(environment_id, 0) >= capacities[environment_id]:
                        continue
                    resources[environment_id] = resources.get(environment_id, 0) + 1
                ready.append(name)
                if len(ready) == limit:
                    break
            if not ready:
                break
            waves.append(tuple(ready))
            done.update(ready)
            remaining = {name: task for name, task in remaining.items() if name not in done}
        return tuple(waves)

    def get(self, actor: ActorContext, identifier: UUID) -> AgentTeamPlan:
        self.authorize(actor)
        job = self.store.get_job(identifier)
        if (
            job is None or job.kind != PLAN_KIND
            or (job.household_id, job.created_by) != (actor.household_id, actor.actor_id)
        ):
            raise NotFoundError("Agent plan not found")
        try:
            plan = AgentTeamPlan.model_validate(job.input["plan"])
        except (KeyError, PydanticError) as error:
            raise ValidationError("Stored agent plan is invalid") from error
        self._context(actor, plan.context_id)
        return plan

    def list(self, actor: ActorContext) -> tuple[AgentTeamPlan, ...]:
        self.authorize(actor)
        visible = []
        for job in self.store.jobs(actor.household_id, actor.actor_id, PLAN_KIND, 0, 100):
            try:
                visible.append(self.get(actor, job.id))
            except NotFoundError:
                continue
        return tuple(visible)
