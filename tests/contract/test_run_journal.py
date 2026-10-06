"""Generated drafts and exact evidence survive independent services in memory and PostgreSQL."""

import hashlib
import json
from uuid import uuid4

import pytest

from simon.domain.errors import AuthorizationError, InvalidTransitionError, NotFoundError
from simon.domain.models import JobStatus
from simon.services.project_outputs import ProjectOutputService
from simon.services.run_journal import RunJournalService
from tests.contract.test_project_outputs import create_output, output_setup


def running(h):
    run = create_output(h)
    executor = uuid4()
    run = h.runs.update(
        run.id,
        lambda current: current.model_copy(
            update={
                "status": JobStatus.RUNNING,
                "executor_id": executor,
                "tasks": (
                    current.tasks[0].model_copy(
                        update={"status": "running", "artifacts": (), "output": ""}
                    ),
                ),
            }
        ),
    )
    task = h.platform.get(h.actor, run.plan_id).tasks[0]
    return run, task, executor


def append(h, run, task, executor, kind, step, **payload):
    return h.service.journal.append(
        h.actor,
        run.id,
        task,
        executor_id=executor,
        project_id=h.projects[0].id,
        kind=kind,
        step=step,
        payload=payload,
    )


def test_candidate_saved_before_terminal_result_and_readable_after_restart(store, tmp_path):
    h = output_setup(store, tmp_path)
    run, task, executor = running(h)
    original = "# Manufacturing report\nA complete numeric model is still missing."
    entry = append(h, run, task, executor, "candidate", 2, text=original)
    assert h.runs.get(h.actor, run.id).tasks[0].output == ""
    independent = ProjectOutputService(h.runs, h.files)
    output = independent.list(h.actor, h.projects[0].id).items[0]
    assert output.id == entry.artifact.id and output.status == "draft"
    assert output.source == "candidate" and output.step == 2 and output.title
    assert independent.read(h.actor, h.projects[0].id, run.id, output.id)[1].decode() == original
    # Simulate a process interruption before it can return its candidate to the dispatcher.
    h.runs.update(
        run.id,
        lambda current: current.model_copy(
            update={
                "status": JobStatus.NEEDS_HUMAN,
                "executor_id": None,
                "tasks": (current.tasks[0].model_copy(update={"status": "unknown"}),),
            }
        ),
    )
    saved = independent.list(h.actor, h.projects[0].id).items[0]
    assert saved.id == output.id and saved.status == "partial" and saved.task_status == "unknown"
    assert independent.artifacts.preview(entry.artifact).text == original


def test_candidate_revision_reviews_are_durable_without_erasing_previous_drafts(store, tmp_path):
    h = output_setup(store, tmp_path)
    run, task, executor = running(h)
    first = append(h, run, task, executor, "candidate", 1, text="Draft with a gap.")
    assert append(h, run, task, executor, "candidate", 1, text="Draft with a gap.") == first
    append(
        h,
        run,
        task,
        executor,
        "review",
        2,
        status="partial",
        candidate_sha256=hashlib.sha256(b"Draft with a gap.").hexdigest(),
    )
    second = append(h, run, task, executor, "candidate", 3, text="Finished report with evidence.")
    append(
        h,
        run,
        task,
        executor,
        "review",
        4,
        status="complete",
        candidate_sha256=hashlib.sha256(b"Finished report with evidence.").hexdigest(),
    )
    page = ProjectOutputService(h.runs, h.files).list(h.actor, h.projects[0].id, limit=1)
    assert page.items[0].id == first.artifact.id and page.items[0].status == "partial"
    page = h.service.list(h.actor, h.projects[0].id, cursor=page.next_cursor, limit=1)
    assert page.items[0].id == second.artifact.id and page.items[0].status == "accepted"
    with pytest.raises(InvalidTransitionError, match="different content"):
        append(
            h, run, task, executor, "candidate", 1, text="Changed content cannot replace the draft."
        )


