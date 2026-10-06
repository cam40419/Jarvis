"""History remains readable when execution configuration changes."""

import pytest

from simon.domain.agent_platform import PlatformManifest
from simon.domain.agent_runs import StartAgentRun
from simon.domain.errors import InvalidTransitionError, NotFoundError, ValidationError
from simon.services.project_history import ProjectHistoryService
from tests.unit.test_project_coordinator import harness, planning


def test_completed_run_remains_readable_after_agents_removed_but_cannot_restart(tmp_path):
    h = harness(tmp_path)
    state = planning(h)
    run_id = state.active_cycle.planning_run_id
    plan_id = state.active_cycle.planning_plan_id
    h.platform.manifest = PlatformManifest()
    assert h.runs.get(h.actor, run_id).tasks[0].status == "succeeded"
    assert h.platform.get(h.actor, plan_id).id == plan_id
    page = ProjectHistoryService(h.work, h.runs).list(h.actor, h.project.id)
    assert [item.id for item in page.items] == [run_id]
    with pytest.raises(ValidationError, match="available"):
        h.coordinator.resolve_team(h.actor, h.project.id)
    with pytest.raises(InvalidTransitionError, match="changed or is unavailable"):
        h.runs.start(h.actor, plan_id, StartAgentRun(idempotency_key="forbidden-new-run"))

    def revoked(*_):
        raise NotFoundError("Project not found")

    h.platform.project_visibility_resolver = revoked
    with pytest.raises(NotFoundError):
        h.runs.get(h.actor, run_id)
