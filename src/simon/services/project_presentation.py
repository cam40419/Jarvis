"""Readable project outcomes derived from authorized durable run snapshots.

Presentation never validates, approves or executes a model-authored plan. The
project cycle remains authoritative even when its planning model call succeeded.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from simon.domain.agent_runs import AgentRun, TaskExecution
from simon.domain.project_work import ProjectWorkState

_PHASE_LABELS = {
    "idle": "Ready for a request",
    "starting": "Request queued",
    "planning": "Planning your request",
    "ready": "Plan ready for review",
    "executing": "Working on your request",
    "completed": "Completed",
    "waiting": "Waiting for input",
    "blocked": "Needs attention",
    "unknown": "Outcome needs review",
    "cancelled": "Stopped",
}

_INVALID_PLAN = "The lead returned an invalid plan. Review its output."
_TASK_FAILURES = {
    "incomplete_worker_output": (
        "The task stopped with an unfinished deliverable after one completion repair. "
        "The required outcome has not met the task's acceptance criteria."
    ),
    "invalid_completion_review": (
        "Simon could not validate the completion review against the recorded evidence. "
        "Task completion could not be confirmed."
    ),
    "completion_review_unavailable": (
        "The agent reached its available step or context limit before it could finish "
        "checking or repairing the deliverable. Task completion could not be confirmed."
    ),
    "invalid_controller_response": (
        "The model returned a response Simon could not interpret as a tool action or final answer."
    ),
    "model_output_truncated": "The model's reply was cut off before a complete response arrived.",
    "model_refused": "The model provider declined to generate a response for this task.",
    "model_output_limit": "The model's reply exceeded this agent's response size limit.",
    "worker_input_limit": (
        "The task's instructions and tool context exceeded this agent's input limit."
    ),
    "invalid_tool_arguments": (
        "The model supplied inputs that did not match the tool's required format."
    ),
    "invalid_json_output": (
        "The model's final response did not match this agent's required JSON format."
    ),
    "empty_worker_output": "The model returned an empty response.",
    "tool_not_authorized": (
        "A requested tool was outside the access granted to this agent or account."
    ),
    "tool_action_not_authorized": (
        "A selected tool action was not permitted by this agent's action policy."
    ),
    "tool_unavailable": "A selected tool was unavailable.",
    "tool_transport_unavailable": "A selected tool's connection to this server was unavailable.",
    "tool_execution_failed": "A tool call failed before this task could finish.",
    "authorization_or_configuration_changed": (
        "The agent's access or configuration changed during this task."
    ),
    "worker_tool_limit": "The agent reached its tool-call limit before finishing.",
    "worker_step_limit": "The agent reached its model-step limit before finishing.",
    "worker_timeout": "This task exceeded its allowed running time.",
    "environment_capacity_unavailable": (
        "The required execution environment had no available capacity."
    ),
    "artifact_integrity_or_handoff_failed": (
        "A required file could not be verified or passed between tasks."
    ),
    "invalid_prompt_encoding": "The task's instructions could not be encoded for the model.",
    "invalid_output_encoding": "The model's response could not be saved as valid text.",
}
_COMPLETION_FAILURES = frozenset(
    {"incomplete_worker_output", "invalid_completion_review", "completion_review_unavailable"}
)


def _review_line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value[:limit].split())


def _completion_review(task: TaskExecution) -> dict[str, Any] | None:
    """Only the latest review of this exact saved candidate may describe its omissions."""
    event = next(
        (item for item in reversed(task.events) if item.get("event") == "completion_review"),
        None,
    )
    if event is None or event.get("status") not in ("partial", "not_delivered"):
        return None
    if task.output:
        try:
            digest = hashlib.sha256(task.output.encode("utf-8")).hexdigest()
        except UnicodeError:
            return None
        if event.get("candidate_sha256") != digest:
            return None
    summary = _review_line(event.get("summary"), 1000)
    unmet = event.get("unmet_requirements")
    requirements = list(
        dict.fromkeys(
            text
            for value in (unmet[:5] if isinstance(unmet, list) else [])
            if (text := _review_line(value, 300))
        )
    )
    if not summary and not requirements:
        return None
    return {"status": event["status"], "summary": summary, "unmet_requirements": requirements}


def _completion_explanation(task: TaskExecution) -> tuple[str, dict[str, Any] | None]:
    text = _TASK_FAILURES[task.error_code or ""]
    review = _completion_review(task)
    if review is not None:
        if review["summary"]:
            text += "\n\nCompletion review: " + review["summary"]
        if review["unmet_requirements"]:
            text += "\n\nStill needed:\n" + "\n".join(
                "- " + requirement for requirement in review["unmet_requirements"]
            )
    return text, review


def _failure_response(
    run: AgentRun, task: TaskExecution, *, planning: bool, can_retry: bool
) -> dict[str, Any]:
    unknown = task.status == "unknown"
    if unknown:
        message = (
            "Simon could not confirm the outcome of this task. "
            "Review the recorded activity and any external actions before starting more work."
        )
    elif task.status == "cancelled":
        message = "This task was cancelled before an answer was saved."
    else:
        message = _TASK_FAILURES.get(
            task.error_code or "",
            "This task could not finish. Its recorded error is available under technical details.",
        )
        message += " No answer was saved for this task."
        if task.error_code in _COMPLETION_FAILURES:
            explanation, _review = _completion_explanation(task)
            message = explanation + "\n\nNo answer was saved for this task."
    if task.tool_calls == 0 and not any(
        event.get("event") in {"tool_dispatch", "tool_complete"} for event in task.events
    ):
        message += " No tool calls were recorded for this task."
    if can_retry and not unknown:
        message += " Use Retry with current access to make a fresh plan."
    timestamp = run.finished_at or run.started_at
    return {
        "kind": "error",
        "source": "system",
        "title": "Outcome needs review"
        if unknown
        else "Planning could not finish"
        if planning
        else "Task could not finish",
        "text": message,
        "run_id": str(run.id),
        "task_id": task.id,
        "phase": "planning" if planning else "execution",
        "created_at": timestamp.isoformat() if timestamp else None,
        "failure": {
            "code": task.error_code,
            "status": task.status,
            "steps": task.steps,
            "tool_calls": task.tool_calls,
        },
    }


def project_presentation(
    state: ProjectWorkState,
    runs: Sequence[AgentRun],
    *,
    blockers: Sequence[str],
    recovery: dict[str, Any],
) -> dict[str, Any]:
    cycle = state.active_cycle or state.last_cycle
    phase = cycle.phase if cycle else "idle"
    label = _PHASE_LABELS[phase]
    if state.autonomy.paused and phase in {"idle", "starting", "planning", "ready", "executing"}:
        label = "Paused"
    response: dict[str, Any] | None = None
    by_id = {run.id: run for run in runs}
    execution = by_id.get(cycle.execution_run_id) if cycle and cycle.execution_run_id else None
    planning = by_id.get(cycle.planning_run_id) if cycle and cycle.planning_run_id else None
    run = execution or planning
    completion_failures = (
        [
            task
            for task in execution.tasks
            if task.status == "failed" and task.error_code in _COMPLETION_FAILURES
        ]
        if execution is not None
        else []
    )
    if run is not None:
        completed = [task for task in run.tasks if task.output.strip()]
        if completed:
            task = next((task for task in completed if task.id == "lead-summary"), completed[-1])
            incomplete = [item for item in completion_failures if item.output.strip()]
            if incomplete:
                task = next(
                    (item for item in incomplete if item.id == "lead-summary"), incomplete[0]
                )
            text = task.output
            kind = "answer" if phase == "completed" and execution else "partial"
            title = "Project answer" if kind == "answer" else "Work so far"
            if execution is None:
                kind, title = "plan", "Lead response"
                # Even an invalid plan can contain a useful explanation. Display
                # only its text here; compilation still uses strict validation.
                try:
                    value = json.loads(text) if len(text) <= 64000 else None
                except (ValueError, TypeError, RecursionError):
                    value = None
                if (
                    isinstance(value, dict)
                    and isinstance(value.get("summary"), str)
                    and value["summary"].strip()
                ):
                    text = value["summary"]
                    if value.get("status") == "waiting":
                        kind, title = "needs_input", "The lead needs more information"
                    elif value.get("status") == "complete" and phase == "completed":
                        kind, title = "answer", "Lead response"
                elif isinstance(value, (dict, list)) or text.lstrip().startswith(("{", "[", "```")):
                    text = (
                        "The lead could not produce a readable plan for this request. "
                        "The technical planning record contains the original response."
                    )
                if phase in {"blocked", "unknown"} and (
                    kind == "plan" or (cycle and cycle.error == _INVALID_PLAN)
                ):
                    kind, title = "error", "Planning needs attention"
            timestamp = run.finished_at or run.started_at
            response = {
                "kind": kind,
                "title": title,
                "text": text,
                "run_id": str(run.id),
                "task_id": task.id,
                "phase": "execution" if execution else "planning",
                "created_at": timestamp.isoformat() if timestamp else None,
            }
            if task in completion_failures:
                explanation, review = _completion_explanation(task)
                response.update(
                    {
                        "kind": "partial",
                        "title": "Partial response - needs completion"
                        if task.error_code == "incomplete_worker_output"
                        else "Partial response - completion unverified",
                        "text": explanation + "\n\n### Saved partial response\n\n" + task.output,
                        "failure": {
                            "code": task.error_code,
                            "status": task.status,
                            "steps": task.steps,
                            "tool_calls": task.tool_calls,
                        },
                        "completion_review": review,
                    }
                )
        else:
            failures = [
                task
                for task in run.tasks
                if task.status in {"failed", "unknown", "blocked", "cancelled"}
            ]
            if failures:
                task = next(
                    (item for item in failures if item.status == "unknown"),
                    next((item for item in failures if item.status == "failed"), failures[0]),
                )
                response = _failure_response(
                    run,
                    task,
                    planning=execution is None,
                    can_retry=bool(recovery.get("can_retry")),
                )
    return {
        "phase": phase,
        "status_label": label,
        "request": state.active_cycle.instruction
        if state.active_cycle
        else recovery.get("instruction") or (cycle.instruction if cycle else ""),
        "response": response,
        "blockers": list(
            dict.fromkeys(
                "The lead's planning response was inconsistent, so delegated work did not start. "
                "Retry to make a fresh plan with the current team access."
                if reason == _INVALID_PLAN
                else reason
                for reason in (
                    *blockers,
                    *(_TASK_FAILURES[task.error_code or ""] for task in completion_failures[:5]),
                )
            )
        ),
        "recovery": recovery,
    }
