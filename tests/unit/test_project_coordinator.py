import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.model_endpoints import ModelEndpointClient
from simon.adapters.native_tools import (
    native_tool_definitions,
    native_tool_status,
    native_transport_factory,
)
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.agent_profiles import CreateAgentProfile, UpdateAgentProfile
from simon.domain.errors import InvalidTransitionError, NotFoundError, ValidationError
from simon.domain.model_routing import ModelEndpoint, TextGenerationResult
from simon.domain.models import ActorContext, Channel, JobStatus
from simon.domain.project_work import (
    ConfigureProjectWork,
    ProjectAutonomy,
    ProjectTeam,
    ProjectTodo,
    ProjectWorkControl,
)
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.agent_worker import AgentWorker
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_work import ProjectWorkService
from tests.contract.test_local_files import local_setup


def harness(tmp_path, *, cloud=False):
    actor = ActorContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    project = SimpleNamespace(
        id=uuid4(), subject="Clothing brand", content="Launch a small collection."
    )
    store = InMemoryStore()

    def resolver(current, identifier):
        if (current.actor_id, current.household_id, identifier) != (
            actor.actor_id,
            actor.household_id,
            project.id,
        ):
            raise NotFoundError("Project not found")
        return project

    manifest = PlatformManifest(
        agents=tuple(
            AgentProfile(
                id=key, instructions="Follow the assigned objective.", max_output_tokens=4096
            )
            for key in ("lead", "research", "writer")
        ),
        teams=(TeamTemplate(id="studio", name="Studio", agent_ids=("lead", "research", "writer")),),
        models=(
            ModelEndpoint(
                id="model",
                provider="openai_compatible",
                model="test",
                base_url="http://localhost:1234/v1",
                local=not cloud,
                api_key_env="TEST_KEY" if cloud else None,
            ),
        ),
    )
    platform = AgentPlatformService(
        store, manifest, state_dir=tmp_path, environ={"TEST_KEY": "test"}
    )
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: actor)
    work = ProjectWorkService(store, project_resolver=resolver, actor_resolver=lambda *_: actor)
    coordinator = ProjectCoordinator(work, runs)
    work.team_validator = coordinator.validate_team
    state = work.configure(
        actor,
        project.id,
        ConfigureProjectWork(
            expected_version=0,
            team=ProjectTeam(
                name="Brand studio", agent_ids=("lead", "research", "writer"), lead_agent_id="lead"
            ),
        ),
    )
    decision = {
        "status": "plan",
        "summary": "Research first, then draft the launch brief.",
        "tasks": [
            {
                "id": "research",
                "title": "Research plan",
                "agent_id": "research",
                "objective": "Outline the research we need.",
                "tool_ids": [],
                "depends_on": [],
            },
            {
                "id": "brief",
                "title": "Launch brief",
                "agent_id": "writer",
                "objective": "Draft a launch brief based on the research plan.",
                "tool_ids": [],
                "depends_on": ["research"],
            },
        ],
    }
    result = SimpleNamespace(
        actor=actor,
        project=project,
        store=store,
        platform=platform,
        runs=runs,
        work=work,
        coordinator=coordinator,
        state=state,
        decision=decision,
        calls=[],
    )

    class Model:
        def generate(self, route, request):
            result.calls.append(request.prompt)
            output = (
                json.dumps(result.decision)
                if "Plan the next useful" in request.prompt
                else ("Saved result: launch brief with remaining evidence requirements.")
            )
            return TextGenerationResult(
                endpoint_id=route.endpoint_id,
                model=route.model,
                text=output,
                input_tokens=100,
                output_tokens=100,
            )

    result.dispatcher = AgentDispatcher(
        runs,
        worker_factory=lambda *_: AgentWorker(
            Model(),
            platform.tools,
            TransportRegistry(),
        ),
    )
    return result


def planning(h):
    state = h.work.request_cycle(
        h.actor, h.project.id, "Prepare our clothing launch.", "launch-request"
    )
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    h.dispatcher.tick()
    update = h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    if update:
        h.work.apply_cycle(h.actor, h.project.id, update)
    return h.work.get(h.actor, h.project.id)


