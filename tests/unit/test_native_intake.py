"""Native intake authority, inference accounting and atomic team/board application."""

import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import httpx
import pytest

from simon.adapters.memory import InMemoryStore
from simon.domain.accounts import ManagedAccount
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_agents import UpdateNativeTeamPolicy
from simon.domain.native_intake import AnalyzeIntake, IntakeSource, SourceUpload, UpdateNativeIntake
from simon.domain.native_projects import (
    CreateNativeTask,
    NativeProjectMember,
    UpdateNativeProject,
    VersionedNativeCommand,
)
from simon.services.identity import ROLE_SCOPES
from simon.services.intake_planner import IntakePlanner
from simon.services.intake_sources import IntakeSourceBytes
from simon.services.native_intake import NativeIntakeService, configured_planner
from tests.unit.test_intake_planner import endpoint, wire_result
from tests.unit.test_native_teams import create_role, issue, setup_team


def role_document(key="researcher", **changes):
    return {
        "role_key": key,
        "action": "create",
        "name": key.title(),
        "instructions": "Compare source evidence and prepare recommendations for review.",
        "success_criteria": "The recommendation cites evidence and exposes uncertainty.",
        "rationale": "The next milestone needs sustained source reconciliation.",
        "agent_id": None,
        "need": "specialist",
        "reuse_assessment": "There is no active role covering this responsibility.",
        **changes,
    }


def task_document(key="charter", **changes):
    return {
        "key": key,
        "title": "Prepare the provisional project charter",
        "description": "Reconcile project objectives and source evidence.",
        "acceptance": "Each claim has evidence or is explicitly labelled an assumption.",
        "assignment": "agent",
        "role_key": "researcher",
        "review_role_key": None,
        "existing_task_id": None,
        **changes,
    }


def setup_intake(tmp_path, *, free=False):
    h = setup_team(InMemoryStore())
    h.calls = []
    h.hook = None
    h.usage = {"input_tokens": 100, "output_tokens": 40}
    h.proposal = {
        "summary": "Create a source-backed charter and expose unresolved owner decisions.",
        "next_milestone": "Owner-reviewed provisional charter",
        "findings": [],
        "questions": [],
        "roles": [role_document()],
        "tasks": [task_document()],
    }
    h.review = {"approved": True, "issues": []}

    def respond(request):
        h.calls.append(request)
        if h.hook:
            h.hook(request, len(h.calls))
        review = "Independently review a proposed project" in request.content.decode()
        document = h.review if review else h.proposal
        text = document if isinstance(document, str) else json.dumps(document)
        return httpx.Response(200, json=wire_result("openai_compatible", text, **h.usage))

    h.planner = IntakePlanner(
        [
            endpoint(
                input_cost_per_million_usd=0 if free else 1,
                output_cost_per_million_usd=0 if free else 2,
            )
        ],
        environ={},
        transport=httpx.MockTransport(respond),
    )
    h.intake = NativeIntakeService(
        h.store,
        h.projects,
        h.teams,
        IntakeSourceBytes(tmp_path / "sources"),
        lambda _: h.planner,
    )
    configure(h)
    return h


@pytest.fixture
def intake(tmp_path):
    return setup_intake(tmp_path)


def configure(h, **changes):
    current = h.store.native_intake(h.workspace, h.first.id)
    values = (
        current.model_dump(exclude={"workspace_id", "project_id", "version", "updated_at"})
        if current
        else {"endpoint_id": "planning", "budget_microusd": 1_000_000}
    )
    return h.intake.update(
        h.owner_actor,
        h.first.id,
        UpdateNativeIntake(
            **{
                **values,
                "expected_version": current.version if current else 0,
                "idempotency_key": f"configure-{uuid4()}",
                **changes,
            }
        ),
    )


def command(h, **changes):
    current = h.store.native_intake(h.workspace, h.first.id)
    return AnalyzeIntake(
        **{
            "expected_version": current.version,
            "idempotency_key": f"analyze-{uuid4()}",
            **changes,
        }
    )


def upload(h, content=b"Source evidence.", key="brief.txt", **changes):
    return h.intake.upload(
        h.owner_actor,
        h.first.id,
        SourceUpload(
            **{
                "source_key": key,
                "filename": key.rsplit("/", 1)[-1],
                "media_type": "text/plain",
                "content_base64": base64.b64encode(content).decode(),
                "expected_version": h.store.native_intake(h.workspace, h.first.id).version,
                "idempotency_key": f"upload-{uuid4()}",
                **changes,
            }
        ),
    )


