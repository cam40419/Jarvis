"""Publication failures preserve settled project writes and the completed worker result."""

import json

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
from simon.domain.artifacts import Artifact, ArtifactError
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import JobStatus
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.project_outputs import ProjectOutputService
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from tests.completion_review_fixtures import reference_check, review_text
from tests.contract.test_project_outputs import create_output, output_setup
from tests.unit.test_agent_dispatcher import ControlledModel
from tests.unit.test_worker_completion import verdict


@pytest.mark.parametrize("publication_failure", ["workspace", "integrity", "interrupted"])
def test_real_saved_project_copies_and_usage_survive_final_artifact_failure(
    tmp_path, monkeypatch, publication_failure
):
    h = output_setup(InMemoryStore(), tmp_path)
    source_run = create_output(h, count=2)
    originals = source_run.tasks[0].artifacts
    sources = [h.service.artifacts.read(artifact) for artifact in originals]
    definition = next(
        tool for tool in project_output_definitions() if tool.id == "project.output_save"
    )
    profile = AgentProfile(
        id="writer",
        instructions="Save the two exact predecessor reports.",
        tool_ids=(definition.id,),
        tool_scopes=h.actor.scopes,
        max_action="write",
    )
    team = TeamTemplate(id="studio", name="Studio", agent_ids=(profile.id,))
    platform = AgentPlatformService(
        h.store,
        PlatformManifest(
            agents=(profile,),
            teams=(team,),
            tools=(definition,),
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
        state_dir=h.platform.state_dir,
        environ={},
        available_transports=("project_outputs",),
    )
    platform.project_visibility_resolver = h.connected.projects.project
    platform.project_team_resolver = lambda *_: team
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: h.actor)
    service = ProjectOutputService(runs, h.files)
    paths = ("outputs/manufacturing.md", "outputs/competition.md")
    candidate = "Saved both exact research reports to this project's files; their hashes match."
    requests = []

    def respond(request):
        if "tool-free reviewer" in request.system:
            assert review_text(request.prompt, "candidate") == candidate
            return verdict(
                reference_check(request.prompt, "Report the completed saves", text=candidate),
                *(
                    reference_check(
                        request.prompt,
                        f"Save report {index}",
                        kind="saved_result",
                        source="task_context",
                        text=f'"path":"{path}"',
                    )
                    for index, path in enumerate(paths)
                ),
            )
        index = len(requests)
        requests.append(request)
        if index < 2:
            return json.dumps(
                {
                    "type": "tool",
                    "tool_id": definition.id,
                    "arguments": {
                        "run_id": str(source_run.id),
                        "artifact_id": str(originals[index].id),
                        "path": paths[index],
                    },
                }
            )
        return json.dumps({"type": "final", "output": candidate})

    model = ControlledModel(respond)
    monkeypatch.setattr(
        "simon.services.agent_dispatcher.ModelEndpointClient", lambda *_, **__: model
    )
    dispatcher = AgentDispatcher(
        runs, transport_factory=project_output_transport_factory(lambda *_: {}, service)
    )
    completed_worker_results = []
    original_factory = dispatcher._worker

    def worker_factory(*arguments):
        worker = original_factory(*arguments)
        execute = worker.execute

        def completed(**kwargs):
            result = execute(**kwargs)
            assert result.status == "succeeded", result
            completed_worker_results.append(result)
            # Exercise the dispatcher defense independently of the worker's
            # newer schema guard against non-workspace artifact paths.
            return (
                result.model_copy(update={"artifact_paths": paths})
                if publication_failure == "workspace"
                else result
            )

        worker.execute = completed
        return worker

    dispatcher.worker_factory = worker_factory
    if publication_failure != "workspace":
        publish_text = dispatcher.artifacts.publish_text

        def failed_publication(**_kwargs):
            if _kwargs.get("name", "").startswith("candidate-"):
                return publish_text(**_kwargs)
            if publication_failure == "integrity":
                raise ArtifactError("Synthetic artifact integrity failure")
            raise OSError("Synthetic unavailable artifact storage")

        monkeypatch.setattr(dispatcher.artifacts, "publish_text", failed_publication)
    plan = platform.plan(
        h.actor,
        PlanTeamRequest(
            team_id=team.id,
            project_id=h.projects[0].id,
            idempotency_key="save-before-publication",
            tasks=(
                AgentTaskSpec(
                    id="save",
                    agent_id=profile.id,
                    objective="Save both reports unchanged into project files.",
                    tool_ids=(definition.id,),
                    completion_contract=PROJECT_DELIVERABLE_CONTRACT,
                ),
            ),
        ),
    )
    assert plan.state == "planned", plan
    queued = runs.start(h.actor, plan.id, StartAgentRun(idempotency_key="execute-save"))
    completed = dispatcher.execute(queued.id)
    assert completed is not None
    saved = completed.tasks[0]
    assert (
        saved.error_code
        == {
            "workspace": "artifact_workspace_unavailable",
            "integrity": "artifact_integrity_or_handoff_failed",
            "interrupted": "worker_interrupted",
        }[publication_failure]
    )
    assert saved.status == ("unknown" if publication_failure == "interrupted" else "failed")
    assert completed.status == (
        JobStatus.NEEDS_HUMAN if publication_failure == "interrupted" else JobStatus.FAILED
    )
    assert saved.output == candidate
    assert (saved.steps, saved.tool_calls, saved.input_tokens, saved.output_tokens) == (
        4,
        2,
        80,
        40,
    )
    assert saved.artifacts == () and completed.reserved_slots == 0
    assert len(completed_worker_results) == 1
    worker_result = completed_worker_results[0]
    assert len(worker_result.provenance) == 2
    receipts = [event for event in saved.events if event["event"] == "tool_complete"]
    assert [event["invocation_id"] for event in receipts] == [
        str(record.invocation_id) for record in worker_result.provenance
    ]
    assert all(event["status"] == "succeeded" for event in receipts)
    assert any(
        event["event"] == "completion_review" and event["status"] == "complete"
        for event in saved.events
    )
    for index, receipt in enumerate(receipts):
        archive = Artifact.model_validate(receipt["evidence_artifact"])
        evidence = json.loads(dispatcher.evidence.read(archive))
        assert evidence["side_effect"] is True and evidence["status"] == "succeeded"
        assert evidence["output"]["project_copy"]["path"] == paths[index]
        assert (
            h.files.path(h.actor, f"project:{h.projects[0].id}", paths[index]).read_bytes()
            == sources[index]
        )
    saves = [
        event
        for event in h.store.audit_events(h.actor.workspace_id)
        if event.event_type == "project.output_saved"
    ]
    assert len(saves) == 2
    assert dispatcher.execute(queued.id) is None  # Terminal failure never replays the writes.
    assert len(model.calls) == 4
    assert (
        len(
            [
                event
                for event in h.store.audit_events(h.actor.workspace_id)
                if event.event_type == "project.output_saved"
            ]
        )
        == 2
    )