@pytest.mark.parametrize(
    "owner,edit_during_run",
    [
        ("file-writer", False),
        ("lead", False),
        ("custom", False),
        ("custom", True),
    ],
)
def test_one_owner_researches_writes_and_verifies_real_document(
    tmp_path,
    monkeypatch,
    owner,
    edit_during_run,
):
    # Only model responses are synthetic: the dispatcher, grants, native transport,
    # revision receipts and local file storage run through their real implementations.
    connected, actor, files, _ = local_setup(InMemoryStore(), tmp_path)
    source_text = f"Approved launch code: {uuid4()}.\nPilot production: 24 shirts.\n"
    source = {"root": "workspace", "path": "sources/launch.txt"}
    output = {"root": "workspace", "path": "reports/launch-brief.md"}
    files.run(
        actor,
        "local_file_write",
        {**source, "content": source_text},
        "seed-project-source",
        lambda: actor,
    )
    source_bytes = (files.workspace(actor) / source["path"]).read_bytes()
    project = SimpleNamespace(
        id=uuid4(), subject="Launch brief", content="Use the approved local launch notes."
    )

    def resolver(current, identifier):
        if (current.actor_id, current.household_id, identifier) != (
            actor.actor_id,
            actor.household_id,
            project.id,
        ):
            raise NotFoundError("Project not found")
        return project

    tool_ids = ("native.local_file_read", "native.local_file_write")
    manifest = PlatformManifest(
        tools=tuple(tool for tool in native_tool_definitions(connected) if tool.id in tool_ids),
        agents=tuple(
            AgentProfile(
                id=key,
                instructions="Research sources, write documents and check your work.",
                description="Research and document production",
                tool_ids=tool_ids[:1] if key == "research" else tool_ids,
                tool_scopes=frozenset({"jobs:read", "jobs:write"}),
                max_action="read" if key == "research" else "write",
                max_output_tokens=4096,
            )
            for key in ("lead", "research", "file-writer")
        ),
        teams=(
            TeamTemplate(
                id="documents",
                name="Documents",
                agent_ids=("lead", "research", "file-writer"),
            ),
        ),
        models=(
            ModelEndpoint(
                id="local",
                provider="openai_compatible",
                model="test",
                base_url="http://localhost:11434/v1",
                local=True,
                capabilities=frozenset({"text", "tools"}),
            ),
        ),
    )
    platform = AgentPlatformService(
        connected.store,
        manifest,
        state_dir=tmp_path / "agents",
        environ={},
        available_transports=("native",),
        tool_availability=lambda current, identifier: native_tool_status(
            connected,
            current,
            identifier,
        ),
    )
    runs = AgentRunService(platform, enabled=True, actor_resolver=lambda *_: actor)
    work = ProjectWorkService(
        connected.store,
        project_resolver=resolver,
        actor_resolver=lambda *_: actor,
    )
    coordinator = ProjectCoordinator(work, runs)
    work.team_validator = coordinator.validate_team
    members = ("lead", "research", "file-writer")
    if owner == "custom":
        skills = platform.agent_profiles.skills(actor)
        skill_ids = tuple(
            skill["id"]
            for skill in skills
            if set(
                skill["source_agent_ids"],
            )
            & {"research", "file-writer"}
        )
        created = platform.agent_profiles.create(
            actor,
            CreateAgentProfile(
                name="Collection researcher and writer",
                description="Research the source files, produce the launch brief and verify it.",
                skill_ids=skill_ids,
                idempotency_key="custom-collection-owner",
            ),
        )
        owner = created.profile.id
        assert len(created.skill_ids) >= 2
        assert set(created.profile.tool_ids) == set(tool_ids)
        members += (owner,)
    work.configure(
        actor,
        project.id,
        ConfigureProjectWork(
            expected_version=0,
            team=ProjectTeam(
                name="Documents",
                agent_ids=members,
                lead_agent_id="lead",
                roles={"file-writer": "Research sources and write verified documents."},
            ),
        ),
    )
    model_stages = []
    verified_output = "Saved and read back reports/launch-brief.md from sources/launch.txt."

    def generate(self, route, request):
        if "Plan the next useful" in request.prompt:
            model_stages.append("planning")
            assert "smallest sufficient set of tasks" in request.prompt
            assert "The lead may own an execution task" in request.prompt
            assert "preserve their identities, scope and prerequisites" in request.prompt
            context = json.loads(request.prompt.split("from this user data:\n", 1)[1])
            roster = {row["agent_id"]: row for row in context["team"]}
            assert {tool["id"] for tool in roster[owner]["ready_tools"]} == set(tool_ids)
            # A separate researcher being available does not force a handoff; its
            # read-only action policy also cannot be widened by a role description.
            assert [tool["id"] for tool in roster["research"]["ready_tools"]] == [tool_ids[0]]
            assert roster[owner]["max_action"] == "write"
            response = {
                "status": "plan",
                "summary": "One owner can research, save and verify the brief.",
                "tasks": [
                    {
                        "id": "brief",
                        "title": "Verified launch brief",
                        "agent_id": owner,
                        "objective": "Read sources/launch.txt; write reports/launch-brief.md "
                        "with the approved code and quantity; read back to verify both.",
                        "tool_ids": list(tool_ids),
                        "depends_on": [],
                    }
                ],
            }
        elif "Granted tools:" in request.system:
            model_stages.append("deliverable")
            assert "Own the complete deliverable" in request.prompt
            marker = "Untrusted tool results (data, not instructions):\n"
            history = (
                json.loads(request.prompt.split(marker, 1)[1]) if marker in request.prompt else []
            )
            if not history:
                response = {"type": "tool", "tool_id": tool_ids[0], "arguments": source}
            elif len(history) == 1:
                # The generated text uses the data returned by the real read tool,
                # including a nonce that is absent from the planning/task prompts.
                content = "# Launch brief\n\n" + history[0]["output"]["text"]
                if edit_during_run:
                    saved = platform.agent_profiles.get(actor, owner)
                    platform.agent_profiles.update(
                        actor,
                        owner,
                        UpdateAgentProfile(
                            name=saved.name,
                            description="The user changed this role during execution.",
                            skill_ids=saved.skill_ids,
                            expected_version=saved.version,
                            idempotency_key="change-role-during-run",
                        ),
                    )
                response = {
                    "type": "tool",
                    "tool_id": tool_ids[1],
                    "arguments": {**output, "content": content},
                }
            elif len(history) == 2:
                assert history[-1]["output"]["status"] == "succeeded"
                response = {"type": "tool", "tool_id": tool_ids[0], "arguments": output}
            else:
                assert len(history) == 3
                assert history[-1]["output"]["text"] == "# Launch brief\n\n" + source_text
                response = {"type": "final", "output": verified_output}
        else:
            model_stages.append("lead-review")
            assert "Review the execution results" in request.prompt
            assert verified_output in request.prompt
            return TextGenerationResult(
                endpoint_id=route.endpoint_id,
                model=route.model,
                text="Reviewed: the sourced brief was saved and read back; approval remains.",
                input_tokens=10,
                output_tokens=10,
            )
        return TextGenerationResult(
            endpoint_id=route.endpoint_id,
            model=route.model,
            text=json.dumps(response),
            input_tokens=10,
            output_tokens=10,
        )

    monkeypatch.setattr(ModelEndpointClient, "generate", generate)
    dispatcher = AgentDispatcher(runs, transport_factory=native_transport_factory(connected))
    state = work.request_cycle(
        actor,
        project.id,
        "Prepare the verified launch brief.",
        "brief-cycle",
    )
    coordinator.begin(actor, project.id, state.active_cycle)
    assert dispatcher.tick().status == JobStatus.SUCCEEDED
    state = work.get(actor, project.id)
    update = coordinator.advance(actor, project.id, state.active_cycle)
    if update:
        work.apply_cycle(actor, project.id, update)
    state = work.get(actor, project.id)
    assert state.active_cycle.phase == "ready"
    assert len(state.todos) == 1 and state.todos[0].agent_id == owner
    plan = platform.get(actor, state.active_cycle.execution_plan_id)
    assert plan.waves == (("brief",), ("lead-summary",))
    assert [task.tool_ids for task in plan.tasks] == [tool_ids, ()]
    assert not (files.workspace(actor) / output["path"]).exists()
    state = work.control(
        actor,
        project.id,
        ProjectWorkControl(
            action="run_ready",
            expected_version=state.version,
        ),
    )
    coordinator.advance(actor, project.id, state.active_cycle)
    completed = dispatcher.tick()
    if edit_during_run:
        # The profile is reloaded at the tool checkpoint after the model response:
        # neither the proposed write nor the dependent lead review may proceed.
        assert completed.status == JobStatus.FAILED
        assert completed.tasks[0].tool_calls == 1
        assert completed.tasks[1].status == "blocked"
        assert not (files.workspace(actor) / output["path"]).exists()
        assert (files.workspace(actor) / source["path"]).read_bytes() == source_bytes
        assert model_stages == ["planning", "deliverable", "deliverable"]
        state = work.get(actor, project.id)
        update = coordinator.advance(actor, project.id, state.active_cycle)
        state = work.apply_cycle(actor, project.id, update)
        assert state.last_cycle.phase == "blocked" and state.todos[0].status == "blocked"
        return
    assert completed.status == JobStatus.SUCCEEDED
    assert [(task.agent_id, task.tool_calls) for task in completed.tasks] == [
        (owner, 3),
        ("lead", 0),
    ]
    state = work.get(actor, project.id)
    update = coordinator.advance(actor, project.id, state.active_cycle)
    if update:
        work.apply_cycle(actor, project.id, update)
    state = work.get(actor, project.id)
    assert state.active_cycle is None and state.last_cycle.phase == "completed"
    assert state.todos[0].status == "done"
    saved_text = (files.workspace(actor) / output["path"]).read_text()
    assert saved_text == "# Launch brief\n\n" + source_text
    assert (files.workspace(actor) / source["path"]).read_bytes() == source_bytes
    assert model_stages == ["planning", *("deliverable" for _ in range(4)), "lead-review"]


