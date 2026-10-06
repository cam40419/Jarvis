"""Coordinator output targets must fit authorized, configured model endpoints."""

import json

import pytest

from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import AgentProfile, AgentTaskSpec
from simon.domain.model_routing import ModelEndpoint
from simon.domain.project_coordination import LeadDecision
from simon.services.agent_worker import AgentWorker
from simon.services.model_router import ModelRouter
from tests.unit.test_agent_worker import Responses
from tests.unit.test_project_coordinator import harness, planning


def endpoint(identifier="model", **changes):
    return ModelEndpoint(
        **{
            "id": identifier,
            "provider": "openai_compatible",
            "model": "fixture",
            "base_url": "http://localhost:1234/v1",
            "local": True,
            "max_output_tokens": 4096,
            **changes,
        }
    )


def configure(h, endpoints, **profile_changes):
    h.platform.manifest = h.platform.manifest.model_copy(
        update={
            "agents": tuple(
                item.model_copy(update={"max_output_tokens": 8192, **profile_changes})
                for item in h.platform.manifest.agents
            ),
            "models": tuple(endpoints),
        }
    )
    h.platform.models = ModelRouter(endpoints, environ={"KEY": "synthetic"})


@pytest.mark.parametrize("ceiling", [2048, 4096, 8192, 12000, 32768])
@pytest.mark.parametrize("profile_limit", [1536, 8192, 32768])
def test_planning_execution_and_summary_fit_configured_output_ceiling(
    tmp_path, ceiling, profile_limit
):
    h = harness(tmp_path)
    configure(
        h,
        [
            endpoint(
                max_output_tokens=ceiling,
                context_window_tokens=131072,
                reasoning_efforts=("medium",),
            )
        ],
        max_output_tokens=profile_limit,
    )
    state = planning(h)
    assert state.active_cycle.phase == "ready"
    planning_job = h.store.get_job(state.active_cycle.planning_plan_id)
    execution_job = h.store.get_job(state.active_cycle.execution_plan_id)
    limit = min(ceiling, profile_limit)
    assert planning_job.input["request"]["tasks"][0]["output_tokens"] == limit
    planning_plan = h.platform.get(h.actor, state.active_cycle.planning_plan_id)
    assert planning_plan.tasks[0].model.request.output_tokens == limit
    assert planning_plan.tasks[0].model.reasoning_effort == "medium"
    expected = {"research": limit, "brief": limit, "lead-summary": limit}
    assert {
        task["id"]: task["output_tokens"] for task in execution_job.input["request"]["tasks"]
    } == expected
    plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert all(task.model.endpoint_id == "model" for task in plan.tasks)
    assert {task.id: task.model.request.output_tokens for task in plan.tasks} == expected
    assert all(task.model.reasoning_effort == "medium" for task in plan.tasks)
    assert all(profile.max_output_tokens == profile_limit for profile in h.platform.manifest.agents)


def test_larger_lead_allowance_does_not_raise_other_members_configured_limits(tmp_path):
    h = harness(tmp_path)
    configure(
        h,
        [
            endpoint(
                provider="openai_responses",
                max_output_tokens=32768,
                context_window_tokens=131072,
                reasoning_efforts=("medium",),
            )
        ],
    )
    limits = {"lead": 32768, "research": 8192, "writer": 2048}
    h.platform.manifest = h.platform.manifest.model_copy(
        update={
            "agents": tuple(
                item.model_copy(update={"max_output_tokens": limits[item.id]})
                for item in h.platform.manifest.agents
            )
        }
    )
    model = Responses(
        json.dumps({"action": {"type": "final", "output": h.decision, "artifacts": []}})
    )
    h.dispatcher.worker_factory = lambda *_: AgentWorker(
        model, h.platform.tools, TransportRegistry()
    )
    state = planning(h)
    assert state.active_cycle.phase == "ready"
    planning_plan = h.platform.get(h.actor, state.active_cycle.planning_plan_id)
    assert planning_plan.tasks[0].model.request.output_tokens == 32768
    assert model.calls[0][1].max_output_tokens == 32768
    assert model.calls[0][1].response_schema is not None
    execution_plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert {task.id: task.model.request.output_tokens for task in execution_plan.tasks} == {
        "research": 8192,
        "brief": 2048,
        "lead-summary": 32768,
    }
    assert all(task.model.reasoning_effort == "medium" for task in execution_plan.tasks)
    assert {item.id: item.max_output_tokens for item in h.platform.manifest.agents} == limits


def test_full_requested_output_is_preserved_when_a_valid_route_supports_it(tmp_path):
    h = harness(tmp_path)
    configure(h, [endpoint("small"), endpoint("large", max_output_tokens=12000)])
    profile = h.platform.manifest.agents[0]
    task = AgentTaskSpec(
        id="draft", agent_id=profile.id, objective="Draft report", output_tokens=8000
    )
    assert h.coordinator._bounded_task_output(profile, task) == task


