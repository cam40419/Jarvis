from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any
from uuid import UUID, uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    AgentTeamPlan,
    PlannedAgentTask,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import AgentRun, StartAgentRun
from simon.domain.agent_worker import WorkerResult
from simon.domain.errors import ValidationError
from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentLease,
    EnvironmentRequest,
    ExecutionCommand,
    ExecutionError,
    ExecutionResult,
)
from simon.domain.model_routing import (
    ModelEndpoint,
    RoutingDecision,
    TextGenerationRequest,
    TextGenerationResult,
)
from simon.domain.models import ActorContext, Channel, JobStatus
from simon.domain.tool_catalog import ToolDefinition, ToolExecutionContext
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.agent_worker import AgentWorker


class FakeBackend:
    def __init__(self) -> None:
        self.created: list[EnvironmentLease] = []
        self.released: list[EnvironmentLease] = []
        self.fail_allocation = False

    def allocate(self, lease: EnvironmentLease) -> str:
        self.created.append(lease)
        if self.fail_allocation:
            raise ExecutionError("Allocation response was lost")
        return "fake-" + lease.id.hex

    def release(self, lease: EnvironmentLease) -> None:
        self.released.append(lease)

    def heartbeat(self, lease: EnvironmentLease) -> None:
        pass

    def execute(self, lease: EnvironmentLease, command: ExecutionCommand) -> ExecutionResult:
        raise AssertionError("These dispatcher tests do not execute environment commands")


class ControlledModel:
    def __init__(
        self,
        respond: Callable[[TextGenerationRequest], str | tuple[str, bool]] | None = None,
    ) -> None:
        self.respond = respond or (lambda _request: "A complete answer.")
        self.calls: list[TextGenerationRequest] = []
        self.lock = Lock()

    def generate(
        self,
        decision: RoutingDecision,
        request: TextGenerationRequest,
    ) -> TextGenerationResult:
        with self.lock:
            self.calls.append(request)
        reply = self.respond(request)
        text, truncated = reply if isinstance(reply, tuple) else (reply, False)
        return TextGenerationResult(
            endpoint_id=decision.endpoint_id,
            model=decision.model,
            text=text,
            input_tokens=20,
            output_tokens=10,
            truncated=truncated,
        )


@dataclass
class Harness:
    platform: AgentPlatformService
    runs: AgentRunService
    actor: ActorContext
    backend: FakeBackend
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def plan(self, tasks: tuple[AgentTaskSpec, ...], key: str = "test-plan") -> AgentTeamPlan:
        plan = self.platform.plan(
            self.actor,
            PlanTeamRequest(
                team_id="team",
                idempotency_key=key,
                tasks=tasks,
            ),
        )
        assert plan.state == "planned", [task.blocked_reasons for task in plan.tasks]
        return plan

    def queue(self, tasks: tuple[AgentTaskSpec, ...], key: str = "test-plan") -> AgentRun:
        plan = self.plan(tasks, key)
        return self.runs.start(self.actor, plan.id, StartAgentRun(idempotency_key="execute-run"))

    def dispatcher(self, model: ControlledModel, *, reconstruct: bool = False) -> AgentDispatcher:
        runs = self.runs
        if reconstruct:
            platform = AgentPlatformService(
                self.platform.store,
                self.platform.manifest,
                state_dir=self.platform.state_dir,
                environ={"TEST_AGENT_KEY": "fake-key"},
                available_transports=("test", "environment"),
            )
            platform.environments.backends["docker"] = self.backend
            runs = AgentRunService(
                platform,
                enabled=True,
                actor_resolver=lambda _actor, _workspace: self.actor,
            )

        def tool_handler(
            definition: ToolDefinition,
            arguments: dict[str, Any],
            context: ToolExecutionContext,
        ) -> dict[str, Any]:
            self.tool_calls.append(arguments)
            return {"value": "A tool result."}

        def worker_factory(
            profile: AgentProfile,
            lease: EnvironmentLease | None,
            actor: ActorContext,
            run_id: UUID,
        ) -> AgentWorker:
            transports = TransportRegistry()
            transports.register("test", tool_handler)
            return AgentWorker(model, runs.platform.tools, transports)

        return AgentDispatcher(runs, worker_factory=worker_factory)


