"""Real artifact bytes and local copies remain project/owner scoped in both stores."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.artifacts import ArtifactError
from simon.domain.context import ExplicitMemory
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.identity import Membership
from simon.domain.models import JobStatus
from simon.domain.project_outputs import PromoteProjectOutput
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.project_outputs import ProjectOutputService
from tests.contract.test_local_files import local_setup
from tests.contract.test_project_history import PRIVATE_TEXT, saved_run


def output_setup(store, tmp_path):
    connected, actor, files, _ = local_setup(store, tmp_path)
    projects = tuple(ExplicitMemory(
        household_id=actor.household_id, created_by=actor.actor_id, category="project",
        subject=f"Project {index}", content="Project files", scope="personal",
    ) for index in range(2))
    for project in projects:
        store.insert_memory(project)
    team = TeamTemplate(id="studio", name="Studio", agent_ids=("writer",))
    platform = AgentPlatformService(store, PlatformManifest(
        agents=(AgentProfile(id="writer", instructions="Write."),), teams=(team,),
    ), state_dir=tmp_path / "agents", environ={})
    platform.project_visibility_resolver = connected.projects.project
    platform.project_team_resolver = lambda *_: team
    runs = AgentRunService(platform)
    service = ProjectOutputService(runs, files)
    return SimpleNamespace(
        actor=actor, projects=projects, store=store, files=files, connected=connected,
        platform=platform, runs=runs, service=service,
    )


@pytest.fixture
def outputs(store, tmp_path):
    return output_setup(store, tmp_path)


def create_output(h, number=1, project=0, *, actor=None, count=1):
    run = saved_run(h.store, h.projects[project].id, number=number,
                    state_dir=h.platform.state_dir, actor=actor or h.actor)
    if count > 1:
        artifact = run.tasks[0].artifacts[0]
        artifacts = (artifact, *(h.service.artifacts.publish_bytes(
            workspace_id=artifact.workspace_id, actor_id=artifact.actor_id, run_id=run.id,
            task_id=artifact.task_id, name=f"report-{index}.txt", media_type="text/plain",
            content=f"Deliverable {number}/{index}".encode(),
        ) for index in range(1, count)))
        run = run.model_copy(update={"tasks": (run.tasks[0].model_copy(update={
            "artifacts": artifacts,
        }),)})
        replace_run(h, run)
    return run


def replace_run(h, run):
    job = h.store.get_job(run.id)
    h.store.save_job(job.model_copy(update={
        "input": {**job.input, "initial_state": run.model_dump(mode="json")},
        "result": run.model_dump(mode="json"),
    }), job.version)


def test_outputs_page_within_run_and_across_runs_does_not_scan_unrelated_jobs(outputs, monkeypatch):
    h = outputs
    for number in (1, 2, 3):
        create_output(h, number, count=3)
    create_output(h, 4, project=1)
    monkeypatch.setattr(h.store, "jobs", lambda *_: pytest.fail("Global job scan"))
    page = h.service.list(h.actor, h.projects[0].id, limit=2)
    assert page.can_promote and page.next_cursor and len(page.items) == 2
    assert PRIVATE_TEXT not in page.model_dump_json()
    items = list(page.items)
    cursor = page.next_cursor
    while cursor:
        page = h.service.list(h.actor, h.projects[0].id, limit=2, cursor=cursor)
        items.extend(page.items)
        cursor = page.next_cursor
    assert [item.run_id.int for item in items] == [3] * 3 + [2] * 3 + [1] * 3
    assert len({item.id for item in items}) == 9
    assert all(item.download_url.endswith(str(item.id)) for item in items)
    first = h.service.list(h.actor, h.projects[0].id, limit=1)
    with pytest.raises(ValidationError, match="cursor"):
        h.service.list(h.actor, h.projects[1].id, cursor=first.next_cursor)
    for cursor in ("bad!!", "e30", "a", "x" * 769):
        with pytest.raises(ValidationError, match="cursor"):
            h.service.list(h.actor, h.projects[0].id, cursor=cursor)


def test_save_copy_replay_edits_and_revision_conflict_keep_original(outputs):
    h = outputs
    run = create_output(h)
    artifact = run.tasks[0].artifacts[0]
    request = PromoteProjectOutput(idempotency_key="save-output-once")
    args = (h.actor, h.projects[0].id, run.id, artifact.id)
    saved = h.service.promote(*args, request, lambda: h.actor)
    copy = saved.project_copy
    assert copy and copy.root == f"project:{h.projects[0].id}"
    target = h.files.path(h.actor, copy.root, copy.path)
    assert target.read_bytes() == b"Result"
    assert h.service.promote(*args, request, lambda: h.actor) == saved
    assert h.service.list(h.actor, h.projects[0].id).items[0].project_copy == copy
    assert len([event for event in h.store.audit_events(h.actor.household_id)
                if event.event_type == "project.output_saved"]) == 1
    # The copy receipt survives an independent service (also verifies PG input persistence).
    service = ProjectOutputService(h.runs, h.files)
    assert service.list(h.actor, h.projects[0].id).items[0].project_copy == copy
    edited = h.files.publish(h.actor, copy.root, copy.path, b"User edit", copy.revision)
    assert h.service.promote(*args, request, lambda: h.actor) == saved
    assert target.read_bytes() == b"User edit"
    with pytest.raises(ValidationError, match="already exists"):
        h.service.promote(*args, PromoteProjectOutput(idempotency_key="different-save"),
                          lambda: h.actor)
    replaced = h.service.promote(*args, PromoteProjectOutput(
        idempotency_key="reviewed-replacement", revision=edited["revision"],
    ), lambda: h.actor)
    assert replaced.project_copy.revision == artifact.sha256
    assert target.read_bytes() == h.service.artifacts.read(artifact) == b"Result"


def test_only_authorized_successful_project_outputs_can_be_promoted(outputs):
    h = outputs
    run = create_output(h)
    artifact = run.tasks[0].artifacts[0]
    request = PromoteProjectOutput(idempotency_key="scope-bound-save")
    with pytest.raises(NotFoundError):
        h.service.promote(h.actor, h.projects[1].id, run.id, artifact.id, request, lambda: h.actor)
    outsider = h.actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises((AuthorizationError, NotFoundError)):
        h.service.list(outsider, h.projects[0].id)
    revoked = h.actor.model_copy(update={"scopes": h.actor.scopes - {"jobs:write"}})
    assert not h.service.list(revoked, h.projects[0].id).can_promote
    with pytest.raises(AuthorizationError):
        h.service.promote(h.actor, h.projects[0].id, run.id, artifact.id, request, lambda: revoked)
    for path in ("../escape.txt", ".git/config", "/host/path", "nested/../../escape"):
        with pytest.raises((AuthorizationError, ValidationError)):
            h.service.promote(h.actor, h.projects[0].id, run.id, artifact.id,
                              request.model_copy(update={"path": path}), lambda: h.actor)
    # Successful work remains useful when another task made the overall run fail.
    replace_run(h, run.model_copy(update={"status": JobStatus.FAILED}))
    assert len(h.service.list(h.actor, h.projects[0].id).items) == 1
    replace_run(h, run.model_copy(update={"tasks": (run.tasks[0].model_copy(update={
        "status": "unknown",
    }),)}))
    assert not h.service.list(h.actor, h.projects[0].id).items
    with pytest.raises(NotFoundError):
        h.service.promote(h.actor, h.projects[0].id, run.id, artifact.id, request, lambda: h.actor)


def test_lost_copy_receipt_and_tampered_artifact_are_safe(outputs):
    h = outputs
    run = create_output(h)
    item = h.service.list(h.actor, h.projects[0].id).items[0]
    path = f"outputs/{item.id.hex[:8]}-{item.name}"
    root = f"project:{h.projects[0].id}"
    h.files.publish(h.actor, root, path, b"Result")
    saved = h.service.promote(h.actor, h.projects[0].id, run.id, item.id,
                             PromoteProjectOutput(idempotency_key="recover-copy-receipt"),
                             lambda: h.actor)
    assert saved.project_copy.path == path
    # Tampering is detected before any new editable copy is created.
    artifact = run.tasks[0].artifacts[0]
    location = h.service.artifacts.root / str(artifact.workspace_id) / str(artifact.actor_id)
    files = list(location.rglob("content"))
    assert len(files) == 1
    files[0].write_bytes(b"Tampered")
    with pytest.raises(ArtifactError):
        h.service.promote(h.actor, h.projects[0].id, run.id, item.id,
                         PromoteProjectOutput(path="other.txt", idempotency_key="tampered-source"),
                         lambda: h.actor)
    assert not h.files.path(h.actor, root, "other.txt").exists()


def test_shared_project_does_not_grant_another_accounts_artifacts(outputs):
    h = outputs
    shared = ExplicitMemory(
        household_id=h.actor.household_id, created_by=h.actor.actor_id, category="project",
        subject="Shared project", content="Shared context, private execution outputs",
        scope="household",
    )
    h.store.insert_memory(shared)
    run = saved_run(h.store, shared.id, actor=h.actor, state_dir=h.platform.state_dir)
    other = h.actor.model_copy(update={"actor_id": uuid4()})
    h.store.put_membership(Membership(
        actor_id=other.actor_id, household_id=other.household_id, role="owner",
    ))
    assert h.connected.projects.project(other, shared.id).id == shared.id
    assert h.service.list(other, shared.id).items == ()
    artifact = run.tasks[0].artifacts[0]
    with pytest.raises(NotFoundError):
        h.service.read(other, shared.id, run.id, artifact.id)
    with pytest.raises(NotFoundError):
        h.service.promote(other, shared.id, run.id, artifact.id,
                         PromoteProjectOutput(idempotency_key="other-owner-copy"), lambda: other)


def test_empty_page_advances_after_bounded_runs_without_successful_outputs(outputs):
    h = outputs
    oldest = create_output(h)
    for number in range(2, 53):
        run = saved_run(h.store, h.projects[0].id, actor=h.actor, number=number)
        replace_run(h, run.model_copy(update={"tasks": (run.tasks[0].model_copy(update={
            "status": "unknown", "artifacts": (),
        }),)}))
    page = h.service.list(h.actor, h.projects[0].id)
    assert not page.items and page.next_cursor
    next_page = h.service.list(h.actor, h.projects[0].id, cursor=page.next_cursor)
    assert [item.run_id for item in next_page.items] == [oldest.id]
    assert next_page.next_cursor is None
