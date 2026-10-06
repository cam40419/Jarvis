"""Bounded project scheduling and reconciliation; agent runs own all model execution."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from simon.domain.errors import DomainError, InvalidTransitionError
from simon.domain.models import ActorContext, Job
from simon.domain.project_work import ProjectCycle, ProjectCycleUpdate
from simon.services.project_work import WORK_KIND, ProjectWorkService


class ProjectCoordinatorProtocol(Protocol):
    def begin(
        self,
        actor: ActorContext,
        project_id: UUID,
        cycle: ProjectCycle,
    ) -> ProjectCycleUpdate | None: ...

    def advance(
        self,
        actor: ActorContext,
        project_id: UUID,
        cycle: ProjectCycle,
    ) -> ProjectCycleUpdate | None: ...


class ProjectAutonomyService:
    def __init__(
        self,
        work: ProjectWorkService,
        coordinator: ProjectCoordinatorProtocol,
        *,
        enabled: bool = False,
        batch_size: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("Project scheduling batch must be between one and 100")
        self.work, self.coordinator, self.enabled = work, coordinator, enabled
        self.clock, self.batch_size = clock or work.clock, batch_size
        self._cursor = 0

    def tick(self) -> int:
        if not self.enabled:
            return 0
        changed = 0
        continuity = getattr(self.coordinator, "continuity", None)
        if continuity is not None:
            changed += continuity.tick(self.batch_size)
        # Running projects include paused work that needs to record an already
        # dispatched run's progress. Rotate within the bounded scan for fairness.
        active = list(self.work.store.jobs_all(WORK_KIND, 10000, "running"))
        if active:
            start = self._cursor % len(active)
            candidates = (active[start:] + active[:start])[: self.batch_size]
            self._cursor = (start + len(candidates)) % len(active)
            for job in candidates:
                changed += self._advance(job)
        due = self.work.store.jobs_all(WORK_KIND, self.batch_size, "queued")
        for job in due:
            state = self.work.view(job)
            if state.next_cycle_at is None:
                self.work.halt(
                    job, "Scheduled work has no saved due date. Review its configuration."
                )
                changed += 1
                continue
            if state.next_cycle_at > self.clock():
                continue
            try:
                actor = self.work.live_actor(job)
                if state.scheduled_cycles_used >= state.autonomy.max_cycles:
                    self.work.halt(job, "The configured scheduled cycle limit has been reached.")
                    changed += 1
                    continue
                key = f"scheduled:{state.cycle_count + 1}:{state.next_cycle_at.isoformat()}"
                self.work.request_cycle(
                    actor,
                    state.project_id,
                    state.autonomy.objective,
                    key,
                    automatic=True,
                    expected_version=state.version,
                )
                changed += 1
            except InvalidTransitionError:
                # Another scheduler, a pause, or a user edit won the same decision.
                continue
            except DomainError:
                self.work.halt(
                    job, "Scheduled work stopped because project access or setup changed."
                )
                changed += 1
            except Exception:
                self.work.halt(
                    job, "Project scheduling failed. Review saved state before continuing."
                )
                changed += 1
        return changed

    def _advance(self, candidate: Job) -> int:
        try:
            actor = self.work.live_actor(candidate)
            state = self.work.get(actor, UUID(candidate.input["project_id"]))
            cycle = state.active_cycle
            if cycle is None:
                return 0
            if state.autonomy.paused and cycle.phase in {"starting", "ready"}:
                return 0
            if cycle.phase == "starting":
                update = self.coordinator.begin(actor, state.project_id, cycle)
            else:
                update = self.coordinator.advance(actor, state.project_id, cycle)
            if update is not None:
                self.work.apply_cycle(actor, state.project_id, update)
            return int(self.work.get(actor, state.project_id).version != state.version)
        except InvalidTransitionError:
            return 0  # The saved revision, pause or cycle changed while coordinating.
        except DomainError:
            self.work.halt(
                candidate, "Project coordination stopped because access or setup changed."
            )
        except Exception:
            # A callback may have queued a run before failing. Never call it again
            # automatically after an unexpected error; preserve run IDs for review.
            self.work.halt(
                candidate,
                "Project coordination was interrupted. Review linked runs.",
                uncertain=True,
            )
        return 1