def make_harness(
    tmp_path: Path,
    *,
    max_parallel: int = 3,
    environment: bool = False,
    tools: bool = False,
    endpoint: ModelEndpoint | None = None,
) -> Harness:
    actor = ActorContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    manifest = PlatformManifest(
        max_parallel=max_parallel,
        agents=(
            AgentProfile(
                id="worker",
                instructions="Create a complete answer using the supplied task.",
                environment_ids=("shared",) if environment else (),
                tool_ids=("lookup",) if tools else (),
            ),
        ),
        teams=(
            TeamTemplate(
                id="team", name="Worker team", agent_ids=("worker",), max_parallel=max_parallel
            ),
        ),
        environments=(
            EnvironmentDefinition(
                id="shared",
                kind="docker",
                container_image="unused-test-image",
                enabled=True,
                max_concurrency=1,
            ),
        )
        if environment
        else (),
        tools=(
            ToolDefinition(
                id="lookup",
                description="Look up a record",
                transport="test",
                configured=True,
            ),
        )
        if tools
        else (),
        models=(
            endpoint
            or ModelEndpoint(
                id="local",
                provider="openai_compatible",
                model="fake-model",
                base_url="http://localhost:11434/v1",
                local=True,
                tier="economy",
                capabilities=frozenset({"text", "tools"}),
            ),
        ),
    )
    platform = AgentPlatformService(
        InMemoryStore(),
        manifest,
        state_dir=tmp_path,
        environ={"TEST_AGENT_KEY": "fake-key"},
        available_transports=("test", "environment"),
    )
    backend = FakeBackend()
    platform.environments.backends["docker"] = backend
    runs = AgentRunService(
        platform,
        enabled=True,
        actor_resolver=lambda _actor, _workspace: actor,
    )
    return Harness(platform, runs, actor, backend)


def task(name: str, *dependencies: str) -> AgentTaskSpec:
    return AgentTaskSpec(id=name, agent_id="worker", objective=name, depends_on=dependencies)


def test_dispatcher_stops_environment_before_collecting_deliverable(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, environment=True)

    class FileWorker:
        def execute(self, **kwargs):
            lease = harness.backend.created[-1]
            (lease.plan.workspace_path / "result.csv").write_bytes(b"name,value\nresult,42\n")
            return WorkerResult(
                status="succeeded", output="Created CSV.", artifact_paths=("result.csv",)
            )

    dispatcher = AgentDispatcher(harness.runs, worker_factory=lambda *_args: FileWorker())
    queued = harness.queue((task("create"),))
    completed = dispatcher.execute(queued.id)
    assert completed.status == "succeeded"
    assert len(harness.backend.released) == 1
    artifacts = completed.tasks[0].artifacts
    assert [item.name for item in artifacts] == ["answer.txt", "result.csv"]
    assert dispatcher.artifacts.read(artifacts[1]) == b"name,value\nresult,42\n"


def test_parallel_siblings_finish_before_dependent_receives_their_outputs(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, max_parallel=2)
    barrier = Barrier(2, timeout=5)

    def respond(request: TextGenerationRequest) -> str:
        objective = request.prompt.splitlines()[1]
        if objective in {"source-a", "source-b"}:
            barrier.wait()
            return objective.upper() + " RESULT"
        assert '"source-a": "SOURCE-A RESULT"' in request.prompt
        assert '"source-b": "SOURCE-B RESULT"' in request.prompt
        return "Both sources combined."

    model = ControlledModel(respond)
    queued = harness.queue(
        (
            task("source-a"),
            task("source-b"),
            task("combine", "source-a", "source-b"),
        )
    )
    completed = harness.dispatcher(model).execute(queued.id)
    assert completed is not None and completed.status == JobStatus.SUCCEEDED
    assert len(model.calls) == 3
    assert model.calls[-1].prompt.splitlines()[1] == "combine"
    assert completed.tasks[-1].output == "Both sources combined."


def test_dependent_opens_exact_saved_file_revision(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, environment=True)

    class FileWorker:
        def execute(self, **kwargs):
            workspace = harness.backend.created[-1].plan.workspace_path
            if kwargs["task"].id == "maker":
                (workspace / "dimensions.csv").write_bytes(b"part,dimension\nplate,-12\n")
                return WorkerResult(
                    status="succeeded",
                    output="Dimensions written.",
                    artifact_paths=("dimensions.csv",),
                )
            inputs = kwargs["dependency_artifacts"]
            assert len(inputs) == 1
            assert (workspace / inputs[0].workspace_path).read_bytes().endswith(b"plate,-12\n")
            manifest = json.loads((workspace / "dependency-inputs.json").read_text())
            assert manifest["inputs"][0]["artifact"]["sha256"] == inputs[0].artifact.sha256
            # The run stores the exact input before review begins.
            saved = harness.runs.view(harness.runs.job(kwargs["run_id"]))
            assert saved.tasks[1].input_artifacts == inputs
            return WorkerResult(status="succeeded", output="Rejected: plate dimension is negative.")

    dispatcher = AgentDispatcher(harness.runs, worker_factory=lambda *_args: FileWorker())
    queued = harness.queue((task("maker"), task("reviewer", "maker")))
    completed = dispatcher.execute(queued.id)
    assert completed.status == "succeeded"
    maker, reviewer = completed.tasks
    assert reviewer.input_artifacts[0].artifact == maker.artifacts[1]
    assert reviewer.output == "Rejected: plate dimension is negative."
    assert dispatcher.artifacts.read(maker.artifacts[1]).endswith(b"plate,-12\n")