def analyze(h, **changes):
    return h.intake.analyze(h.owner_actor, h.first.id, command(h, **changes))


def mutate_command(version, prefix="mutate"):
    return VersionedNativeCommand(expected_version=version, idempotency_key=f"{prefix}-{uuid4()}")


def test_real_planner_review_autostaff_assigns_work_and_separate_human_review(intake):
    h = intake
    source = upload(h, b"Small independent clothing label.")
    h.proposal["findings"] = [
        {
            "kind": "fact",
            "statement": "The project is an independent label.",
            "evidence": [{"source_id": str(source.id), "quote": "independent clothing label"}],
        }
    ]
    request = command(h)
    run = h.intake.analyze(h.owner_actor, h.first.id, request)
    assert run.status == "applied"
    assert len(h.calls) == 2
    assert run.charged_microusd == 360 and run.reserved_microusd == 0
    assert run.input_tokens == 200 and run.output_tokens == 80
    assert run.included_source_ids == (source.id,)
    agents = h.store.native_agents(h.workspace, h.first.id, 0, 100)
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    assert len(agents) == 1 and not agents[0].can_manage_team
    assert len(tasks) == 2
    main = next(task for task in tasks if not task.title.startswith("Review:"))
    review = next(task for task in tasks if task.title.startswith("Review:"))
    assert main.assignment.agent_id == agents[0].id
    assert review.assignment.actor_id == h.owner
    assert "Acceptance criteria:" in main.description
    assert str(main.id) in review.description
    assert h.intake.analyze(h.owner_actor, h.first.id, request) == run
    assert len(h.calls) == 2
    assert h.intake.view(h.owner_actor, h.first.id)["spent_microusd"] == 360


def test_reuses_existing_roles_and_work_without_rewriting_manual_records(intake):
    h = intake
    existing = create_role(h, key="researcher")
    old_task = h.projects.create_task(
        h.owner_actor,
        h.first.id,
        CreateNativeTask(title="Already planned work", idempotency_key="existing-task"),
    )
    h.proposal["roles"] = [
        role_document(
            action="reuse",
            agent_id=str(existing.id),
            need="reuse",
            instructions="Model text must not overwrite owner instructions.",
        )
    ]
    h.proposal["tasks"].append(task_document("existing", existing_task_id=str(old_task.id)))
    run = analyze(h)
    assert run.status == "applied"
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == (existing,)
    assert h.store.native_task(h.workspace, h.first.id, old_task.id) == old_task
    assert old_task.id in run.applied_task_ids
    assert len(h.store.native_tasks(h.workspace, h.first.id, 0, 100)) == 3


def test_manual_mode_applies_once_after_review_and_replay_checks_current_authority(intake):
    h = intake
    configure(h, auto_staff=False)
    run = analyze(h)
    assert run.status == "ready"
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()
    apply_command = mutate_command(run.version, "apply")
    applied = h.intake.apply(h.owner_actor, h.first.id, run.id, apply_command)
    assert applied.status == "applied"
    assert h.intake.apply(h.owner_actor, h.first.id, run.id, apply_command) == applied
    h.store.save_managed_account(
        ManagedAccount(
            actor_id=h.owner,
            workspace_id=h.workspace,
            invited_by=h.owner,
            display_name="Disabled owner",
            disabled=True,
        )
    )
    with pytest.raises(AuthorizationError):
        h.intake.apply(h.owner_actor, h.first.id, run.id, apply_command)


@pytest.mark.parametrize(
    "case",
    [
        "cap",
        "idle-role",
        "duplicate-role",
        "duplicate-task",
        "foreign-reuse",
        "unsupported-citation",
        "unseen-quote",
    ],
)
def test_semantic_guard_rejects_unnecessary_or_unsupported_work_before_reviewer(intake, case):
    h = intake
    if case == "cap":
        create_role(h, key="existing")
        h.teams.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateNativeTeamPolicy(
                expected_version=0,
                max_active_agents=1,
                agents_can_manage_team=True,
                idempotency_key="one-role-policy",
            ),
        )
    elif case == "idle-role":
        h.proposal["roles"].append(role_document("idle"))
    elif case == "duplicate-role":
        create_role(h, key="researcher")
    elif case == "duplicate-task":
        h.projects.create_task(
            h.owner_actor,
            h.first.id,
            CreateNativeTask(
                title=h.proposal["tasks"][0]["title"], idempotency_key="same-title-task"
            ),
        )
    elif case == "foreign-reuse":
        h.proposal["roles"] = [role_document(action="reuse", agent_id=str(uuid4()), need="reuse")]
    else:
        source = upload(h, b"First excerpt." + b" " * 3100 + b"Hidden later quote.")
        h.proposal["findings"] = [
            {
                "kind": "fact",
                "statement": "An invented fact.",
                "evidence": [
                    {
                        "source_id": str(uuid4() if case == "unsupported-citation" else source.id),
                        "quote": "First excerpt."
                        if case == "unsupported-citation"
                        else "Hidden later quote.",
                    }
                ],
            }
        ]
    before_roles = h.store.native_agents(h.workspace, h.first.id, 0, 100)
    before_tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    run = analyze(h)
    assert run.status == "needs_revision"
    assert run.error_code == "proposal_invalid"
    assert run.charged_microusd == 180 and run.reserved_microusd == 0
    assert len(h.calls) == 1
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == before_roles
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == before_tasks