def test_journal_evidence_is_exact_and_cross_project_account_and_executor_fail_closed(
    store, tmp_path
):
    h = output_setup(store, tmp_path)
    run, task, executor = running(h)
    invocation = uuid4()
    source = "Exact archived middle source. " * 900
    payload = {
        "invocation_id": str(invocation),
        "tool_id": "source.read",
        "status": "succeeded",
        "side_effect": False,
        "output": {"text": source},
    }
    artifact = h.service.journal.evidence.publish_text(
        workspace_id=h.actor.workspace_id,
        actor_id=h.actor.actor_id,
        run_id=run.id,
        task_id=task.task_id,
        name=f"{invocation}.json",
        media_type="application/json",
        text=json.dumps(payload),
    )
    entry = h.service.journal.append(
        h.actor,
        run.id,
        task,
        executor_id=executor,
        project_id=h.projects[0].id,
        kind="evidence",
        step=1,
        payload={},
        artifact=artifact,
    )
    fresh = RunJournalService(h.runs)
    page = fresh.read(
        h.actor, h.projects[0].id, run.id, entry.id, pointer="/output/text", offset=8000, limit=8000
    )
    assert page.text == source[8000:16000] and page.next_offset == 16000
    assert page.value_sha256 == hashlib.sha256(source.encode()).hexdigest()
    for actor, project in (
        (h.actor, h.projects[1].id),
        (h.actor.model_copy(update={"actor_id": uuid4()}), h.projects[0].id),
        (h.actor.model_copy(update={"workspace_id": uuid4()}), h.projects[0].id),
    ):
        with pytest.raises(NotFoundError):
            fresh.read(actor, project, run.id, entry.id)
    before = tuple(h.platform.state_dir.rglob("content"))
    with pytest.raises(InvalidTransitionError):
        append(h, run, task, uuid4(), "candidate", 2, text="Stale writer")
    assert tuple(h.platform.state_dir.rglob("content")) == before
    with pytest.raises(InvalidTransitionError):
        h.service.journal.append(
            h.actor,
            run.id,
            task,
            executor_id=executor,
            project_id=h.projects[1].id,
            kind="candidate",
            step=2,
            payload={"text": "Wrong project"},
        )


def test_context_and_visible_response_redaction_retains_no_configured_secret(store, tmp_path):
    h = output_setup(store, tmp_path)
    h.platform._environ = {"MY_API_KEY": "synthetic-secret-credential"}
    run, task, executor = running(h)
    task = task.model_copy(update={"objective": "Inspect synthetic-secret-credential sources"})
    entry = append(
        h,
        run,
        task,
        executor,
        "context",
        0,
        objective="Use synthetic-secret-credential",
        password="test-password",
        headers={"Authorization": "Bearer private"},
        references=["Ordinary source text"],
        visible_response=json.dumps({"action": {"arguments": {"api_key": "unknown-new-key"}}}),
    )
    page = RunJournalService(h.runs).read(h.actor, h.projects[0].id, run.id, entry.id)
    assert page.entry.redacted
    assert page.entry.title == "Inspect [redacted] sources"
    assert "synthetic-secret-credential" not in page.text and "test-password" not in page.text
    assert "Bearer private" not in page.text and "Ordinary source text" in page.text
    assert "unknown-new-key" not in page.text
    assert "[redacted]" in page.text


@pytest.mark.parametrize("status", ["failed", "cancelled", "unknown", "succeeded"])
def test_explicit_legacy_backfill_keeps_original_and_is_idempotent(store, tmp_path, status):
    h = output_setup(store, tmp_path)
    run = create_output(h)
    text = "Saved old attempt with a comparison and an unfinished calculation."
    historical_executor = uuid4()
    run = h.runs.update(
        run.id,
        lambda current: current.model_copy(
            update={
                "status": JobStatus.NEEDS_HUMAN if status == "unknown" else JobStatus(status),
                "executor_id": historical_executor,
                "reserved_slots": 0,
                "tasks": (
                    current.tasks[0].model_copy(
                        update={
                            "output": text,
                            "status": status,
                            "artifacts": (),
                        }
                    ),
                ),
            }
        ),
    )
    preview = h.service.journal.backfill(h.actor, h.projects[0].id, run.id, dry_run=True)
    assert preview["eligible"] == 1 and preview["created"] == 0
    assert h.service.list(h.actor, h.projects[0].id).items == ()
    result = h.service.journal.backfill(h.actor, h.projects[0].id, run.id)
    assert result["created"] == 1 and result["next_offset"] is None
    assert h.runs.get(h.actor, run.id) == run
    assert h.runs.get(h.actor, run.id).executor_id == historical_executor
    item = h.service.list(h.actor, h.projects[0].id).items[0]
    assert item.status == ("accepted" if status == "succeeded" else "partial")
    assert item.task_status == status
    assert h.service.read(h.actor, h.projects[0].id, run.id, item.id)[1].decode() == text
    assert h.service.journal.backfill(h.actor, h.projects[0].id, run.id)["created"] == 0
    assert len(h.service.list(h.actor, h.projects[0].id).items) == 1