def test_global_slots_apply_across_runs_and_service_instances(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, max_parallel=1)
    entered, release = Event(), Event()

    def respond(_request: TextGenerationRequest) -> str:
        entered.set()
        assert release.wait(timeout=10)
        return "Finished."

    model = ControlledModel(respond)
    first = harness.queue((task("first"),), "first-plan")
    second = harness.queue((task("second"),), "second-plan")
    owner = harness.dispatcher(model)
    competitor = harness.dispatcher(model, reconstruct=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner.execute, first.id)
        try:
            assert entered.wait(timeout=5)
            assert competitor.execute(second.id) is None
            assert harness.runs.get(harness.actor, second.id).status == JobStatus.QUEUED
            assert len(model.calls) == 1
        finally:
            release.set()
        assert future.result(timeout=10).status == JobStatus.SUCCEEDED
    completed = competitor.execute(second.id)
    assert completed is not None and completed.status == JobStatus.SUCCEEDED
    assert len(model.calls) == 2


def test_shared_environment_capacity_waits_across_runs_without_unknown_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = make_harness(tmp_path, max_parallel=2, environment=True)
    entered, release, waiting = Event(), Event(), Event()

    def respond(_request: TextGenerationRequest) -> str:
        entered.set()
        assert release.wait(timeout=10)
        return "Finished in an exclusive workspace."

    model = ControlledModel(respond)
    first = harness.queue((task("first"),), "first-plan")
    second = harness.queue((task("second"),), "second-plan")
    owner = harness.dispatcher(model)
    competitor = harness.dispatcher(model, reconstruct=True)
    reserve = competitor._reserve_task

    def observed_reserve(run: AgentRun, planned: PlannedAgentTask) -> bool:
        reserved = reserve(run, planned)
        if not reserved:
            waiting.set()
        return reserved

    monkeypatch.setattr(competitor, "_reserve_task", observed_reserve)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(owner.execute, first.id)
        try:
            assert entered.wait(timeout=5)
            second_future = pool.submit(competitor.execute, second.id)
            assert waiting.wait(timeout=5)
            waiting_state = harness.runs.get(harness.actor, second.id)
            assert waiting_state.tasks[0].status == "queued"
            assert len(harness.backend.created) == 1
            assert len(model.calls) == 1
        finally:
            release.set()
        assert first_future.result(timeout=10).status == JobStatus.SUCCEEDED
        assert second_future.result(timeout=10).status == JobStatus.SUCCEEDED
    assert len(harness.backend.created) == len(harness.backend.released) == 2
    assert len(model.calls) == 2


def test_cancelling_queued_run_prevents_environment_and_model_allocation(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, environment=True)
    queued = harness.queue((task("first"), task("second", "first")))
    cancelled = harness.runs.cancel(harness.actor, queued.id)
    model = ControlledModel()
    assert cancelled.status == JobStatus.CANCELLED
    assert harness.dispatcher(model).execute(queued.id) is None
    assert model.calls == []
    assert harness.backend.created == []
    assert all(item.status == "cancelled" for item in cancelled.tasks)


def test_cancel_during_model_call_prevents_proposed_tool_and_dependent_task(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, tools=True)
    entered, release = Event(), Event()

    def respond(_request: TextGenerationRequest) -> str:
        entered.set()
        assert release.wait(timeout=10)
        return json.dumps({"type": "tool", "tool_id": "lookup", "arguments": {}})

    model = ControlledModel(respond)
    queued = harness.queue((task("first"), task("dependent", "first")))
    dispatcher = harness.dispatcher(model)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatcher.execute, queued.id)
        try:
            assert entered.wait(timeout=5)
            harness.runs.cancel(harness.actor, queued.id)
        finally:
            release.set()
        completed = future.result(timeout=10)
    assert completed is not None and completed.cancel_requested
    assert completed.status == JobStatus.CANCELLED
    assert completed.tasks[1].status == "cancelled"
    assert not completed.tasks[0].artifacts
    assert len(model.calls) == 1
    assert harness.tool_calls == []