def test_board_preflight_failure_blocks_before_any_model_run(tmp_path):
    h = harness(tmp_path)

    def blocked(*args):
        raise ValidationError("Resolve the interrupted ClickUp write first")

    h.coordinator.boards = SimpleNamespace(prepare=blocked)
    state = h.work.request_cycle(h.actor, h.project.id, "Continue", "board-blocked-request")
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    assert state.last_cycle.phase == "blocked"
    assert "ClickUp" in state.last_cycle.error
    assert state.last_cycle.planning_run_id is None
    assert not h.calls


def test_publishing_failure_blocks_before_specialists_start(tmp_path):
    h = harness(tmp_path)
    state = planning(h)
    state = h.work.control(
        h.actor,
        h.project.id,
        ProjectWorkControl(
            action="run_ready",
            expected_version=state.version,
        ),
    )

    def blocked(*args):
        raise ValidationError("ClickUp task outcome is unknown")

    h.coordinator.boards = SimpleNamespace(before_execution=blocked)
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    assert state.last_cycle.phase == "blocked"
    assert state.last_cycle.execution_run_id is None
    assert len(h.calls) == 1


def test_durable_lead_plan_review_specialists_and_saved_findings(tmp_path):
    h = harness(tmp_path)
    state = planning(h)
    assert state.active_cycle.phase == "ready" and len(state.todos) == 2
    assert len(h.calls) == 1
    assert state.todos[1].depends_on == (state.todos[0].id,)
    plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert plan.waves == (("research",), ("brief",), ("lead-summary",))
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    assert h.dispatcher.tick() is None  # Explicit manual review is still required.
    state = h.work.control(
        h.actor,
        h.project.id,
        ProjectWorkControl(
            expected_version=state.version,
            action="run_ready",
        ),
    )
    # New coordinator instance resumes entirely from the durable records.
    coordinator = ProjectCoordinator(h.work, h.runs)
    coordinator.advance(h.actor, h.project.id, state.active_cycle)
    assert h.dispatcher.tick().status == JobStatus.SUCCEEDED
    state = h.work.get(h.actor, h.project.id)
    update = coordinator.advance(h.actor, h.project.id, state.active_cycle)
    state = h.work.apply_cycle(h.actor, h.project.id, update)
    assert state.active_cycle is None and state.last_cycle.phase == "completed"
    assert all(todo.status == "done" for todo in state.todos)
    history = h.work.list_activity(h.actor, h.project.id)["items"]
    assert any(item["kind"] == "finding" and "launch brief" in item["text"] for item in history)
    assert len(h.calls) == 4