def test_model_cannot_inject_authority_fields_and_usage_still_settles(intake):
    h = intake
    h.proposal["roles"][0]["can_manage_team"] = True
    run = analyze(h)
    assert run.status == "failed" and run.error_code == "invalid_proposal_schema"
    assert run.charged_microusd == 180 and not run.reserved_microusd
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()


def test_review_rejection_preserves_findings_without_applying(intake):
    h = intake
    h.review = {
        "approved": False,
        "issues": ["The proposed role is not justified by near-term work."],
    }
    run = analyze(h)
    assert run.status == "needs_revision" and run.review.issues
    assert len(h.calls) == 2 and run.charged_microusd == 360
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == ()
    with pytest.raises(InvalidTransitionError):
        h.intake.apply(h.owner_actor, h.first.id, run.id, mutate_command(run.version))


def test_blocking_interview_questions_become_human_tasks_before_any_staffing(intake):
    h = intake
    h.proposal["questions"] = [
        {
            "key": "direction",
            "question": "Which direction should we explore?",
            "why": "Source material conflicts.",
            "blocking": True,
        }
    ]
    run = analyze(h)
    assert run.status == "questions"
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()
    with pytest.raises(InvalidTransitionError):
        h.intake.apply(h.owner_actor, h.first.id, run.id, mutate_command(run.version))
    assert len(h.store.native_tasks(h.workspace, h.first.id, 0, 100)) == 1
    decision = next(
        t
        for t in h.store.native_tasks(h.workspace, h.first.id, 0, 100)
        if t.title.startswith("Decision:")
    )
    assert decision.assignment.actor_id == h.owner


@pytest.mark.parametrize("change", ["intake", "source", "board", "archive"])
def test_context_changed_during_generation_stops_review_and_preserves_charge(intake, change):
    h = intake

    def change_context(_request, number):
        assert number == 1
        if change == "intake":
            configure(h, background="Owner changed the direction.")
        elif change == "source":
            upload(h, b"New evidence.")
        elif change == "board":
            h.projects.create_task(
                h.owner_actor,
                h.first.id,
                CreateNativeTask(title="Owner-created task", idempotency_key="mid-call-task"),
            )
        else:
            h.projects.update_project(
                h.owner_actor,
                h.first.id,
                UpdateNativeProject(
                    expected_version=h.first.version,
                    name=h.first.name,
                    objective=h.first.objective,
                    status="archived",
                    idempotency_key="archive-mid-call",
                ),
            )

    h.hook = change_context
    if change == "archive":
        with pytest.raises(AuthorizationError):
            analyze(h)
        run = h.store.native_intake_runs(h.workspace, h.first.id)[0]
    else:
        run = analyze(h)
    assert run.status == "stale" and run.proposal is None
    assert run.charged_microusd == 180 and run.reserved_microusd == 0
    assert len(h.calls) == 1 and h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()


def test_authority_loss_during_call_does_not_rollback_usage_settlement(intake):
    h = intake

    def revoke(_request, number):
        assert number == 1
        h.store.save_managed_account(
            ManagedAccount(
                actor_id=h.owner,
                workspace_id=h.workspace,
                invited_by=h.owner,
                display_name="Disabled owner",
                disabled=True,
            )
        )

    h.hook = revoke
    with pytest.raises(AuthorizationError):
        analyze(h)
    run = h.store.native_intake_runs(h.workspace, h.first.id)[0]
    assert run.status == "stale" and run.error_code == "planning_authority_changed"
    assert run.charged_microusd == 180 and run.reserved_microusd == 0
    assert len(h.calls) == 1


