"""Admission covers every permitted completion-review call, including tool-free work."""

import pytest

from simon.domain.agent_runs import StartAgentRun
from simon.domain.errors import ValidationError
from simon.domain.model_routing import ModelEndpoint
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from tests.unit.test_agent_dispatcher import ControlledModel, make_harness, task


def priced_harness(tmp_path):
    h = make_harness(
        tmp_path,
        endpoint=ModelEndpoint(
            id="priced",
            provider="openai_compatible",
            model="synthetic",
            base_url="https://model.example/v1",
            api_key_env="TEST_AGENT_KEY",
            max_output_tokens=8192,
            input_cost_per_million_usd=1,
            output_cost_per_million_usd=1,
        ),
    )
    h.platform.manifest = h.platform.manifest.model_copy(
        update={
            "agents": tuple(
                profile.model_copy(update={"max_steps": 8, "max_output_tokens": 8192})
                for profile in h.platform.manifest.agents
            )
        }
    )
    return h


@pytest.mark.parametrize("reviewed,calls_reserved", [(False, 1), (True, 8)])
def test_tool_free_completion_contract_reserves_its_full_permitted_model_loop(
    tmp_path, reviewed, calls_reserved
):
    h = priced_harness(tmp_path)
    spec = task("summary").model_copy(
        update={
            "output_tokens": 8192,
            "completion_contract": PROJECT_DELIVERABLE_CONTRACT if reviewed else None,
        }
    )
    plan = h.plan((spec,))
    assert plan.tasks[0].tool_ids == ()
    assert plan.tasks[0].model.request.output_tokens == 8192
    per_call = (32768 + 8192) / 1_000_000
    queued = h.runs.start(
        h.actor,
        plan.id,
        StartAgentRun(
            idempotency_key="reserved-completion", model_budget_usd=per_call * calls_reserved
        ),
    )
    assert queued.model_reserved_usd == pytest.approx(per_call * calls_reserved)
    assert queued.tasks[0].model_reserved_usd == pytest.approx(per_call * calls_reserved)
    assert queued.tasks[0].status == "queued" and not queued.execution_started


@pytest.mark.parametrize("budget_scope", ["task", "run"])
def test_budget_covering_one_call_cannot_admit_a_multi_call_completion_contract(
    tmp_path, budget_scope
):
    h = priced_harness(tmp_path)
    spec = task("summary").model_copy(
        update={
            "output_tokens": 8192,
            "completion_contract": PROJECT_DELIVERABLE_CONTRACT,
            "budget_usd": 0.05 if budget_scope == "task" else None,
        }
    )
    plan = h.plan((spec,))
    with pytest.raises(ValidationError, match="budget cannot cover"):
        h.runs.start(
            h.actor,
            plan.id,
            StartAgentRun(
                idempotency_key="insufficient-completion-budget",
                model_budget_usd=0.05 if budget_scope == "run" else None,
            ),
        )
    assert h.runs.list(h.actor) == ()
    model = ControlledModel()
    assert h.dispatcher(model).tick() is None
    assert model.calls == []