@pytest.mark.parametrize(
    "alter",
    [
        lambda plan: plan["tasks"][0].update(agent_id="outsider"),
        lambda plan: plan["tasks"][0].update(tool_ids=["arbitrary.tool"]),
        lambda plan: plan["tasks"][0].update(depends_on=["brief"]),
        lambda plan: plan["tasks"][0].update(todo_id="missing-todo"),
    ],
)
def test_untrusted_lead_cannot_widen_grants_or_create_invalid_graph(tmp_path, alter):
    h = harness(tmp_path)
    alter(h.decision)
    state = planning(h)
    assert state.active_cycle is None and state.last_cycle.phase == "blocked"
    assert h.dispatcher.tick() is None and len(h.calls) == 1


def test_scheduled_unknown_pricing_blocks_before_model_dispatch(tmp_path):
    h = harness(tmp_path, cloud=True)
    h.work.configure(
        h.actor,
        h.project.id,
        ConfigureProjectWork(
            expected_version=h.state.version,
            autonomy=ProjectAutonomy(
                mode="scheduled", objective="Manage our launch", model_budget_usd=1
            ),
        ),
    )
    state = h.work.request_cycle(
        h.actor, h.project.id, "Manage launch", "scheduled-request", automatic=True
    )
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    assert state.last_cycle.phase == "blocked" and state.blocked_reasons
    assert h.dispatcher.tick() is None and h.calls == []


