from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Event, Lock
from uuid import UUID

import pytest

from simon.adapters.tool_transports import TransportRegistry
from simon.domain.agent_platform import (
    AgentProfile,
    AgentTaskSpec,
    AgentTeamPlan,
    PlanTeamRequest,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import StartAgentRun
from simon.domain.errors import IdempotencyConflictError
from simon.domain.execution import EnvironmentLease
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from simon.domain.model_routing import (
    ModelEndpoint,
    RoutingDecision,
    TextGenerationRequest,
    TextGenerationResult,
)
from simon.domain.models import ActorContext, Channel, JobStatus
from simon.domain.ports import Store
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.agent_worker import AgentWorker
from simon.services.artifacts import ArtifactStore


class RecordingModel:
    def __init__(self, *, block: bool = False) -> None:
        self.calls: list[TextGenerationRequest] = []
        self.entered = Event()
        self.release = Event()
        self.lock = Lock()
        self.block = block

    def generate(
        self, decision: RoutingDecision, request: TextGenerationRequest
    ) -> TextGenerationResult:
        with self.lock:
            self.calls.append(request)
        self.entered.set()
        if self.block and not self.release.wait(timeout=10):
            raise RuntimeError("Contract test did not release its controlled model call")
        return TextGenerationResult(
            endpoint_id=decision.endpoint_id, model=decision.model,
            text="A completed brand outline.", input_tokens=32, output_tokens=11,
        )


@dataclass
class RunFixture:
    actor: ActorContext
    manifest: PlatformManifest
    platform: AgentPlatformService
    runs: AgentRunService
    plan: AgentTeamPlan
    state_dir: Path

    def reconstruct(self) -> AgentRunService:
        platform = AgentPlatformService(
            self.platform.store, self.manifest, state_dir=self.state_dir, environ={},
        )
        return AgentRunService(
            platform, enabled=True, actor_resolver=lambda _actor, _workspace: self.actor,
        )

    def dispatcher(self, runs: AgentRunService, model: RecordingModel) -> AgentDispatcher:
        def worker_factory(
            profile: AgentProfile, lease: EnvironmentLease | None, actor: ActorContext,
            run_id: UUID,
        ) -> AgentWorker:
            assert profile.id == "writer"
            assert lease is None
            assert actor.actor_id == self.actor.actor_id
            return AgentWorker(model, runs.platform.tools, TransportRegistry())

        return AgentDispatcher(
            runs, artifacts=ArtifactStore(self.state_dir / "artifacts"),
            worker_factory=worker_factory,
        )


@pytest.fixture
def run_fixture(store: Store, tmp_path: Path) -> RunFixture:
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID, household_id=DEV_HOUSEHOLD_ID, channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    manifest = PlatformManifest(
        max_parallel=2,
        agents=(AgentProfile(
            id="writer", instructions="Write the brand outline using the supplied brief.",
            output_instructions="Return a concise finished draft.",
        ),),
        teams=(TeamTemplate(id="solo", name="Solo writer", agent_ids=("writer",)),),
        models=(ModelEndpoint(
            id="local", provider="openai_compatible", model="contract-model",
            base_url="http://localhost:11434/v1", local=True, tier="economy",
        ),),
    )
    platform = AgentPlatformService(store, manifest, state_dir=tmp_path, environ={})
    runs = AgentRunService(
        platform, enabled=True, actor_resolver=lambda _actor, _workspace: actor,
    )
    plan = platform.plan(actor, PlanTeamRequest(
        team_id="solo", idempotency_key="contract-plan",
        tasks=(AgentTaskSpec(id="draft", agent_id="writer", objective="Draft a brand outline"),),
    ))
    assert plan.state == "planned"
    return RunFixture(actor, manifest, platform, runs, plan, tmp_path)


def test_queued_agent_run_survives_service_reconstruction(run_fixture: RunFixture) -> None:
    fixture = run_fixture
    queued = fixture.runs.start(
        fixture.actor, fixture.plan.id, StartAgentRun(idempotency_key="durable-queue"),
    )
    raw = fixture.platform.store.get_job(queued.id)
    assert raw is not None
    assert raw.result is None
    reconstructed = fixture.reconstruct()
    assert reconstructed.get(fixture.actor, queued.id) == queued
    assert reconstructed.list(fixture.actor) == (queued,)
    assert queued.status == JobStatus.QUEUED
    assert not queued.execution_started
    assert queued.tasks[0].status == "queued"


def test_dispatch_result_and_artifact_roundtrip_after_reconstruction(
    run_fixture: RunFixture,
) -> None:
    fixture = run_fixture
    queued = fixture.runs.start(
        fixture.actor, fixture.plan.id, StartAgentRun(idempotency_key="durable-result"),
    )
    reconstructed = fixture.reconstruct()
    model = RecordingModel()
    dispatcher = fixture.dispatcher(reconstructed, model)
    completed = dispatcher.tick()
    assert completed is not None
    assert completed.id == queued.id
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.execution_started
    assert completed.reserved_slots == 0
    assert completed.finished_at is not None
    task = completed.tasks[0]
    assert task.status == "succeeded"
    assert task.output == "A completed brand outline."
    assert (task.steps, task.tool_calls, task.input_tokens, task.output_tokens) == (1, 0, 32, 11)
    assert [event["event"] for event in task.events] == ["model_dispatch", "model_complete"]
    assert len(task.artifacts) == 1
    artifact = task.artifacts[0]
    assert artifact.run_id == queued.id
    assert artifact.actor_id == fixture.actor.actor_id
    assert artifact.workspace_id == fixture.actor.household_id
    assert dispatcher.artifacts.read(artifact) == task.output.encode()
    assert fixture.reconstruct().get(fixture.actor, queued.id) == completed
    assert len(model.calls) == 1
    assert "Write the brand outline" in model.calls[0].system
    assert "Return a concise finished draft" in model.calls[0].system
    assert "Draft a brand outline" in model.calls[0].prompt
    assert dispatcher.tick() is None


def test_duplicate_start_returns_original_run_before_and_after_execution(
    run_fixture: RunFixture,
) -> None:
    fixture = run_fixture
    request = StartAgentRun(idempotency_key="idempotent-start")
    queued = fixture.runs.start(fixture.actor, fixture.plan.id, request)
    reconstructed = fixture.reconstruct()
    assert reconstructed.start(fixture.actor, fixture.plan.id, request) == queued
    model = RecordingModel()
    completed = fixture.dispatcher(reconstructed, model).execute(queued.id)
    assert completed is not None
    assert fixture.runs.start(fixture.actor, fixture.plan.id, request) == completed
    with pytest.raises(IdempotencyConflictError):
        fixture.runs.start(fixture.actor, fixture.plan.id, request.model_copy(update={
            "model_budget_usd": 1.0,
        }))
    assert len(fixture.runs.list(fixture.actor)) == 1
    assert len(model.calls) == 1


def test_simultaneous_claims_have_exactly_one_owner(run_fixture: RunFixture) -> None:
    fixture = run_fixture
    queued = fixture.runs.start(
        fixture.actor, fixture.plan.id, StartAgentRun(idempotency_key="concurrent-claim"),
    )
    contenders = [fixture.reconstruct(), fixture.reconstruct()]
    barrier = Barrier(2, timeout=10)

    def claim(runs: AgentRunService):
        barrier.wait()
        return runs.claim(queued.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, runs) for runs in contenders]
        claims = [future.result(timeout=15) for future in futures]
    owners = [state for state in claims if state is not None]
    assert len(owners) == 1
    assert owners[0].executor_id is not None
    assert fixture.runs.get(fixture.actor, queued.id) == owners[0]
    assert fixture.reconstruct().claim(queued.id) is None


def test_competing_dispatchers_do_not_repeat_an_inflight_model_call(
    run_fixture: RunFixture,
) -> None:
    fixture = run_fixture
    queued = fixture.runs.start(
        fixture.actor, fixture.plan.id, StartAgentRun(idempotency_key="concurrent-dispatch"),
    )
    model = RecordingModel(block=True)
    first = fixture.dispatcher(fixture.reconstruct(), model)
    second = fixture.dispatcher(fixture.reconstruct(), model)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(first.execute, queued.id)
        try:
            assert model.entered.wait(timeout=10), "Dispatcher did not reach the fake model"
            assert second.execute(queued.id) is None
            assert second.tick() is None
            assert len(model.calls) == 1
        finally:
            model.release.set()
        completed = active.result(timeout=15)
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED
    assert second.execute(queued.id) is None
    assert len(model.calls) == 1