def test_cancellation_and_concurrent_key_replay_never_redispatch_or_apply(intake):
    h = intake
    started, release = Event(), Event()

    def pause(_request, number):
        assert number == 1
        started.set()
        assert release.wait(10)

    h.hook = pause
    request = command(h)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.intake.analyze, h.owner_actor, h.first.id, request)
        try:
            assert started.wait(10)
            running = h.intake.analyze(h.owner_actor, h.first.id, request)
            assert running.status == "running" and running.reserved_microusd > 0
            with pytest.raises(InvalidTransitionError, match="already running"):
                analyze(h)
            with pytest.raises(InvalidTransitionError, match="allowance"):
                configure(h, budget_microusd=0)
            cancelled = h.intake.cancel(
                h.owner_actor, h.first.id, running.id, mutate_command(running.version)
            )
            assert cancelled.status == "cancelled"
            assert cancelled.reserved_microusd == running.reserved_microusd
        finally:
            release.set()
        settled = future.result(timeout=10)
    assert settled.status == "cancelled" and settled.proposal is None
    assert settled.charged_microusd == 180 and not settled.reserved_microusd
    assert len(h.calls) == 1 and h.store.native_tasks(h.workspace, h.first.id, 0, 100) == ()


@pytest.mark.parametrize("failure_at", [1, 2])
def test_unknown_provider_outcome_holds_full_reservation_and_idempotent_retry_does_not_call(
    intake, failure_at
):
    h = intake

    def timeout(request, number):
        if number == failure_at:
            raise httpx.ReadTimeout("private provider details", request=request)

    h.hook = timeout
    request = command(h)
    run = h.intake.analyze(h.owner_actor, h.first.id, request)
    assert run.status == "unknown" and run.reserved_microusd > 0
    assert run.charged_microusd == 0
    assert "private" not in run.model_dump_json()
    assert h.intake.analyze(h.owner_actor, h.first.id, request) == run
    assert len(h.calls) == failure_at
    configure(h, budget_microusd=run.reserved_microusd)
    with pytest.raises(InvalidTransitionError, match="allowance"):
        analyze(h)
    assert len(h.calls) == failure_at


def test_missing_usage_retains_liability_even_for_valid_proposal(intake):
    h = intake
    h.usage = {"input_tokens": None, "output_tokens": None}
    run = analyze(h)
    assert run.reserved_microusd > 0 and run.charged_microusd == 0
    assert run.input_tokens is None and run.output_tokens is None


def test_interrupted_deadline_is_unknown_and_never_replayed(intake):
    h = intake
    started, release = Event(), Event()

    def pause(_request, number):
        assert number == 1
        started.set()
        assert release.wait(10)

    h.hook = pause
    request = command(h)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.intake.analyze, h.owner_actor, h.first.id, request)
        try:
            assert started.wait(10)
            current = h.store.native_intake_runs(h.workspace, h.first.id)[0]
            # Simulate a persisted attempt from a restarted process; dispatch identity
            # is immutable to ordinary store updates, so install its prior clock here.
            h.store._native_intake_runs[current.id] = current.model_copy(
                update={
                    "started_at": utc_now() - timedelta(minutes=10),
                    "deadline_at": utc_now() - timedelta(minutes=5),
                }
            )
            reloaded = h.intake.analyze(h.owner_actor, h.first.id, request)
            assert reloaded.status == "unknown" and reloaded.reserved_microusd > 0
        finally:
            release.set()
        finished = future.result(timeout=10)
    assert finished.status == "unknown"
    assert len(h.calls) == 1


def test_latest_source_revision_revocation_never_falls_back_to_old_context(intake):
    h = intake
    first = upload(h, b"Old source text.")
    latest = upload(h, b"Replacement source text.")
    assert latest.revision == 2 and first.revision == 1
    version = h.store.native_intake(h.workspace, h.first.id).version
    h.intake.revoke(h.owner_actor, h.first.id, latest.id, mutate_command(version))
    with pytest.raises(NotFoundError):
        h.intake.read_source(h.owner_actor, h.first.id, latest.id)
    assert h.intake.read_source(h.owner_actor, h.first.id, first.id)[1] == b"Old source text."
    with pytest.raises(ValidationError, match="current"):
        analyze(h, source_ids=(first.id,))
    run = analyze(h)
    assert run.included_source_ids == ()
    assert "Old source text" not in h.calls[0].content.decode()
    assert "Replacement source text" not in h.calls[0].content.decode()