@pytest.mark.parametrize("reserved,task_status", [(1, "failed"), (0, "queued"), (0, "running")])
def test_terminal_backfill_still_rejects_reservations_and_unsettled_tasks(
    store, tmp_path, reserved, task_status
):
    h = output_setup(store, tmp_path)
    run = create_output(h)
    run = h.runs.update(
        run.id,
        lambda current: current.model_copy(
            update={
                "status": JobStatus.FAILED,
                "executor_id": uuid4(),
                "reserved_slots": reserved,
                "tasks": (
                    current.tasks[0].model_copy(
                        update={
                            "status": task_status,
                            "output": "Prior visible answer",
                            "artifacts": (),
                        }
                    ),
                ),
            }
        ),
    )
    with pytest.raises(InvalidTransitionError, match="settled"):
        h.service.journal.backfill(h.actor, h.projects[0].id, run.id)
    assert h.service.journal.list_run(h.actor, h.projects[0].id, run.id) == ()


def test_legacy_backfill_rejects_running_foreign_and_readonly_and_skips_existing_artifacts(
    store, tmp_path
):
    h = output_setup(store, tmp_path)
    finished = create_output(h)
    assert h.service.journal.backfill(h.actor, h.projects[0].id, finished.id)["created"] == 0
    run, _, _ = running(h)
    with pytest.raises(InvalidTransitionError, match="settled"):
        h.service.journal.backfill(h.actor, h.projects[0].id, run.id)
    with pytest.raises(NotFoundError):
        h.service.journal.backfill(h.actor, h.projects[1].id, run.id)
    with pytest.raises(NotFoundError):
        h.service.journal.backfill(
            h.actor.model_copy(update={"actor_id": uuid4()}), h.projects[0].id, run.id
        )
    with pytest.raises(AuthorizationError):
        h.service.journal.backfill(
            h.actor.model_copy(update={"scopes": h.actor.scopes - {"jobs:write"}}),
            h.projects[0].id,
            run.id,
        )


def test_maximum_sized_captured_evidence_remains_pageable_without_wrapper_overflow(store, tmp_path):
    from simon.services.worker_context import MAX_EVIDENCE_CHARS

    h = output_setup(store, tmp_path)
    run, task, executor = running(h)
    payload = {"invocation_id": str(uuid4()), "output": {"text": ""}}
    header = json.dumps(payload, separators=(",", ":"))
    payload["output"]["text"] = "x" * (MAX_EVIDENCE_CHARS - len(header))
    content = json.dumps(payload, separators=(",", ":"))
    assert len(content) == MAX_EVIDENCE_CHARS
    artifact = h.service.journal.evidence.publish_text(
        workspace_id=h.actor.workspace_id,
        actor_id=h.actor.actor_id,
        run_id=run.id,
        task_id=task.task_id,
        name="full-size.json",
        media_type="application/json",
        text=content,
    )
    entry = h.service.journal.append(
        h.actor,
        run.id,
        task,
        executor_id=executor,
        project_id=h.projects[0].id,
        kind="evidence",
        step=1,
        payload={},
        artifact=artifact,
    )
    page = h.service.journal.read(
        h.actor,
        h.projects[0].id,
        run.id,
        entry.id,
        pointer="/output/text",
        offset=len(payload["output"]["text"]) - 10,
        limit=10,
    )
    assert page.text == "x" * 10 and page.eof and page.next_offset is None