def test_failed_dependency_blocks_its_dependent_but_independent_task_succeeds(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, max_parallel=2)

    def respond(request: TextGenerationRequest) -> str | tuple[str, bool]:
        objective = request.prompt.splitlines()[1]
        if objective == "failing":
            return ("Incomplete output", True)
        assert objective == "independent"
        return "Independent work completed."

    model = ControlledModel(respond)
    queued = harness.queue((task("failing"), task("dependent", "failing"), task("independent")))
    completed = harness.dispatcher(model).execute(queued.id)
    assert completed is not None and completed.status == JobStatus.FAILED
    assert [record.status for record in completed.tasks] == ["failed", "blocked", "succeeded"]
    assert completed.tasks[1].error_code == "dependency_failed"
    assert len(model.calls) == 2


def test_changed_manifest_before_dispatch_prevents_any_model_call(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    queued = harness.queue((task("first"),))
    profile = harness.platform.manifest.agents[0].model_copy(
        update={
            "instructions": "Changed role",
        }
    )
    harness.platform.manifest = harness.platform.manifest.model_copy(update={"agents": (profile,)})
    model = ControlledModel()
    completed = harness.dispatcher(model).execute(queued.id)
    assert completed is not None and completed.status == JobStatus.NEEDS_HUMAN
    assert model.calls == []
    assert completed.reserved_slots == 0


@pytest.mark.parametrize("known_prices", [True, False])
def test_aggregate_run_budget_rejects_excess_or_unknown_cloud_cost(
    tmp_path: Path,
    known_prices: bool,
) -> None:
    endpoint = ModelEndpoint(
        id="cloud",
        provider="openai_compatible",
        model="fake-cloud",
        base_url="https://model.example/v1",
        local=False,
        tier="economy",
        api_key_env="TEST_AGENT_KEY",
        input_cost_per_million_usd=1 if known_prices else None,
        output_cost_per_million_usd=1 if known_prices else None,
    )
    harness = make_harness(tmp_path, endpoint=endpoint)
    plan = harness.plan((task("first"), task("second")))
    # One task reserves $0.034768; two exceed the aggregate $0.05 admission cap.
    with pytest.raises(ValidationError, match="all task reservations"):
        harness.runs.start(
            harness.actor,
            plan.id,
            StartAgentRun(
                idempotency_key="budgeted-run",
                model_budget_usd=0.05,
            ),
        )
    assert harness.runs.list(harness.actor) == ()


def test_allocation_failure_keeps_lease_reference_and_attempts_owned_cleanup(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, environment=True)
    harness.backend.fail_allocation = True
    queued = harness.queue((task("first"),))
    model = ControlledModel()
    completed = harness.dispatcher(model).execute(queued.id)
    assert completed is not None and completed.status == JobStatus.NEEDS_HUMAN
    record = completed.tasks[0]
    assert record.status == "unknown"
    assert record.environment_lease_id is not None
    assert record.environment_lease_id == harness.backend.created[0].id
    assert harness.backend.released[0].id == record.environment_lease_id
    assert harness.platform.environments.get(record.environment_lease_id).status == "released"
    assert model.calls == []


def test_preexisting_external_lease_blocks_without_claiming_unknown_execution(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, environment=True)
    existing = harness.platform.environments.allocate(
        EnvironmentRequest(
            workspace_id=harness.actor.household_id,
            agent_id="worker",
            task_id=uuid4(),
            attempt_id=uuid4(),
        ),
        environment_id="shared",
    )
    queued = harness.queue((task("first"),))
    model = ControlledModel()
    try:
        completed = harness.dispatcher(model).execute(queued.id)
        assert completed is not None and completed.status == JobStatus.FAILED
        assert completed.tasks[0].status == "blocked"
        assert completed.tasks[0].error_code == "environment_capacity_unavailable"
        assert completed.tasks[0].environment_lease_id is None
        assert harness.platform.environments.get(existing.id).status == "active"
        assert model.calls == []
        assert harness.backend.released == []
    finally:
        harness.platform.environments.release(
            existing.id,
            attempt_id=existing.plan.request.attempt_id,
            fencing_token=existing.fencing_token,
        )
