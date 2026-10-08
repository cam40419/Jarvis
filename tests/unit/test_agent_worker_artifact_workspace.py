"""Generic file saves are not Docker workspace exports or permission to publish paths."""

import json
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from simon.adapters.tool_transports import TransportRegistry
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_worker import AgentWorker, _controller_schema
from simon.services.tool_catalog import ToolCatalog
from tests.unit.test_agent_worker import Responses, assignment, profile
from tests.unit.test_agent_worker import actor as actor
from tests.unit.test_agent_worker import endpoint as endpoint
from tests.unit.test_agent_worker_completion import action


@pytest.mark.parametrize("exportable", [False, True])
@pytest.mark.parametrize("tool_ids", [(), ("files.save",)])
def test_initial_and_final_only_schema_enforce_actual_workspace_availability(exportable, tool_ids):
    validator = Draft202012Validator(_controller_schema(tool_ids, exportable_workspace=exportable))
    control = {"action": {"type": "final", "output": "Useful answer.", "artifacts": []}}
    validator.validate(control)
    control["action"]["artifacts"] = ["deliverables/report.md"]
    assert validator.is_valid(control) is exportable
    control["action"]["artifacts"] = [f"file-{index}.md" for index in range(17)]
    assert not validator.is_valid(control)
    control["action"]["artifacts"] = ["x" * 1001]
    assert not validator.is_valid(control)


@pytest.mark.parametrize("provider", ["openai_responses", "openai_compatible"])
@pytest.mark.parametrize("exportable", [False, True])
@pytest.mark.parametrize("paths", [[], ["research/report.md"]])
def test_file_save_and_final_answer_do_not_require_or_imply_a_workspace_export(
    actor, endpoint, provider, exportable, paths
):
    endpoint = endpoint.model_copy(update={"provider": provider})
    definition = ToolDefinition(
        id="files.save",
        transport="test_files",
        description="Save a file",
        configured=True,
        side_effect=True,
        action_policy="write",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "artifact_id": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["run_id", "artifact_id", "path"],
            "additionalProperties": False,
        },
    )
    actor = actor.model_copy(update={"scopes": actor.scopes | definition.required_scopes})
    agent = profile(tools=True, max_action="write").model_copy(
        update={"tool_ids": (definition.id,), "tool_scopes": actor.scopes}
    )
    task, spec = assignment(endpoint, tools=True)
    task = task.model_copy(update={"tool_ids": (definition.id,)})
    candidate = "Saved research/report.md from the verified report artifact."
    model = Responses(
        action(
            endpoint,
            "tool",
            tool_id=definition.id,
            arguments={
                "run_id": str(uuid4()),
                "artifact_id": str(uuid4()),
                "path": "research/report.md",
            },
        ),
        action(endpoint, "final", output=candidate, artifacts=paths),
    )
    calls = []

    def save_file(_definition, arguments, context):
        calls.append(context.invocation_id)
        return {"saved_file": {"path": arguments["path"], "revision": "a" * 64, "bytes": 321}}

    registry = TransportRegistry()
    registry.register("test_files", save_file)
    worker = AgentWorker(
        model, ToolCatalog((definition,), available_transports=("test_files",)), registry
    )
    events = []
    result = worker.execute(
        actor=actor,
        run_id=uuid4(),
        task=task,
        spec=spec,
        profile=agent,
        dependency_outputs={},
        exportable_workspace=exportable,
        # Capabilities and a successful write alone never confer export authority.
        environment_capabilities=frozenset({"python", "browser"}),
        checkpoint=events.append,
    )
    assert result.output == candidate
    assert result.steps == len(model.calls) == 2
    assert result.tool_calls == len(calls) == 1
    assert (result.input_tokens, result.output_tokens) == (40, 10)
    assert len(result.provenance) == 1 and result.provenance[0].status == "succeeded"
    assert [event["event"] for event in events].count("tool_dispatch") == 1
    assert "report confirmed paths in final.output, never artifacts" in model.calls[0][1].system
    if exportable or not paths:
        assert result.status == "succeeded"
        assert result.artifact_paths == tuple(paths)
    else:
        assert result.status == "failed"
        assert result.error_code == "artifact_workspace_unavailable"
        assert result.artifact_paths == ()
        assert (
            "no exportable Docker workspace: final.artifacts must be []" in model.calls[0][1].system
        )
    if provider == "openai_responses":
        final_schema = model.calls[-1][1].response_schema
        final_document = json.loads(action(endpoint, "final", output=candidate, artifacts=paths))
        assert Draft202012Validator(final_schema).is_valid(final_document) is (
            exportable or not paths
        )