def test_redaction_applies_to_nested_answers_sources_and_role_context_without_breaking_json(intake):
    h = intake
    secret = "sk-" + "a" * 32
    configure(
        h,
        answers={"launch": f"api_key={secret}\nKeep the brand calm."},
        background="password=verysecretvalue\nA genuine project brief.",
    )
    source = upload(h, f"Bearer {'x' * 30}\nValid source evidence.".encode())
    assert source.redactions > 0
    run = analyze(h)
    assert run.status == "applied"
    for call in h.calls:
        raw = call.content.decode()
        assert secret not in raw and "verysecretvalue" not in raw and "x" * 30 not in raw
        assert "REDACTED CREDENTIAL" in raw and "genuine project brief" in raw
    metadata = h.intake.view(h.owner_actor, h.first.id)["sources"][0]
    assert "text" not in metadata


@pytest.mark.parametrize("limit", ["keys", "bytes", "revisions"])
def test_source_inventory_quotas_cover_all_revisions_including_revoked(intake, limit):
    h = intake
    count = {"keys": 500, "bytes": 20, "revisions": 2000}[limit]
    for index in range(count):
        source = IntakeSource(
            workspace_id=h.workspace,
            project_id=h.first.id,
            source_key=f"file-{index}.txt" if limit != "revisions" else "one.txt",
            revision=index + 1 if limit == "revisions" else 1,
            filename="note.txt",
            media_type="text/plain",
            sha256="a" * 64,
            size_bytes=5 * 1024 * 1024 if limit == "bytes" else 1,
            extraction_status="text",
            created_by=h.owner,
        )
        h.store.insert_native_intake_source(source)
    with pytest.raises(ValidationError, match="capacity"):
        upload(h, b"new", key="new-key.txt")
    assert h.store.native_intake(h.workspace, h.first.id).version == 1


def test_member_reads_but_cannot_change_or_spend_and_scoped_agent_cannot_read(intake):
    h = intake
    h.store.put_native_project_member(
        NativeProjectMember(workspace_id=h.workspace, project_id=h.first.id, actor_id=h.member)
    )
    member = ActorContext(
        actor_id=h.member,
        workspace_id=h.workspace,
        scopes=ROLE_SCOPES["member"],
        channel=Channel.API,
    )
    assert not h.intake.view(member, h.first.id)["can_manage"]
    with pytest.raises(AuthorizationError):
        h.intake.analyze(member, h.first.id, command(h))
    agent = create_role(h)
    machine, _, _ = issue(h, agent)
    with pytest.raises(AuthorizationError):
        h.intake.view(machine, h.first.id)
    with pytest.raises(NotFoundError):
        h.intake.view(h.owner_actor, h.foreign.id)


def test_changed_body_with_reused_planning_key_conflicts_before_dispatch(intake):
    h = intake
    request = command(h)
    run = h.intake.analyze(h.owner_actor, h.first.id, request)
    configure(h, constraints="A revised constraint")
    with pytest.raises(IdempotencyConflictError):
        analyze(h, idempotency_key=request.idempotency_key)
    assert len(h.calls) == 2
    assert h.store.native_intake_run(h.workspace, h.first.id, run.id) == run


def test_catalog_is_admin_owned_workspace_scoped_and_fails_closed(tmp_path):
    workspace = uuid4()
    catalog = tmp_path / "model-catalog.json"
    rows = [{"workspace_ids": [str(workspace)], "endpoint": endpoint().model_dump(mode="json")}]
    catalog.write_text(json.dumps(rows), encoding="utf-8")
    assert configured_planner(catalog, workspace).options()[0]["id"] == "planning"
    assert configured_planner(catalog, uuid4()).options() == ()
    assert configured_planner(None, workspace).options() == ()
    invalid = copy.deepcopy(rows)
    invalid[0]["arbitrary"] = "not allowed"
    catalog.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValidationError, match="catalog"):
        configured_planner(catalog, workspace)


def test_composed_description_bound_stops_review_and_staffing_but_keeps_usage(intake):
    h = intake
    h.proposal["tasks"][0]["description"] = "x" * 7900
    h.proposal["tasks"][0]["acceptance"] = "y" * 1000
    run = analyze(h)
    assert run.status == "needs_revision"
    assert run.charged_microusd == 180 and run.reserved_microusd == 0
    assert len(h.calls) == 1
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == ()


