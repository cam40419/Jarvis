"""A document writer can use predecessor answer artifacts without discovery permission."""

import json
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.project_output_tools import (
    project_output_definitions,
    project_output_transport_factory,
)
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import StartAgentRun
from simon.domain.agent_worker import WorkerResult
from simon.domain.model_routing import ModelEndpoint
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.project_outputs import ProjectOutputService
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from tests.completion_review_fixtures import reference_check, review_text
from tests.contract.test_project_outputs import create_output, output_setup
from tests.unit.test_agent_dispatcher import ControlledModel, make_harness, task
from tests.unit.test_worker_completion import verdict


def test_plain_reasoning_retains_unchanged_answer_without_artifact_metadata(tmp_path):
    h = output_setup(InMemoryStore(), tmp_path)
    task = create_output(h).tasks[0]
    assert len(task.artifacts) == 1
    assert AgentDispatcher._dependency_output(task) == task.output
    rendered = AgentDispatcher._dependency_output(task, include_answer=True)
    assert str(task.artifacts[0].id) in rendered
    assert task.artifacts[0].sha256 in rendered


@pytest.mark.parametrize(
    "selected",
    [
        ("project.output_read",),
        ("project.output_save",),
        ("project.output_read", "project.output_save"),
    ],
)
@pytest.mark.parametrize("reviewed", [False, True])
def test_writer_reads_or_saves_exact_predecessor_answer_without_listing(
    tmp_path, monkeypatch, selected, reviewed
):
    h = output_setup(InMemoryStore(), tmp_path)
    tools = tuple(tool for tool in project_output_definitions() if tool.id != "project.outputs")
    writer = AgentProfile(
        id="writer",
        instructions="Read and save the predecessor research when requested.",
        tool_ids=tuple(tool.id for tool in tools),
        tool_scopes=h.actor.scopes,
        max_action="write",
        max_steps=8,
    )
    research = AgentProfile(id="research", instructions="Deliver the requested research.")
    lead = AgentProfile(
        id="lead", instructions="Summarize supplied research and confirmed actions."
    )
    team = TeamTemplate(id="studio", name="Studio", agent_ids=(research.id, writer.id, lead.id))
    platform = AgentPlatformService(
        h.store,
        PlatformManifest(
            agents=(research, writer, lead),
            teams=(team,),
            tools=tools,
            models=(
                ModelEndpoint(
                    id="synthetic",
                    provider="openai_compatible",
                    model="synthetic",
                    local=True,
                    base_url="http://127.0.0.1:11434/v1",
                    capabilities=frozenset({"text", "tools"}),
                ),
            ),
        ),
        state_dir=tmp_path / "runtime",
        environ={},
        available_transports=("project_outputs",),
    )
    platform.project_visibility_resolver = h.connected.projects.project
    platform.project_team_resolver = lambda *_: team
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: h.actor)
    service = ProjectOutputService(runs, h.files)
    source_text = "Manufacturing research: compare unique-pattern setup costs and batch labor."
    stages = []
    seen_reference = None

    def respond(request):
        nonlocal seen_reference
        if "tool-free reviewer" in request.system:
            context = review_text(request.prompt, "task_context")
            phase = context.splitlines()[1]
            candidate = review_text(request.prompt, "candidate")
            checks = [
                reference_check(request.prompt, "Provide the assigned output", text=candidate)
            ]
            if phase in {"save", "lead"} and "project.output_save" in selected:
                checks.append(
                    reference_check(
                        request.prompt,
                        "Save the requested research document",
                        kind="saved_result",
                        source="task_context",
                        text='"path":"outputs/manufacturing.md"',
                    )
                )
            if phase == "lead":
                assert source_text in candidate
                assert "Controller-verified dependency project-output receipts" in request.prompt
                assert not request.controller_mode
            return verdict(*checks)
        phase = request.prompt.splitlines()[1]
        if phase == "research":
            return source_text
        dependencies, _ = json.JSONDecoder().raw_decode(
            request.prompt.split("Dependency outputs (reference data):\n", 1)[1]
        )
        handoff = dependencies["research"]
        assert handoff.startswith(source_text)
        if phase == "lead":
            assert "Granted tools:" not in request.system
            receipts = json.loads(dependencies["save"].rsplit("\n", 1)[1])["receipts"]
            assert {item["tool_id"] for item in receipts} == set(selected)
            assert all(item["sha256"] == seen_reference["sha256"] for item in receipts)
            if "project.output_save" in selected:
                saved = next(item for item in receipts if item["tool_id"] == "project.output_save")
                assert saved["project_copy"]["path"] == "outputs/manufacturing.md"
                assert saved["project_copy"]["revision"] == seen_reference["sha256"]
                return source_text + " Saved outputs/manufacturing.md."
            return source_text + " The predecessor source was read."
        references = json.loads(handoff.rsplit("\n", 1)[1])
        assert len(references) == 1
        seen_reference = references[0]
        assert seen_reference["name"] == "answer.txt"
        assert "project.outputs" not in request.system
        if len(stages) < len(selected):
            identifier = selected[len(stages)]
            stages.append(identifier)
            arguments = {key: seen_reference[key] for key in ("run_id", "artifact_id")}
            if identifier == "project.output_save":
                arguments["path"] = "outputs/manufacturing.md"
            return json.dumps({"type": "tool", "tool_id": identifier, "arguments": arguments})
        if "project.output_read" in selected:
            assert source_text in request.prompt
            assert '"untrusted_source":true' in request.prompt
        if "project.output_save" in selected:
            assert '"project_copy"' in request.prompt
        return json.dumps({"type": "final", "output": "Used the exact predecessor answer."})

    model = ControlledModel(respond)
    monkeypatch.setattr(
        "simon.services.agent_dispatcher.ModelEndpointClient", lambda *_, **__: model
    )
    dispatcher = AgentDispatcher(
        runs, transport_factory=project_output_transport_factory(lambda *_: {}, service)
    )
    plan = platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id=team.id,
            project_id=h.projects[0].id,
            idempotency_key="answer-handoff",
            tasks=(
                AgentTaskSpec(
                    id="research",
                    agent_id=research.id,
                    objective="research",
                    tool_ids=(),
                    completion_contract=PROJECT_DELIVERABLE_CONTRACT if reviewed else None,
                ),
                AgentTaskSpec(
                    id="save",
                    agent_id=writer.id,
                    objective="save",
                    depends_on=("research",),
                    tool_ids=selected,
                    completion_contract=PROJECT_DELIVERABLE_CONTRACT if reviewed else None,
                ),
                *(
                    (
                        AgentTaskSpec(
                            id="lead",
                            agent_id=lead.id,
                            objective="lead",
                            depends_on=("research", "save"),
                            tool_ids=(),
                            completion_contract=PROJECT_DELIVERABLE_CONTRACT,
                        ),
                    )
                    if reviewed
                    else ()
                ),
            ),
        ),
    )
    assert plan.state == "planned", plan
    run = runs.start(h.actor, plan.id, StartAgentRun(idempotency_key="answer-handoff-run"))
    result = dispatcher.execute(run.id)
    assert result.status == "succeeded", result
    original = result.tasks[0].artifacts[0]
    assert seen_reference["artifact_id"] == str(original.id)
    assert seen_reference["run_id"] == str(run.id)
    assert seen_reference["sha256"] == original.sha256
    assert stages == list(selected)
    assert [
        event["tool_id"] for event in result.tasks[1].events if event["event"] == "tool_dispatch"
    ] == list(selected)
    assert service.artifacts.read(original).decode() == source_text
    if reviewed:
        assert result.tasks[2].status == "succeeded"
        assert result.tasks[2].tool_calls == 0
        assert source_text in result.tasks[2].output
    if "project.output_save" in selected:
        saved, _ = service.source(h.actor, h.projects[0].id, run.id, original.id)
        copy = saved.project_copy
        assert copy is not None
        assert h.files.path(h.actor, copy.root, copy.path).read_text() == source_text