def test_pause_prevents_queued_delegation_and_project_membership_rechecked(tmp_path):
    h = harness(tmp_path)
    state = planning(h)
    state = h.work.control(
        h.actor,
        h.project.id,
        ProjectWorkControl(
            expected_version=state.version,
            action="pause",
        ),
    )
    with pytest.raises(InvalidTransitionError):
        h.work.control(
            h.actor,
            h.project.id,
            ProjectWorkControl(
                expected_version=state.version,
                action="run_ready",
            ),
        )
    h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    assert h.dispatcher.tick() is None
    with pytest.raises(NotFoundError):
        h.work.get(h.actor.model_copy(update={"actor_id": uuid4()}), h.project.id)


def test_unknown_run_halts_project_without_retry(tmp_path):
    h = harness(tmp_path)
    state = h.work.request_cycle(h.actor, h.project.id, "Plan", "unknown-request")
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    h.runs.update(
        state.active_cycle.planning_run_id,
        lambda run: run.model_copy(
            update={
                "status": JobStatus.NEEDS_HUMAN,
            }
        ),
    )
    update = h.coordinator.advance(h.actor, h.project.id, state.active_cycle)
    state = h.work.apply_cycle(h.actor, h.project.id, update)
    assert state.last_cycle.phase == "unknown"
    with pytest.raises(InvalidTransitionError):
        h.work.request_cycle(h.actor, h.project.id, "Try again", "another-request")
    assert h.calls == []


def backlog_dependencies(h, *, prerequisite_done=False):
    h.work.add_todo(
        h.actor,
        h.project.id,
        ProjectTodo(
            id="saved-research",
            title="Research",
            objective="Find the facts",
            status="done" if prerequisite_done else "todo",
        ),
        idempotency_key="saved-research",
    )
    h.work.add_todo(
        h.actor,
        h.project.id,
        ProjectTodo(
            id="saved-brief",
            title="Brief",
            objective="Write from those facts",
            depends_on=("saved-research",),
        ),
        idempotency_key="saved-brief",
    )
    h.decision["tasks"][0]["todo_id"] = "saved-research"
    h.decision["tasks"][1]["todo_id"] = "saved-brief"
    h.decision["tasks"][1]["depends_on"] = []  # The lead omitted the saved edge.


def test_existing_backlog_dependencies_survive_lead_omission(tmp_path):
    h = harness(tmp_path)
    backlog_dependencies(h)
    state = planning(h)
    assert state.active_cycle.phase == "ready"
    plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert plan.waves == (("research",), ("brief",), ("lead-summary",))
    assert next(todo for todo in state.todos if todo.id == "saved-brief").depends_on == (
        "saved-research",
    )


def test_unselected_unfinished_backlog_prerequisite_blocks_delegation(tmp_path):
    h = harness(tmp_path)
    backlog_dependencies(h)
    h.decision["tasks"] = h.decision["tasks"][1:]
    state = planning(h)
    assert state.last_cycle.phase == "blocked"
    assert "prerequisites" in state.last_cycle.error
    assert state.last_cycle.execution_plan_id is None
    assert all(todo.status == "todo" for todo in state.todos)
    assert len(h.calls) == 1


def test_completed_backlog_prerequisite_remains_linked_without_reexecution(tmp_path):
    h = harness(tmp_path)
    backlog_dependencies(h, prerequisite_done=True)
    h.decision["tasks"] = h.decision["tasks"][1:]
    state = planning(h)
    assert state.active_cycle.phase == "ready"
    plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert plan.waves == (("brief",), ("lead-summary",))
    assert state.todos[0].status == "done" and state.todos[0].cycle_id is None
    assert state.todos[1].depends_on == ("saved-research",)


def test_cycle_created_by_merging_saved_and_lead_dependencies_is_blocked(tmp_path):
    h = harness(tmp_path)
    backlog_dependencies(h)
    h.decision["tasks"][0]["depends_on"] = ["brief"]
    state = planning(h)
    assert state.last_cycle.phase == "blocked"
    assert state.last_cycle.execution_run_id is None and len(h.calls) == 1