def test_partial_application_failure_rolls_back_entire_team_and_board(intake, monkeypatch):
    h = intake
    create = h.projects.create_task
    writes = 0

    def fail_review_task(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise InvalidTransitionError("Injected review-task write conflict")
        return create(*args, **kwargs)

    monkeypatch.setattr(h.projects, "create_task", fail_review_task)
    run = analyze(h)
    assert run.status in {"stale", "needs_revision"}
    assert writes == 2
    assert run.charged_microusd == 360 and run.reserved_microusd == 0
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == ()
    assert not run.applied_agent_ids and not run.applied_task_ids


def test_provider_overreported_usage_remains_charged_and_stops_application(intake):
    h = intake
    h.usage = {"input_tokens": 1000000, "output_tokens": 1000000}
    run = analyze(h)
    assert run.status == "needs_revision"
    assert run.error_code == "provider_usage_exceeded_reservation"
    assert run.charged_microusd == 3000000 and not run.reserved_microusd
    assert len(h.calls) == 1
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()
    with pytest.raises(InvalidTransitionError, match="allowance"):
        analyze(h)


def test_repeated_unresolved_interview_question_reuses_open_decision_task(intake):
    h = intake
    h.proposal["questions"] = [
        {
            "key": "direction",
            "question": "Which creative direction should we explore?",
            "why": "The existing evidence conflicts.",
            "blocking": True,
        }
    ]
    first = analyze(h)
    second = analyze(h)
    assert first.status == second.status == "questions"
    assert len(h.calls) == 4
    decisions = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    assert len(decisions) == 1 and decisions[0].title.startswith("Decision:")
    assert first.applied_task_ids == second.applied_task_ids == (decisions[0].id,)
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()


@pytest.mark.parametrize("another_human", [False, True])
def test_human_work_never_assigns_independent_review_to_its_maker(intake, another_human):
    h = intake
    if another_human:
        h.store.put_native_project_member(
            NativeProjectMember(workspace_id=h.workspace, project_id=h.first.id, actor_id=h.member)
        )
    h.proposal["roles"] = []
    h.proposal["tasks"] = [task_document(assignment="human", role_key=None)]
    run = analyze(h)
    assert run.status == "applied"
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    maker = next(task for task in tasks if not task.title.startswith("Review:"))
    review = next(task for task in tasks if task.title.startswith("Review:"))
    assert maker.assignment.actor_id == h.owner
    assert review.assignment.actor_id != maker.assignment.actor_id
    if another_human:
        assert review.assignment.actor_id == h.member
    else:
        assert review.assignment.kind == "pool"


@pytest.mark.parametrize("interrupted", [False, True])
def test_source_revocation_during_review_never_restores_proposal_or_review(intake, interrupted):
    h = intake
    source = upload(h, b"A private quotation whose access may be revoked.")
    h.proposal["findings"] = [
        {
            "kind": "fact",
            "statement": "A source was available during intake.",
            "evidence": [{"source_id": str(source.id), "quote": "A private quotation"}],
        }
    ]

    def revoke_during_review(_request, number):
        if number != 2:
            return
        if interrupted:
            current = h.store.native_intake_runs(h.workspace, h.first.id)[0]
            h.intake._save_run(current, status="unknown", error_code="planning_interrupted")
        version = h.store.native_intake(h.workspace, h.first.id).version
        h.intake.revoke(h.owner_actor, h.first.id, source.id, mutate_command(version))

    h.hook = revoke_during_review
    run = analyze(h)
    assert run.status == ("unknown" if interrupted else "stale")
    assert run.proposal is None and run.review is None
    assert len(h.calls) == 2 and run.charged_microusd == 360
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == ()
    assert h.intake.get_run(h.owner_actor, h.first.id, run.id) == run


def test_catalog_url_swap_under_same_endpoint_identity_stops_second_dispatch(intake):
    h = intake

    def forbidden(_request):
        raise AssertionError("A replacement model catalog must not inherit an active reservation")

    def swap_catalog(_request, number):
        assert number == 1
        h.planner = IntakePlanner(
            [
                endpoint(
                    base_url="http://127.0.0.1:19998/v1",
                    input_cost_per_million_usd=1,
                    output_cost_per_million_usd=2,
                )
            ],
            environ={},
            transport=httpx.MockTransport(forbidden),
        )

    h.hook = swap_catalog
    run = analyze(h)
    assert run.status == "stale" and run.review is None
    assert run.charged_microusd == 180 and run.reserved_microusd == 0
    assert len(h.calls) == 1
    assert h.store.native_agents(h.workspace, h.first.id, 0, 100) == ()
