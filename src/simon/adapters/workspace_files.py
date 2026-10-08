"""Copy an authorized local input into a uniquely named file in an owned Docker workspace."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import Field

from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.execution import EnvironmentLease
from simon.domain.models import ActorContext, StrictModel
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition, ToolExecutionContext
from simon.services.local_files import LocalFileService, reject_links, revision


class ImportFile(StrictModel):
    root: str = Field(pattern=r"^workspace$")
    path: str = Field(min_length=1, max_length=1000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def workspace_file_definition() -> ToolDefinition:
    return ToolDefinition(
        id="workspace.import_local",
        transport="workspace_files",
        configured=True,
        description=(
            "Copy a local workspace file into this task's Docker workspace. "
            "Returns the assigned input filename and hash; use that filename in Python/Git/media "
            "tools. Original files are never changed. Files are limited to 50 MiB."
        ),
        categories=frozenset({"files"}),
        capabilities=frozenset({"workspace.import"}),
        required_scopes=frozenset({"jobs:read", "jobs:write"}),
        environment_capabilities=frozenset({"workspace.write"}),
        side_effect=True,
        action_policy="write",
        input_schema=ImportFile.model_json_schema(),
    )


def workspace_file_configuration_reason(tool: ToolDefinition) -> str | None:
    definitions = (workspace_file_definition(),)
    canonical = next((item for item in definitions if item.id == tool.id), None)
    if canonical is None or tool.transport != "workspace_files":
        return "Unknown workspace import tool"
    if any(
        getattr(tool, field) != getattr(canonical, field)
        for field in (
            "input_schema",
            "side_effect",
            "action_policy",
            "required_scopes",
            "environment_capabilities",
        )
    ):
        return "Workspace import contract does not match its installed definition"
    return None


class WorkspaceFileTransport:
    def __init__(
        self,
        files: LocalFileService,
        lease: EnvironmentLease,
        *,
        actor: ActorContext,
        run_id: UUID,
        revalidate: Callable[[], ActorContext],
    ) -> None:
        self.files, self.lease, self.actor = files, lease.model_copy(deep=True), actor
        self.run_id, self.revalidate = run_id, revalidate

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if reason := workspace_file_configuration_reason(definition):
            raise ToolCatalogError(reason)
        canonical = definition
        owner = self.lease.plan.request
        if (
            context.actor_id != self.actor.actor_id
            or context.workspace_id != owner.workspace_id
            or context.workspace_id != self.actor.workspace_id
            or context.run_id != self.run_id
            or context.agent_id != owner.agent_id
            or not context.scopes >= canonical.required_scopes
            or context.authorized_action != "write"
            or definition.id not in context.allowed_tool_ids
        ):
            raise AuthorizationError("Workspace import is outside this assignment's grant")
        if (
            definition.id != canonical.id
            or definition.transport != canonical.transport
            or not definition.enabled
            or not definition.configured
            or not definition.side_effect
            or definition.action_policy != "write"
            or self.lease.status != "active"
            or self.lease.definition.kind != "docker"
            or not context.environment_capabilities >= canonical.environment_capabilities
            or not self.lease.definition.capabilities >= canonical.environment_capabilities
        ):
            raise ToolCatalogError("Workspace import requires an active writable Docker assignment")
        actor = self.revalidate()
        if (actor.actor_id, actor.workspace_id) != (self.actor.actor_id, self.actor.workspace_id):
            raise AuthorizationError("Workspace import account changed")
        actor = actor.model_copy(
            update={
                "scopes": actor.scopes & self.actor.scopes & context.scopes,
            }
        )
        if not actor.scopes >= canonical.required_scopes:
            raise AuthorizationError("Workspace import permission changed")
        request = ImportFile.model_validate(arguments)
        self.files.authorize(actor, write=True)
        source = self.files.path(actor, request.root, request.path)
        data = self.files.blob(source)
        suffix, expected = source.suffix, request.expected_sha256
        digest = revision(data)
        if expected is not None and digest != expected:
            raise ValidationError("Input file changed; read its current revision before importing")
        # Only the mount root and a controller-generated leaf are used. Container
        # processes cannot replace mount ancestors; exclusive create rejects a raced
        # symlink/file at the leaf. No model-supplied destination directories are opened.
        filename = f"input-{context.invocation_id}{suffix}"
        destination = self.lease.plan.workspace_path / filename
        reject_links(destination)
        current = self.revalidate()
        if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
            raise AuthorizationError("Workspace import account changed")
        current = current.model_copy(
            update={
                "scopes": current.scopes & self.actor.scopes & context.scopes,
            }
        )
        if not current.scopes >= canonical.required_scopes:
            raise AuthorizationError("Workspace import permission changed")
        self.files.authorize(current, write=True)
        if self.files.path(current, request.root, request.path) != source:
            raise AuthorizationError("Workspace import source changed")
        try:
            with destination.open("xb") as target:
                target.write(data)
        except FileExistsError:
            raise ValidationError(
                "This import already has a workspace file; inspect it first",
            ) from None
        metadata = {"source_root": request.root, "source_path": request.path}
        return {"path": destination.name, "size": len(data), "sha256": digest, **metadata}