@pytest.mark.parametrize("reviewed", [False, True])
def test_legacy_dependency_text_survives_without_fabricated_save_proof(tmp_path, reviewed):
    h = make_harness(tmp_path)
    team = h.platform.manifest.teams[0]
    h.platform.project_team_resolver = lambda *_: team
    project_id = uuid4()
    summary = task("summary", "legacy_writer").model_copy(
        update={
            "completion_contract": PROJECT_DELIVERABLE_CONTRACT if reviewed else None,
        }
    )
    plan = h.platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id=team.id,
            project_id=project_id,
            idempotency_key="legacy-handoff",
            tasks=(task("legacy_writer"), summary),
        ),
    )
    assert plan.state == "planned"
    candidate = "The research compares setup costs and batch labor. Save status is unverified."
    legacy_output = "Compare setup costs and batch labor; predecessor reports a saved file."

    def respond(request):
        assert "Controller-verified dependency project-output receipts" not in request.prompt
        assert legacy_output in request.prompt
        if "tool-free reviewer" in request.system:
            return verdict(
                reference_check(request.prompt, "Provide the research summary", text=candidate),
                reference_check(
                    request.prompt,
                    "Verify the requested saved file",
                    kind="saved_result",
                    status="unverified",
                ),
                status="partial",
                summary="The retained legacy record does not prove the file was saved.",
            )
        return candidate

    model = ControlledModel(respond)
    dispatcher = h.dispatcher(model)
    worker_factory = dispatcher.worker_factory
    legacy_executions = []

    class LegacyRecordWorker:
        """Restore a pre-archive worker outcome without executing its historical action."""

        def __init__(self, worker):
            self.worker = worker

        def execute(self, **kwargs):
            if kwargs["task"].id != "legacy_writer":
                return self.worker.execute(**kwargs)
            legacy_executions.append(kwargs["task"].id)
            invocation = str(uuid4())
            kwargs["checkpoint"](
                {
                    "event": "tool_dispatch",
                    "tool_id": "project.output_save",
                    "invocation_id": invocation,
                    "side_effect": True,
                }
            )
            kwargs["checkpoint"](
                {
                    "event": "tool_complete",
                    "tool_id": "project.output_save",
                    "invocation_id": invocation,
                    "status": "succeeded",
                }
            )
            return WorkerResult(status="succeeded", output=legacy_output)

    dispatcher.worker_factory = lambda *args: LegacyRecordWorker(worker_factory(*args))
    run = h.runs.start(h.actor, plan.id, StartAgentRun(idempotency_key="legacy-handoff-run"))
    result = dispatcher.execute(run.id)
    assert legacy_executions == ["legacy_writer"]
    assert h.tool_calls == []
    assert result.tasks[0].status == "succeeded"
    assert result.tasks[1].tool_calls == 0
    assert result.tasks[1].output == candidate
    if reviewed:
        assert result.status == "failed"
        assert result.tasks[1].error_code == "incomplete_worker_output"
        assert [
            event["status"]
            for event in result.tasks[1].events
            if event["event"] == "completion_review"
        ] == ["partial", "partial"]
        assert len(model.calls) == 4
    else:
        assert result.status == "succeeded"
        assert len(model.calls) == 1
