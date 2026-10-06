"""Agent file operations bound to the assigned project's chosen storage roots."""

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.project_storage import ProjectStorageRead, ProjectStorageWrite
from simon.domain.tool_catalog import ToolDefinition, ToolExecutionContext
from simon.services.agent_runs import AgentRunService
from simon.services.project_storage import ProjectStorageService


def project_storage_definitions() -> tuple[ToolDefinition, ...]:
    return tuple(
        ToolDefinition(
            id="project.storage_" + operation,
            transport="project_storage",
            description=description,
            categories=frozenset({"project", "files", "storage"}),
            capabilities=frozenset({"project.storage." + operation}),
            configured=True,
            settings={"network": operation != "locations"},
            required_scopes=frozenset(
                {"jobs:write" if operation == "write" else "jobs:read", "memories:read"}
            ),
            side_effect=operation == "write",
            action_policy="write" if operation == "write" else "read",
            input_schema=schema.model_json_schema()
            if schema
            else {"type": "object", "additionalProperties": False},
        )
        for operation, description, schema in (
            (
                "locations",
                "List available file locations selected for this project and their supported "
                "operation schemas. Use only these locations for project files.",
                None,
            ),
            (
                "read",
                "Read or browse a chosen project file location. First call storage_locations "
                "for location IDs and provider operation schemas.",
                ProjectStorageRead,
            ),
            (
                "write",
                "Write project files in a selected writable location. First call storage_locations "
                "for location IDs and provider operation schemas. Existing-file edits require "
                "their current revision.",
                ProjectStorageWrite,
            ),
        )
    )


def project_storage_transport_factory(
    base: Callable[..., Mapping[str, Any]],
    storage: ProjectStorageService,
    runs: AgentRunService,
) -> Callable[..., dict[str, Any]]:
    def factory(
        actor: ActorContext, run_id: UUID, revalidate: Callable[[], ActorContext], lease: Any = None
    ) -> dict[str, Any]:
        handlers = dict(base(actor, run_id, revalidate, lease))

        def execute(
            definition: ToolDefinition, arguments: dict[str, Any], context: ToolExecutionContext
        ) -> dict[str, Any]:
            current = revalidate()
            if (context.actor_id, context.workspace_id, context.run_id) != (
                actor.actor_id,
                actor.workspace_id,
                run_id,
            ):
                raise AuthorizationError("Project storage execution owner changed")
            run = runs.get(current, run_id)
            plan = runs.platform.get(current, run.plan_id)
            if plan.project_id is None:
                raise AuthorizationError("Project file operations require an assigned project")
            if definition.id == "project.storage_locations":
                return storage.agent_locations(current, plan.project_id)
            write = definition.id == "project.storage_write"
            if definition.id not in {"project.storage_read", "project.storage_write"}:
                raise AuthorizationError("Unknown project storage operation")
            if write and context.authorized_action != "write":
                raise AuthorizationError("Project storage mutation requires write authorization")
            request = (ProjectStorageWrite if write else ProjectStorageRead).model_validate(
                arguments
            )
            return storage.execute(
                current,
                plan.project_id,
                request.location_id,
                request.operation,
                request.arguments,
                write=write,
                key=f"agent-storage:{run_id}:{context.invocation_id}",
                revalidate=revalidate,
            )

        handlers["project_storage"] = execute
        return handlers

    return factory
