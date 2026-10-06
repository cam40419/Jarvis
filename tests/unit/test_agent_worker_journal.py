"""Controller-owned saving happens before review and survives a stopped attempt."""

import pytest

from simon.domain.artifacts import ArtifactError
from simon.domain.models import JobStatus
from simon.services.run_journal import RunJournalService
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from tests.unit.test_agent_dispatcher import ControlledModel, make_harness, task
from tests.unit.test_worker_completion import check, verdict


def test_candidate_is_durable_before_review_even_when_process_stops(tmp_path):
    h = make_harness(tmp_path)
    candidate = "Supplier comparison: the quote is provisional and excludes shipping."
    queued = h.queue(
        (
            task("research").model_copy(
                update={
                    "completion_contract": PROJECT_DELIVERABLE_CONTRACT,
                }
            ),
        )
    )

    class ProcessStopped(BaseException):
        pass

    def respond(request):
        if "tool-free reviewer" not in request.system:
            return candidate
        current = h.runs.get(h.actor, queued.id)
        assert current.tasks[0].output == ""  # No terminal worker return has happened.
        fresh = RunJournalService(h.runs)
        records = fresh._entries(h.actor, current)
        draft = next(item for item in records if item.kind == "candidate")
        assert draft.status == "draft"
        assert fresh.artifacts.read(draft.artifact).decode() == candidate
        assert {item.kind for item in records} >= {"context", "model_response", "candidate"}
        raise ProcessStopped

    with pytest.raises(ProcessStopped):
        h.dispatcher(ControlledModel(respond)).execute(queued.id)
    current = h.runs.get(h.actor, queued.id)
    assert current.status == JobStatus.RUNNING and current.tasks[0].output == ""
    fresh = RunJournalService(h.runs)
    draft = next(item for item in fresh._entries(h.actor, current) if item.kind == "candidate")
    assert fresh.artifacts.read(draft.artifact).decode() == candidate


def test_journal_failure_stops_before_any_model_or_tool_dispatch(tmp_path, monkeypatch):
    h = make_harness(tmp_path, tools=True)
    model = ControlledModel(lambda _: pytest.fail("No model request after failed persistence"))
    dispatcher = h.dispatcher(model)

    def fail(*_args, **_kwargs):
        raise ArtifactError("Synthetic disk failure")

    monkeypatch.setattr(dispatcher.journal, "append", fail)
    queued = h.queue((task("research"),))
    saved = dispatcher.execute(queued.id).tasks[0]
    assert saved.status == "failed" and saved.error_code == "journal_storage_failed"
    assert saved.steps == 0 and saved.tool_calls == 0


def test_accepted_candidate_and_context_remain_durable_without_promotion_tool(tmp_path):
    h = make_harness(tmp_path)
    candidate = "Cost comparison: supplier A quotes $12; supplier B quotes $15 before freight."
    queued = h.queue(
        (
            task("comparison").model_copy(
                update={
                    "completion_contract": PROJECT_DELIVERABLE_CONTRACT,
                }
            ),
        )
    )

    def respond(request):
        if "tool-free reviewer" in request.system:
            return verdict(check("Provide the comparison", text="supplier A quotes $12"))
        return candidate

    finished = h.dispatcher(ControlledModel(respond)).execute(queued.id)
    assert finished.status == JobStatus.SUCCEEDED
    records = RunJournalService(h.runs)._entries(h.actor, finished)
    drafts = [item for item in records if item.kind == "candidate"]
    assert len(drafts) == 1 and drafts[0].status == "accepted"
    assert len([item for item in records if item.kind == "model_response"]) == 2
    assert RunJournalService(h.runs).artifacts.read(drafts[0].artifact).decode() == candidate
    assert finished.tasks[0].tool_calls == 0
