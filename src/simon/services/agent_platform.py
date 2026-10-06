"""Compile reusable teams into durable, reviewable plans without executing them."""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from dotenv import dotenv_values
from pydantic import ValidationError as PydanticError

from simon.adapters.execution_backends import DockerBackend, MachineBackend
from simon.adapters.tool_preflight import integration_status, uses_network
from simon.adapters.ui_tool_binding import with_ui_tools
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    AgentTeamPlan,
    PlannedAgentTask,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.execution import EnvironmentDefinition, EnvironmentRequest, ExecutionError
from simon.domain.model_routing import RoutingRequest
from simon.domain.models import ActorContext, Job, JobStatus
from simon.domain.ports import Store
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition, ToolExecutionContext
from simon.services.agent_profiles import AgentProfileService
from simon.services.agent_prompts import render_agent_prompt
from simon.services.audit import AuditService
from simon.services.canonical import canonical_json, digest
from simon.services.execution import EnvironmentManager
from simon.services.integrations import IntegrationService
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
        tool_availability: Callable[[ActorContext, str], dict[str, Any]] | None = None,
        integrations: IntegrationService | None = None,
    ) -> None:
        self.integrations = integrations
        if integrations is not None:
            manifest = with_ui_tools(manifest)
        self.store, self.manifest = store, manifest
        self.shared_tools = integrations is not None
        self.agent_profiles = AgentProfileService(store, manifest, shared_tools=self.shared_tools)
        self.state_dir = state_dir
        self.audit = AuditService(store)
        self.available_transports = frozenset(available_transports)
        self.tool_availability = tool_availability
        self.project_tool_availability: (
            Callable[[ActorContext, UUID, str], dict[str, Any]] | None
        ) = None
        self.project_team_resolver: Callable[[ActorContext, UUID], TeamTemplate] | None = None
        self.project_visibility_resolver: Callable[[ActorContext, UUID], Any] | None = None
        self.project_profile_resolver: (
            Callable[[ActorContext, UUID], dict[str, AgentProfile | None]] | None
        ) = None
        self.project_tool_filter_factory: (
            Callable[[ActorContext, UUID], Callable[[ToolDefinition], bool]] | None
        ) = None
        self._environ = (
            integrations.credentials
            if integrations is not None and environ is None
            else platform_credentials()
            if environ is None
            else environ
        )
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
            and context.workspace_id == actor.workspace_id
            and (not context.actor_ids or actor.actor_id in context.actor_ids)
            for context in self.manifest.contexts
        ):
            raise NotFoundError("Work context not found")

    def tool_statuses(
        self, actor: ActorContext, project_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        """Configuration preflight only: never contact a provider while opening the UI."""
        result = []
        project_filter = (
            self.project_tool_filter_factory(actor, project_id)
            if project_id and self.project_tool_filter_factory
            else None
        )
        for tool in self.manifest.tools:
            reasons = []
            state = "configured"
            if project_filter and not project_filter(tool):
                result.append(
                    {
                        "id": tool.id,
                        "state": "unavailable",
                        "blocked_reasons": [],
                        "description": tool.description,
                        "categories": sorted(tool.categories),
                        "capabilities": sorted(tool.capabilities),
                        "transport": tool.transport,
                        "side_effect": tool.side_effect,
                    }
                )
                continue
            if self.integrations and tool.settings.get("ui_managed") is True:
                try:
                    tool = self.integrations.bind_tool(tool, actor)
                except ToolCatalogError as error:
                    result.append(
                        {
                            "id": tool.id,
                            "description": tool.description,
                            "categories": sorted(tool.categories),
                            "capabilities": sorted(tool.capabilities),
                            "transport": tool.transport,
                            "side_effect": tool.side_effect,
                            "state": "unconfigured",
                            "blocked_reasons": [str(error)],
                        }
                    )
                    continue
            if not tool.enabled:
                state = "disabled"
                reasons.append("Disabled by the server configuration")
            elif not tool.configured:
                state = "unconfigured"
                reasons.append("Integration setup is incomplete")
            elif not tool.required_scopes <= actor.scopes:
                state = "permission_required"
                reasons.append("Your account does not have the required permission")
            elif tool.transport not in self.available_transports:
                state = "unavailable"
                reasons.append("No execution handler is installed for this transport")
            elif tool.credential_env and not self._environ.get(tool.credential_env, "").strip():
                state = "unavailable"
                reasons.append("Server credential is missing")
            elif self.tool_availability is not None and tool.transport in {
                "native",
                "external_actions",
                "project_boards",
            }:
                availability = self.tool_availability(actor, tool.id)
                if not availability["available"]:
                    state = "unavailable"
                    reasons.append(availability["reason"])
            else:
                state, optional_reasons = integration_status(tool, actor, self._environ)
                reasons.extend(optional_reasons)
            result.append(
                {
                    "id": tool.id,
                    "description": tool.description,
                    "categories": sorted(tool.categories),
                    "capabilities": sorted(tool.capabilities),
                    "transport": tool.transport,
                    "side_effect": tool.side_effect,
                    "state": state,
                    "blocked_reasons": reasons,
                }
            )
        return result

    def resolve_connected_tool(
        self, definition: ToolDefinition, context: ToolExecutionContext
    ) -> ToolDefinition:
        if self.integrations is None:
            return definition
        from simon.domain.models import Channel

        actor = ActorContext(
            actor_id=context.actor_id,
            workspace_id=context.workspace_id,
            scopes=context.scopes,
            channel=Channel.WORKER,
        )
        return self.integrations.bind_tool(definition, actor)

    def test_environment(self, actor: ActorContext, identifier: str) -> dict[str, Any]:
        """Check a runtime without allocating a workspace or running project commands."""
        self.authorize(actor)
        environment = next(
            (env for env in self.manifest.environments if env.id == identifier), None
        )
        if environment is None:
            raise NotFoundError("Execution runtime not found")
        if not environment.enabled:
            return {"status": "failed", "message": "Runtime is disabled."}
        if environment.kind != "docker":
            return {
                "status": "configuration_only",
                "message": "Machine runner configured; a read-only health check is not available.",
            }
        import subprocess

        try:
            checked = subprocess.run(
                ["docker", "image", "inspect", str(environment.container_image)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if checked.returncode == 0:
                return {
                    "status": "passed",
                    "message": "Docker is reachable and this runtime image is installed.",
                }
        except (OSError, subprocess.SubprocessError):
            pass
        return {
            "status": "failed",
            "message": "Start Docker and install this runtime image, then test again.",
        }

    def profiles(
        self,
        actor: ActorContext,
        project_id: UUID | None = None,
    ) -> tuple[AgentProfile, ...]:
        """Resolve current owner-scoped profiles without changing the shared manifest."""
        self.authorize(actor)
        stock_ids = {profile.id for profile in self.manifest.agents}
        # list() revalidates saved grants. Block both sides of an ID collision so
        # an existing project team cannot silently substitute an operator profile
        # for a previously selected custom role, even if that stock role is visible.
        records = self.agent_profiles.list(actor)
        saved_ids = {record.id for record in records}
        stock = tuple(profile for profile in self.manifest.agents if profile.id not in saved_ids)
        custom = tuple(
            record.profile
            for record in records
            if record.state == "configured" and record.id not in stock_ids
        )
        profiles = (*stock, *custom)
        if project_id is not None and self.project_profile_resolver is not None:
            members = self.project_profile_resolver(actor, project_id)
            profiles = (
                *(profile for profile in profiles if profile.id not in members),
                *(profile for profile in members.values() if profile is not None),
            )
        if self.shared_tools:
            profiles = tuple(self.shared_profile(actor, profile) for profile in profiles)
        return profiles

    def shared_profile(self, actor: ActorContext, profile: AgentProfile) -> AgentProfile:
        return profile.model_copy(
            update={
                "tool_access": "shared",
                "tool_ids": tuple(
                    tool.id for tool in self.manifest.tools if tool.required_scopes <= actor.scopes
                ),
                "tool_scopes": actor.scopes,
                "environment_ids": tuple(
                    env.id for env in self.manifest.environments if env.enabled
                ),
                "max_action": "write",
                "max_tool_calls": max(20, profile.max_tool_calls),
                "model_capabilities": profile.model_capabilities,
            }
        )

    def task_tool_selection(
        self,
        actor: ActorContext,
        profile: AgentProfile,
        task: AgentTaskSpec,
        project_id: UUID | None,
    ) -> tuple[tuple[str, ...], str | None]:
        """Roles share tools; the scheduler chooses a compatible runtime for each task."""
        statuses = {item["id"]: item["state"] for item in self.tool_statuses(actor, project_id)}
        candidates = [
            tool
            for tool in self.manifest.tools
            if tool.id in profile.tool_ids
            and statuses.get(tool.id) == "configured"
            and tool.action_policy != "external_commitment"
            and (profile.privacy != "local_only" or not uses_network(tool))
            and (
                project_id is not None
                or tool.transport
                not in {
                    "project_work",
                    "project_outputs",
                    "project_journal",
                    "project_boards",
                    "project_storage",
                }
            )
        ]
        text = (task.objective + " " + profile.description).lower()
        keywords = {
            "cad": ("3d", "cad", "stl", "model a", "solid", "freecad"),
            "pcb": ("pcb", "circuit", "kicad", "gerber"),
            "processing": (
                "pdf",
                "document",
                "report",
                "spreadsheet",
                "image",
                "video",
                "audio",
                "ocr",
            ),
            "browser": ("web", "research", "https://", "screenshot", "website", "browse"),
            "git": ("code", "repository", "software", "program", "script", "git"),
        }

        def compatible(tool: ToolDefinition, environment: EnvironmentDefinition) -> bool:
            if not tool.environment_capabilities:
                return True
            if not tool.environment_capabilities <= environment.capabilities:
                return False
            if tool.transport == "browser":
                return environment.network == (
                    "none" if tool.id == "browser.render_html" else "bridge"
                )
            return tool.transport not in {"cad", "pcb"} or environment.network == "none"

        choices = []
        for environment in self.manifest.environments:
            if not environment.enabled or environment.id not in profile.environment_ids:
                continue
            if task.environment_id and environment.id != task.environment_id:
                continue
            supported = [tool for tool in candidates if compatible(tool, environment)]
            score = sum(
                1
                for transport, words in keywords.items()
                if any(word in text for word in words)
                and any(
                    tool.transport == transport and tool.environment_capabilities
                    for tool in supported
                )
            )
            if score or task.environment_id:
                choices.append((score, len(supported), environment.id, supported))
        if choices:
            _, _, identifier, supported = max(choices, key=lambda item: (item[0], item[1], item[2]))
            return tuple(tool.id for tool in supported), identifier
        return tuple(tool.id for tool in candidates if not tool.environment_capabilities), None

    def teams(self, actor: ActorContext) -> tuple[TeamTemplate, ...]:
        self.authorize(actor)
        visible = tuple(
            team
            for team in self.manifest.teams
            if not team.allowed_workspace_ids or actor.workspace_id in team.allowed_workspace_ids
        )
        custom = self.agent_profiles.profiles(actor)
        if custom:
            occupied = {team.id for team in self.manifest.teams}
            identifier = "my-custom-agents"
            suffix = 1
            while identifier in occupied:
                identifier = f"my-custom-agents-{suffix}"
                suffix += 1
            visible += (
                TeamTemplate(
                    id=identifier,
                    name="My agents",
                    agent_ids=tuple(item.id for item in custom),
                    max_parallel=self.manifest.max_parallel,
                    allowed_workspace_ids=frozenset({actor.workspace_id}),
                ),
            )
        return visible

    def plan_profiles(self, actor: ActorContext, plan: AgentTeamPlan) -> dict[str, AgentProfile]:
        """Fence execution to the reviewed profiles, including durable custom versions."""
        self.authorize(actor)
        if (plan.actor_id, plan.workspace_id) != (actor.actor_id, actor.workspace_id):
            raise NotFoundError("Agent plan not found")
        job = self.store.get_job(plan.id)
        if job is None or job.kind != PLAN_KIND:
            raise NotFoundError("Agent plan not found")
        try:
            saved = {
                profile.id: profile
                for value in job.input["configuration"]["agents"]
                for profile in (AgentProfile.model_validate(value),)
            }
        except (KeyError, TypeError, PydanticError) as error:
            raise ValidationError("Stored agent profile snapshot is invalid") from error
        current = {profile.id: profile for profile in self.profiles(actor, plan.project_id)}
        required = {task.agent_id for task in plan.tasks}
        if required != set(saved) or any(
            identifier not in current
            or (
                not self.shared_tools
                and stable_configuration(current[identifier].model_dump(mode="python"))
                != stable_configuration(saved[identifier].model_dump(mode="python"))
            )
            for identifier in required
        ):
            raise InvalidTransitionError(
                "Agent profile changed or is unavailable; create a new plan"
            )
        if self.shared_tools:
            return {key: self.shared_profile(actor, profile) for key, profile in saved.items()}
        return saved

    def catalog(self, actor: ActorContext) -> dict[str, Any]:
        self.authorize(actor)
        teams = self.teams(actor)
        agent_ids = {agent_id for team in teams for agent_id in team.agent_ids}
        tool_statuses = self.tool_statuses(actor)
        return {
            "version": self.manifest.version,
            "configured": bool(teams),
            "max_parallel": self.manifest.max_parallel,
            "teams": [team.model_dump(mode="json") for team in teams],
            "agents": [
                agent.model_dump(mode="json")
                for agent in self.profiles(actor)
                if agent.id in agent_ids
            ],
            "custom_agents": [
                record.model_dump(mode="json") for record in self.agent_profiles.list(actor)
            ],
            "skills": self.agent_profiles.skills(actor, tool_statuses),
            "individual_skills": self.agent_profiles.individual_skills(actor, tool_statuses),
            "contexts": [
                context.model_dump(mode="json")
                for context in self.manifest.contexts
                if context.workspace_id == actor.workspace_id
                and (not context.actor_ids or actor.actor_id in context.actor_ids)
            ],
            "models": [
                {
                    "id": item.id,
                    "model": item.model,
                    "provider": item.provider,
                    "local": item.local,
                    "tier": item.tier,
                    "budget_configured": item.local
                    or (
                        item.input_cost_per_million_usd is not None
                        and item.output_cost_per_million_usd is not None
                    ),
                    "capabilities": sorted(item.capabilities),
                    "enabled": item.enabled,
                    "state": (
                        "disabled"
                        if not item.enabled
                        else "unavailable"
                        if item.api_key_env and not self._environ.get(item.api_key_env, "").strip()
                        else "configured"
                    ),
                    "blocked_reasons": (
                        ["Disabled by the server configuration"]
                        if not item.enabled
                        else ["Server credential is missing"]
                        if item.api_key_env and not self._environ.get(item.api_key_env, "").strip()
                        else []
                    ),
                }
                for item in self.manifest.models
            ],
            "environments": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "os": item.os,
                    "capabilities": sorted(item.capabilities),
                    "enabled": item.enabled,
                    "max_concurrency": item.max_concurrency,
                }
                for item in self.manifest.environments
            ],
            "tools": [
                {
                    "id": tool.id,
                    "categories": sorted(tool.categories),
                    "capabilities": sorted(tool.capabilities),
                    "transport": tool.transport,
                    "side_effect": tool.side_effect,
                }
                for tool in self.tools.discover(scopes=actor.scopes)
            ],
            "tool_templates": [tool.model_dump(mode="json") for tool in builtin_tool_templates()],
            "tool_statuses": tool_statuses,
            "execution_enabled": False,
        }

    def _task(
        self,
        actor: ActorContext,
        plan_id: UUID,
        task: AgentTaskSpec,
        profile: AgentProfile,
        project_id: UUID | None = None,
    ) -> PlannedAgentTask:
        blocked: list[str] = []
        render_agent_prompt(profile, task, dict.fromkeys(task.depends_on, ""))
        task_id = uuid5(plan_id, task.id)
        attempt_id = uuid5(task_id, "initial-attempt")
        selected_tools = task.tool_ids if task.tool_ids is not None else profile.tool_ids
        if profile.tool_access == "shared" and task.tool_ids is not None:
            authorized = {
                tool.id for tool in self.manifest.tools if tool.required_scopes <= actor.scopes
            }
            if not set(selected_tools) <= authorized:
                raise AuthorizationError("Task tools exceed the account's grant")
            ready = {
                item["id"]
                for item in self.tool_statuses(actor, project_id)
                if item["state"] == "configured"
            }
            selected_tools = tuple(key for key in selected_tools if key in ready)
        automatic_environment = None
        if profile.tool_access == "shared" and task.tool_ids is None:
            selected_tools, automatic_environment = self.task_tool_selection(
                actor, profile, task, project_id
            )
        if not set(selected_tools) <= set(profile.tool_ids):
            raise AuthorizationError("Task tools exceed the selected agent's grant")
        if task.environment_id and task.environment_id not in profile.environment_ids:
            raise AuthorizationError("Task environment exceeds the selected agent's grant")
        if profile.privacy == "local_only" and task.privacy == "allow_cloud":
            raise AuthorizationError("Task cannot widen the agent's local-only policy")
        definitions = {tool.id: tool for tool in self.manifest.tools}
        project_output_tools = {
            key
            for key, tool in definitions.items()
            if tool.transport in {"project_outputs", "project_journal"}
            or key == "workspace.import_artifact"
        }
        if project_id is None and task.tool_ids is None:
            # Optional project context is absent from standalone runs. Do not make
            # otherwise useful document/developer presets depend on a project.
            selected_tools = tuple(key for key in selected_tools if key not in project_output_tools)
        for key in selected_tools:
            tool = definitions[key]
            if key in project_output_tools and project_id is None:
                blocked.append("Project output tools require a project-assigned plan")
            if tool.transport == "project_work" and project_id is None:
                blocked.append("Project reporting tools require a project-assigned plan")
            if tool.transport == "project_storage" and project_id is None:
                blocked.append("Project storage tools require a project-assigned plan")
            if tool.transport == "project_boards":
                if project_id is None:
                    blocked.append("ClickUp tools require a project-assigned plan")
                elif self.project_tool_availability is not None:
                    availability = self.project_tool_availability(actor, project_id, key)
                    if not availability["available"]:
                        blocked.append(f"{key}: {availability['reason']}")
            try:
                bound = self.integrations.bind_tool(tool, actor) if self.integrations else tool
                state, reasons = integration_status(bound, actor, self._environ)
            except ToolCatalogError as error:
                state, reasons = "unconfigured", (str(error),)
            if state != "configured":
                blocked.extend(f"{key}: {reason}" for reason in reasons)
            if tool.action_policy == "external_commitment" or (
                tool.side_effect and profile.max_action != "write"
            ):
                blocked.append(f"The selected agent cannot authorize this tool's action: {key}")
            if self.tool_availability is not None and tool.transport in {
                "native",
                "external_actions",
                "project_boards",
            }:
                availability = self.tool_availability(actor, key)
                if not availability["available"]:
                    blocked.append(f"{key}: {availability['reason']}")
        if (task.privacy or profile.privacy) == "local_only" and any(
            uses_network(definitions[key]) for key in selected_tools
        ):
            blocked.append("Local-only tasks cannot use network-connected tools")
        if any(
            definitions[key].credential_env
            and not self._environ.get(definitions[key].credential_env or "", "").strip()
            for key in selected_tools
        ):
            blocked.append("A selected tool is missing its configured credential")
        needed = frozenset(
            requirement
            for tool_id in selected_tools
            for requirement in definitions[tool_id].environment_capabilities
        )
        environment = None
        # Explicit tool subsets can run without the profile's optional containers.
        # Planning and connected-file reads should not acquire a browser workspace
        # merely because that agent also has browser skills. An explicit environment
        # or the profile's default task configuration still retains its requirements.
        choices = (
            (task.environment_id,)
            if task.environment_id
            else (automatic_environment,)
            if automatic_environment
            else profile.environment_ids
            if needed or task.tool_ids is None
            else ()
        )
        if profile.tool_access == "shared" and task.tool_ids is None and not automatic_environment:
            choices = ()
        browser_networks = {
            "none" if key == "browser.render_html" else "bridge"
            for key in selected_tools
            if definitions[key].transport == "browser"
        }
        for identifier in choices:
            definition = next(item for item in self.manifest.environments if item.id == identifier)
            # Browser environments share capability names but have deliberately
            # different network policies. Choose a compatible existing grant,
            # rather than selecting the first match and rejecting it afterward.
            # Explicit environment choices still receive their normal blockers.
            if (
                task.environment_id is None
                and browser_networks
                and (
                    len(browser_networks) != 1
                    or definition.network not in browser_networks
                    or definition.kind != "docker"
                    or definition.os != "linux"
                )
            ):
                continue
            if (
                definition.credential_env
                and not self._environ.get(definition.credential_env, "").strip()
            ):
                continue
            try:
                environment = self.environments.plan(
                    EnvironmentRequest(
                        workspace_id=actor.workspace_id,
                        agent_id=profile.id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        capabilities=needed,
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
        if (
            (task.privacy or profile.privacy) == "local_only"
            and environment is not None
            and environment.network != "none"
        ):
            blocked.append("Local-only tasks require an environment with networking disabled")
        if environment is not None:
            for key in selected_tools:
                tool = definitions[key]
                if tool.transport in {"cad", "pcb"}:
                    definition = self.environments.definitions[environment.environment_id]
                    if (
                        environment.network != "none"
                        or definition.kind != "docker"
                        or definition.os != "linux"
                    ):
                        blocked.append(f"{key} requires an offline Linux Docker environment")
                if tool.transport == "browser":
                    definition = self.environments.definitions[environment.environment_id]
                    if definition.kind != "docker" or definition.os != "linux":
                        blocked.append(f"{key} requires a Linux Docker browser environment")
                    network = "none" if tool.id == "browser.render_html" else "bridge"
                    if environment.network != network:
                        blocked.append(
                            f"{key} requires a browser environment with {network} networking"
                        )
        environment_capabilities = next(
            (
                item.capabilities
                for item in self.manifest.environments
                if environment is not None and item.id == environment.environment_id
            ),
            frozenset(),
        )
        try:
            self.tools.resolve(
                selected_tools,
                scopes=actor.scopes & profile.tool_scopes,
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
            id=task.id,
            agent_id=profile.id,
            task_id=task_id,
            attempt_id=attempt_id,
            objective=task.objective,
            depends_on=task.depends_on,
            tool_ids=tuple(selected_tools),
            model=model,
            environment=environment,
            blocked_reasons=tuple(blocked),
        )

    def resolve_team(
        self,
        actor: ActorContext,
        team_id: str,
        project_id: UUID | None = None,
    ) -> TeamTemplate:
        if project_id is not None:
            if self.project_team_resolver is None:
                raise NotFoundError("Project team is unavailable")
            project_team = self.project_team_resolver(actor, project_id)
            if project_team.id != team_id:
                raise NotFoundError("Project team not found")
            return project_team
        team = next((team for team in self.teams(actor) if team.id == team_id), None)
        if team is None or (
            team.allowed_workspace_ids and actor.workspace_id not in team.allowed_workspace_ids
        ):
            raise NotFoundError("Team template not found")
        return team

    def plan(self, actor: ActorContext, request: PlanTeamRequest) -> AgentTeamPlan:
        self.authorize(actor, write=True)
        self._context(actor, request.context_id)
        team = self.resolve_team(actor, request.team_id, request.project_id)
        if not {task.agent_id for task in request.tasks} <= set(team.agent_ids):
            raise AuthorizationError("Task agent is not part of the selected team")
        identifier = uuid5(
            actor.actor_id, f"{actor.workspace_id}:platform:{request.idempotency_key}"
        )
        fingerprint = digest(stable_configuration(request.model_dump(mode="python")))
        existing = self.store.get_job(identifier)
        if existing is not None:
            if existing.input_digest != fingerprint:
                raise IdempotencyConflictError("Idempotency key was used for a different plan")
            return self.get(actor, identifier)
        limit = min(
            self.manifest.max_parallel,
            team.max_parallel,
            request.max_parallel or self.manifest.max_parallel,
        )
        profiles = {profile.id: profile for profile in self.profiles(actor, request.project_id)}
        if not {task.agent_id for task in request.tasks} <= profiles.keys():
            raise ValidationError("A selected agent is unavailable; review the team")
        tasks = tuple(
            self._task(actor, identifier, task, profiles[task.agent_id], request.project_id)
            for task in request.tasks
        )
        waves = self._waves(tasks, limit)
        plan = AgentTeamPlan(
            id=identifier,
            workspace_id=actor.workspace_id,
            actor_id=actor.actor_id,
            team_id=team.id,
            team_version=team.version,
            context_id=request.context_id,
            project_id=request.project_id,
            manifest_digest=digest(stable_configuration(self.manifest.model_dump(mode="python"))),
            max_parallel=limit,
            state="blocked" if any(t.blocked_reasons for t in tasks) else "planned",
            tasks=tasks,
            waves=waves,
        )
        job = Job(
            id=identifier,
            workspace_id=actor.workspace_id,
            created_by=actor.actor_id,
            kind=PLAN_KIND,
            idempotency_key=digest(
                {
                    "actor": str(actor.actor_id),
                    "key": request.idempotency_key,
                }
            ),
            input={
                "request": request.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "configuration": {
                    "team": team.model_dump(mode="json"),
                    "agents": [
                        profiles[key].model_dump(mode="json")
                        for key in sorted({task.agent_id for task in tasks})
                    ],
                    "models": [
                        item.model_dump(mode="json")
                        for item in self.manifest.models
                        if item.id
                        in {task.model.endpoint_id for task in tasks if task.model is not None}
                    ],
                    "environments": [
                        item.model_dump(mode="json")
                        for item in self.manifest.environments
                        if item.id
                        in {
                            task.environment.environment_id
                            for task in tasks
                            if task.environment is not None
                        }
                    ],
                    "tools": [
                        item.model_dump(mode="json")
                        for item in self.manifest.tools
                        if item.id in {key for task in tasks for key in task.tool_ids}
                    ],
                },
            },
            input_digest=fingerprint,
            status=JobStatus.WAITING,
        )
        with self.store.transaction(actor.workspace_id):
            saved, created = self.store.create_job(job)
            if created:
                self.audit.record(
                    event_type="platform.plan.created",
                    actor=actor,
                    resource_type="platform.plan",
                    resource_id=str(identifier),
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
            job is None
            or job.kind != PLAN_KIND
            or (job.workspace_id, job.created_by) != (actor.workspace_id, actor.actor_id)
        ):
            raise NotFoundError("Agent plan not found")
        try:
            plan = AgentTeamPlan.model_validate(job.input["plan"])
        except (KeyError, PydanticError) as error:
            raise ValidationError("Stored agent plan is invalid") from error
        self._context(actor, plan.context_id)
        if plan.project_id is not None:
            if self.project_visibility_resolver is not None:
                self.project_visibility_resolver(actor, plan.project_id)
            else:
                self.resolve_team(actor, plan.team_id, plan.project_id)
        return plan

    def list(self, actor: ActorContext) -> tuple[AgentTeamPlan, ...]:
        self.authorize(actor)
        visible = []
        for job in self.store.jobs(actor.workspace_id, actor.actor_id, PLAN_KIND, 0, 100):
            try:
                visible.append(self.get(actor, job.id))
            except NotFoundError:
                continue
        return tuple(visible)