def test_explicit_model_override_keeps_its_own_ceiling(tmp_path):
    h = harness(tmp_path)
    configure(
        h,
        [endpoint("small", max_output_tokens=2048), endpoint("large", max_output_tokens=16000)],
        model_override="small",
    )
    state = planning(h)
    assert state.active_cycle.phase == "ready"
    plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert all(task.model.endpoint_id == "small" for task in plan.tasks)
    assert all(task.model.request.output_tokens == 2048 for task in plan.tasks)


def test_local_only_never_uses_cloud_capacity(tmp_path):
    h = harness(tmp_path)
    configure(
        h,
        [
            endpoint("private", max_output_tokens=2048),
            endpoint("cloud", local=False, api_key_env="KEY"),
        ],
        privacy="local_only",
    )
    state = planning(h)
    assert state.active_cycle.phase == "ready"
    plan = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert all(task.model.endpoint_id == "private" for task in plan.tasks)
    assert all(task.model.request.privacy == "local_only" for task in plan.tasks)
    assert all(task.model.request.output_tokens == 2048 for task in plan.tasks)


@pytest.mark.parametrize(
    "profile_changes,endpoint_changes,task_changes",
    [
        ({"depth": 5}, {"tier": "economy"}, {}),
        ({"privacy": "local_only"}, {"local": False, "api_key_env": "KEY"}, {}),
        ({}, {"enabled": False}, {}),
        ({}, {"local": False, "api_key_env": "MISSING_KEY"}, {}),
        ({"model_override": "absent"}, {}, {}),
        ({}, {}, {"tool_ids": ("explicit.tool",)}),
        ({}, {"context_window_tokens": 4096}, {"input_tokens": 5000}),
        (
            {},
            {"input_cost_per_million_usd": 1, "output_cost_per_million_usd": 1},
            {"budget_usd": 0},
        ),
    ],
)
def test_lower_output_does_not_bypass_other_route_requirements(
    tmp_path, profile_changes, endpoint_changes, task_changes
):
    h = harness(tmp_path)
    configure(h, [endpoint(**endpoint_changes)])
    profile = AgentProfile(
        id="role", instructions="Assigned objective", max_output_tokens=8000, **profile_changes
    )
    task = AgentTaskSpec(
        id="draft",
        agent_id=profile.id,
        objective="Draft report",
        output_tokens=8000,
        **task_changes,
    )
    # Leave unavailable requests intact so ordinary planning reports its blockers.
    assert h.coordinator._bounded_task_output(profile, task) == task


def test_quality_floor_still_blocks_compiled_plan(tmp_path):
    h = harness(tmp_path)
    configure(h, [endpoint(tier="economy")], depth=5)
    state = h.work.request_cycle(h.actor, h.project.id, "Prepare launch", "quality-floor")
    update = h.coordinator._compile(h.actor, state, LeadDecision.model_validate(h.decision))
    assert update.cycle.phase == "blocked"
    assert "quality floor" in update.cycle.error
    plan = h.platform.get(h.actor, update.cycle.execution_plan_id)
    assert all(task.model is None for task in plan.tasks)


def test_unavailable_project_lead_blocks_without_starting_a_run(tmp_path, monkeypatch):
    h = harness(tmp_path)
    original = h.platform.profiles

    def profiles(actor, project_id=None):
        return tuple(
            profile
            for profile in original(actor, project_id)
            if project_id is None or profile.id != "lead"
        )

    monkeypatch.setattr(h.platform, "profiles", profiles)
    state = h.work.request_cycle(h.actor, h.project.id, "Prepare launch", "lead-unavailable")
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    assert state.last_cycle.phase == "blocked"
    assert "lead is unavailable" in state.last_cycle.error
    assert state.last_cycle.planning_run_id is None


def test_task_constraints_and_inputs_are_preserved(tmp_path):
    h = harness(tmp_path)
    configure(h, [endpoint(capabilities=frozenset({"text", "tools"}))])
    profile = h.platform.manifest.agents[0]
    task = AgentTaskSpec(
        id="draft",
        agent_id=profile.id,
        objective="Draft report",
        output_tokens=8000,
        input_tokens=6000,
        depth=3,
        importance=3,
        privacy="local_only",
        model_override="model",
        tool_ids=("explicit.tool",),
        budget_usd=0,
    )
    bounded = h.coordinator._bounded_task_output(profile, task)
    assert bounded.output_tokens == 4096
    assert bounded.model_dump(exclude={"output_tokens"}) == task.model_dump(
        exclude={"output_tokens"}
    )
    assert task.output_tokens == 8000
