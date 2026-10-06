"""Larger model responses get a bounded transport allowance, not longer tool calls."""

import pytest

from simon.services.agent_dispatcher import AgentDispatcher
from tests.unit.test_agent_dispatcher import make_harness, task


@pytest.mark.parametrize(
    ("output_tokens", "profile_timeout", "expected"),
    [
        (2000, 600, 120),
        (8192, 600, 120),
        (8193, 600, 300),
        (32768, 3600, 300),
        (32768, 180, 180),
        (32768, 45, 45),
        (8192, 45, 45),
    ],
)
def test_default_worker_factory_bounds_model_timeout_without_changing_task_limits(
    tmp_path, output_tokens, profile_timeout, expected
):
    harness = make_harness(tmp_path)
    run = harness.queue((task("research"),))
    assert harness.runs.claim(run.id) is not None
    configured = harness.platform.manifest.agents[0].model_copy(
        update={"max_output_tokens": output_tokens, "timeout_seconds": profile_timeout}
    )
    original = configured.model_dump()
    worker = AgentDispatcher(harness.runs)._worker(configured, None, harness.actor, run.id)
    assert worker.model_client._timeout == expected
    assert configured.model_dump() == original
    assert configured.timeout_seconds == profile_timeout
    assert worker.transports._handlers["http"].timeout_seconds == min(30, profile_timeout)
