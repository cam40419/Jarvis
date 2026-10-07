"""Native project boards in a real browser, with isolated services and no AI calls."""

import os
import re
import socket
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from simon.api.app import AppContainer, create_app
from simon.api.auth import session_cookie
from simon.config import Settings
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID, Membership
from simon.domain.native_projects import (
    CreateNativeProject,
    CreateNativeTask,
    PutNativeProjectMember,
    UpdateNativeProject,
    UpdateNativeTask,
)
from simon.services.identity import csrf_token

pytestmark = pytest.mark.browser


@pytest.fixture
def native_ui(tmp_path, request, monkeypatch):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import sync_playwright

    prefix = getattr(request, "param", "")
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    container = AppContainer(
        settings=Settings(
            _env_file=None,
            environment="test",
            storage_backend="memory",
            model_provider="local",
            openai_api_key=None,
            agent_manifest_file=None,
            agent_execution_enabled=False,
            project_drive_sync_enabled=False,
            public_origin=origin,
            public_path=prefix,
            rp_id="127.0.0.1",
            dev_login_enabled=True,
            dev_login_token=SecretStr("test-development-secret-32-characters"),
            agent_state_dir=tmp_path / "agents",
            local_files_dir=tmp_path / "files",
            local_files_enabled=False,
        )
    )
    legacy_calls = []

    def unexpected_legacy_call(*args, **kwargs):
        legacy_calls.append((args, kwargs))
        raise AssertionError("Native boards must not invoke legacy projects or workers")

    for service, method in (
        (container.connected.projects, "create"),
        (container.connected.projects, "ensure"),
        (container.project_coordinator, "begin"),
        (container.agent_runs, "start"),
    ):
        monkeypatch.setattr(service, method, unexpected_legacy_call)
    server = uvicorn.Server(
        uvicorn.Config(create_app(container), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    errors = []
    requests = []
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Isolated test API did not start"
        with sync_playwright() as playwright, ExitStack() as stack:
            options = {"headless": True}
            if os.environ.get("SIMON_BROWSER_CHANNEL"):
                options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
            browser = playwright.chromium.launch(**options)
            stack.callback(browser.close)

            def user(*, actor_id=None, role="member", name="Test collaborator"):
                actor_id = actor_id or uuid4()
                container.store.put_membership(
                    Membership(
                        actor_id=actor_id,
                        workspace_id=DEV_WORKSPACE_ID,
                        role=role,
                        display_name=name,
                    )
                )
                token, _ = container.identity._issue(actor_id, DEV_WORKSPACE_ID, "development")
                _, actor = container.identity.resolve(token)
                client = stack.enter_context(httpx.Client(base_url=origin, trust_env=False))
                client.cookies.set(session_cookie(container.identity), token)
                client.headers.update(
                    {
                        "Origin": origin,
                        "X-CSRF-Token": csrf_token(token),
                        "X-Workspace-ID": str(DEV_WORKSPACE_ID),
                    }
                )
                context = browser.new_context(viewport={"width": 1440, "height": 1080})
                context.add_cookies(
                    [
                        {
                            "name": session_cookie(container.identity),
                            "value": token,
                            "url": origin,
                            "sameSite": "Strict",
                        }
                    ]
                )
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda event: requests.append((event.method, event.url)))
                return SimpleNamespace(page=page, context=context, client=client, actor=actor)

            owner = user(actor_id=DEV_ACTOR_ID, role="owner", name="Project owner")
            ui = SimpleNamespace(
                **vars(owner),
                container=container,
                service=container.native_projects,
                origin=origin,
                prefix=prefix,
                url=origin + prefix + "/projects",
                path=lambda path: prefix + path,
                user=user,
                requests=requests,
            )
            yield ui
            assert not errors, errors
            assert not legacy_calls
            assert not [url for _, url in requests if "/v1/projects" in url]
            assert container.agent_runs.list(owner.actor) == ()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "Isolated test API did not stop"


def seed_project(ui, name="Studio launch"):
    return ui.service.create_project(
        ui.actor,
        CreateNativeProject(
            name=name,
            objective="Review the available context, agree on direction, and prepare a launch.",
            idempotency_key=str(uuid4()),
        ),
    )


def seed_task(ui, project, title="Review direction"):
    return ui.service.create_task(
        ui.actor,
        project.id,
        CreateNativeTask(title=title, idempotency_key=str(uuid4())),
    )


def open_project(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    page.goto(f"{ui.url}?project={project.id}")
    expect(page.locator("#np-name")).to_have_text(project.name)
    expect(page.locator("#np-refresh")).to_be_enabled()


def create_project(ui, name="Launch studio", objective="Prepare a reviewed launch plan."):
    from playwright.sync_api import expect

    page = ui.page
    page.goto(ui.url)
    page.locator("#np-new").click()
    page.locator("#np-project-name").fill(name)
    page.locator("#np-project-objective").fill(objective)
    page.locator("#np-project-save").click()
    expect(page.locator("#np-project-dialog")).not_to_be_visible()
    expect(page.locator("#np-name")).to_have_text(name)
    project_id = UUID(parse_qs(urlsplit(page.url).query)["project"][0])
    return ui.service.get_project(ui.actor, project_id)


def task_card(page, task):
    return page.locator(f'[data-task-id="{task.id}"]')


def create_task(ui, project, title="Review brand direction"):
    from playwright.sync_api import expect

    ui.page.locator("#np-new-task").click()
    ui.page.locator("#np-task-title").fill(title)
    ui.page.locator("#np-task-description").fill("Document alternatives and the decision criteria.")
    ui.page.locator("#np-task-save").click()
    expect(ui.page.locator("#np-task-dialog")).not_to_be_visible()
    return next(task for task in ui.service.tasks(ui.actor, project.id) if task.title == title)


def share(ui, project, member):
    latest = ui.service.get_project(ui.actor, project.id)
    ui.service.put_member(
        ui.actor,
        project.id,
        PutNativeProjectMember(
            actor_id=member.actor.actor_id,
            expected_version=latest.version,
            idempotency_key=str(uuid4()),
        ),
    )


def test_project_intake_task_lifecycle_and_refresh_persistence(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    page = ui.page
    project = create_project(ui)
    expect(page.locator("#np-objective")).to_have_text(project.objective)
    assert ui.container.store.explicit_memory(DEV_WORKSPACE_ID, project.id) is None
    task = create_task(ui, project)
    card = task_card(page, task)
    expect(card).to_contain_text("Available to pick up")
    card.get_by_role("button", name="Pick up task", exact=True).click()
    expect(card).to_contain_text("Project owner")
    claimed = ui.service.get_task(ui.actor, project.id, task.id)
    assert claimed.assignment.actor_id == DEV_ACTOR_ID
    for status in ("in_progress", "in_review", "blocked", "done", "cancelled", "todo"):
        card.locator(".np-task-open").click()
        page.locator("#np-task-status").select_option(status)
        page.locator("#np-task-save").click()
        expect(page.locator("#np-task-dialog")).not_to_be_visible()
        expect(page.locator(f'[data-status="{status}"]')).to_contain_text(task.title)
        assert ui.service.get_task(ui.actor, project.id, task.id).status == status
    card.locator(".np-task-open").click()
    page.locator("#np-task-title").fill("Review completed direction")
    page.locator("#np-task-assignee").select_option("")
    page.locator("#np-task-save").click()
    expect(page.locator("#np-task-dialog")).not_to_be_visible()
    page.locator("#np-edit-project").click()
    page.locator("#np-project-name").fill("Reviewed studio launch")
    page.locator("#np-project-objective").fill("The direction is agreed. Prepare the launch.")
    page.locator("#np-project-save").click()
    expect(page.locator("#np-project-dialog")).not_to_be_visible()
    page.reload()
    expect(page.locator("#np-name")).to_have_text("Reviewed studio launch")
    expect(page.locator("#np-objective")).to_contain_text("direction is agreed")
    expect(task_card(page, task)).to_contain_text("Review completed direction")
    expect(task_card(page, task)).to_contain_text("Available to pick up")
    page.locator("#np-back").click()
    expect(page.locator("#np-list")).to_be_visible()
    page.get_by_role("link", name="Reviewed studio launch", exact=True).click()
    expect(page.locator("#np-name")).to_have_text("Reviewed studio launch")


def test_members_share_real_board_and_guests_are_read_only(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    collaborator = ui.user(name="Clothing designer")
    guest = ui.user(role="guest", name="Review guest")
    project = seed_project(ui)
    task = seed_task(ui, project)
    open_project(ui, project)
    ui.page.locator("#np-team").click()
    for person in (collaborator, guest):
        ui.page.locator("#np-member-actor").select_option(str(person.actor.actor_id))
        ui.page.locator("#np-member-save").click()
        expect(ui.page.locator("#np-members")).to_contain_text(
            "Review guest" if person is guest else "Clothing designer"
        )
    ui.page.locator('[data-close="np-team-dialog"]').click()
    open_project(ui, project, page=collaborator.page)
    task_card(collaborator.page, task).get_by_role(
        "button", name="Pick up task", exact=True
    ).click()
    expect(task_card(collaborator.page, task)).to_contain_text("Clothing designer")
    ui.page.locator("#np-refresh").click()
    expect(task_card(ui.page, task)).to_contain_text("Clothing designer")
    open_project(ui, project, page=guest.page)
    expect(task_card(guest.page, task)).to_contain_text(task.title)
    for selector in ("#np-new-task", "#np-edit-project"):
        expect(guest.page.locator(selector)).not_to_be_visible()
    task_card(guest.page, task).locator(".np-task-open").click()
    expect(guest.page.locator("#np-task-title")).to_be_disabled()
    expect(guest.page.locator("#np-task-save")).not_to_be_visible()
    guest.page.keyboard.press("Escape")
    guest.page.locator("#np-team").click()
    expect(guest.page.locator("#np-member-form")).not_to_be_visible()
    expect(guest.page.locator("#np-members")).to_contain_text("Clothing designer")
    response = guest.client.post(
        ui.path(f"/v2/projects/{project.id}/tasks"),
        json={"title": "Forbidden guest write", "idempotency_key": "guest-browser-attempt"},
    )
    assert response.status_code == 403
    assert len(ui.service.tasks(ui.actor, project.id)) == 1


@pytest.mark.parametrize("kind", ["project", "task"])
def test_conflicting_edit_preserves_draft_until_explicit_rebase(native_ui, kind):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    task = seed_task(ui, project)
    open_project(ui, project)
    if kind == "project":
        ui.page.locator("#np-edit-project").click()
        field = ui.page.locator("#np-project-objective")
        ui.service.update_project(
            ui.actor,
            project.id,
            UpdateNativeProject(
                name=project.name,
                objective="A collaborator saved a revised brief.",
                expected_version=project.version,
                idempotency_key=str(uuid4()),
            ),
        )
    else:
        task_card(ui.page, task).locator(".np-task-open").click()
        field = ui.page.locator("#np-task-description")
        ui.service.update_task(
            ui.actor,
            project.id,
            task.id,
            UpdateNativeTask(
                title=task.title,
                description="A collaborator saved a revised task.",
                status="in_progress",
                assignment=task.assignment,
                expected_version=task.version,
                idempotency_key=str(uuid4()),
            ),
        )
    draft = "Keep my unsaved reasoning until I review the collaborator's changes."
    field.fill(draft)
    ui.page.locator(f"#np-{kind}-save").click()
    expect(ui.page.locator(f"#np-{kind}-conflict")).to_be_visible()
    expect(ui.page.locator(f"#np-{kind}-latest")).to_contain_text("collaborator saved")
    expect(field).to_have_value(draft)
    ui.page.locator(f"#np-{kind}-rebase").click()
    expect(field).to_have_value(draft)
    ui.page.locator(f"#np-{kind}-save").click()
    expect(ui.page.locator(f"#np-{kind}-dialog")).not_to_be_visible()
    saved = (
        ui.service.get_project(ui.actor, project.id)
        if kind == "project"
        else ui.service.get_task(ui.actor, project.id, task.id)
    )
    assert saved.version == 3
    assert (saved.objective if kind == "project" else saved.description) == draft


def test_lost_create_response_retries_same_command_without_duplicate(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    submitted = []

    def lose_first_response(route):
        if route.request.method != "POST":
            route.continue_()
            return
        submitted.append(route.request.post_data_json)
        if len(submitted) == 1:
            response = route.fetch()
            assert response.status == 201
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/v2/projects", lose_first_response)
    ui.page.goto(ui.url)
    ui.page.locator("#np-new").click()
    ui.page.locator("#np-project-name").fill("Uncertain delivery")
    ui.page.locator("#np-project-objective").fill("Keep the command safe to retry.")
    ui.page.locator("#np-project-save").click()
    expect(ui.page.locator("#np-project-feedback")).to_contain_text("save could not be confirmed")
    expect(ui.page.locator("#np-project-dialog")).to_be_visible()
    expect(ui.page.locator("#np-project-name")).to_have_value("Uncertain delivery")
    expect(ui.page.locator("#np-project-name")).to_be_disabled()
    ui.page.keyboard.press("Escape")
    expect(ui.page.locator("#np-project-dialog")).to_be_visible()
    assert len(ui.service.list_projects(ui.actor)) == 1
    ui.page.locator("#np-project-save").click()
    expect(ui.page.locator("#np-project-dialog")).not_to_be_visible()
    expect(ui.page.locator("#np-name")).to_have_text("Uncertain delivery")
    assert len(submitted) == 2
    assert submitted[0] == submitted[1]
    assert len(ui.service.list_projects(ui.actor)) == 1


def test_lost_claim_response_remains_retryable_after_refresh(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    task = seed_task(ui, project)
    submitted = []

    def lose_first_response(route):
        submitted.append(route.request.post_data_json)
        if len(submitted) == 1:
            response = route.fetch()
            assert response.status == 200
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/tasks/*/claim", lose_first_response)
    open_project(ui, project)
    task_card(ui.page, task).get_by_role("button", name="Pick up task", exact=True).click()
    expect(ui.page.locator("#np-status")).to_contain_text("save could not be confirmed")
    ui.page.locator("#np-refresh").click()
    expect(task_card(ui.page, task)).to_contain_text("Project owner")
    task_card(ui.page, task).get_by_role("button", name="Retry claim", exact=True).click()
    expect(task_card(ui.page, task).get_by_role("button", name="Retry claim")).to_have_count(0)
    assert len(submitted) == 2
    assert submitted[0] == submitted[1]
    assert ui.service.get_task(ui.actor, project.id, task.id).version == 2


def test_lost_task_create_response_replays_saved_payload(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    submitted = []

    def lose_first_response(route):
        if route.request.method != "POST":
            route.continue_()
            return
        submitted.append(route.request.post_data_json)
        if len(submitted) == 1:
            response = route.fetch()
            assert response.status == 201
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/v2/projects/*/tasks", lose_first_response)
    open_project(ui, project)
    ui.page.locator("#np-new-task").click()
    ui.page.locator("#np-task-title").fill("Exactly one design review")
    ui.page.locator("#np-task-save").click()
    expect(ui.page.locator("#np-task-feedback")).to_contain_text("save could not be confirmed")
    expect(ui.page.locator("#np-task-title")).to_be_disabled()
    assert len(ui.service.tasks(ui.actor, project.id)) == 1
    ui.page.keyboard.press("Escape")
    expect(ui.page.locator("#np-task-dialog")).to_be_visible()
    ui.page.locator("#np-task-save").click()
    expect(ui.page.locator("#np-task-dialog")).not_to_be_visible()
    expect(ui.page.locator("[data-task-id]")).to_have_count(1)
    assert len(submitted) == 2 and submitted[0] == submitted[1]
    assert len(ui.service.tasks(ui.actor, project.id)) == 1


def test_lost_member_removal_locks_dialog_and_replays_removal(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    member = ui.user(name="Temporary reviewer")
    project = seed_project(ui)
    share(ui, project, member)
    submitted = []

    def lose_first_response(route):
        submitted.append((route.request.url, route.request.post_data_json))
        if len(submitted) == 1:
            response = route.fetch()
            assert response.status == 200
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/members/*/remove", lose_first_response)
    open_project(ui, project)
    ui.page.locator("#np-team").click()
    row = ui.page.locator("#np-members li").filter(has_text="Temporary reviewer")
    row.get_by_role("button", name="Remove", exact=True).click()
    expect(ui.page.locator("#np-team-feedback")).to_contain_text("save could not be confirmed")
    expect(row.get_by_role("button", name="Change role", exact=True)).to_be_disabled()
    ui.page.keyboard.press("Escape")
    expect(ui.page.locator("#np-team-dialog")).to_be_visible()
    ui.page.locator("#np-member-save").click()
    expect(ui.page.locator("#np-team-dialog")).not_to_be_visible()
    ui.page.locator("#np-team").click()
    expect(ui.page.locator("#np-members")).not_to_contain_text("Temporary reviewer")
    assert len(submitted) == 2 and submitted[0] == submitted[1]
    assert len(ui.service.members(ui.actor, project.id)) == 1


def test_revoked_access_cannot_redisplay_cached_project_or_board(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    member = ui.user(name="Temporary collaborator")
    project = seed_project(ui, "Confidential collection")
    task = seed_task(ui, project, "Private design direction")
    share(ui, project, member)
    member.page.goto(ui.url)
    member.page.get_by_role("link", name=project.name, exact=True).click()
    expect(task_card(member.page, task)).to_be_visible()
    open_project(ui, project)
    ui.page.locator("#np-team").click()
    row = ui.page.locator("#np-members li").filter(has_text="Temporary collaborator")
    row.get_by_role("button", name="Remove", exact=True).click()
    expect(ui.page.locator("#np-team-dialog")).not_to_be_visible()
    member.page.locator("#np-refresh").click()
    expect(member.page.locator("#np-status")).to_contain_text("Project not found")
    expect(member.page.locator("#np-detail")).not_to_be_visible()
    expect(task_card(member.page, task)).not_to_be_visible()
    member.page.locator("#np-search").fill("Confidential")
    expect(member.page.get_by_role("link", name=project.name, exact=True)).to_have_count(0)
    assert member.client.get(ui.path(f"/v2/projects/{project.id}")).status_code == 404


def test_archived_projects_preserve_board_and_restore_editing(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    task = seed_task(ui, project)
    open_project(ui, project)
    ui.page.locator("#np-edit-project").click()
    ui.page.locator("#np-project-status").select_option("archived")
    ui.page.locator("#np-project-save").click()
    expect(ui.page.locator("#np-project-dialog")).not_to_be_visible()
    expect(ui.page.locator("#np-meta")).to_contain_text(re.compile("Archived", re.I))
    expect(ui.page.locator("#np-new-task")).not_to_be_visible()
    expect(task_card(ui.page, task)).to_contain_text(task.title)
    task_card(ui.page, task).locator(".np-task-open").click()
    expect(ui.page.locator("#np-task-title")).to_be_disabled()
    expect(ui.page.locator("#np-task-save")).not_to_be_visible()
    ui.page.keyboard.press("Escape")
    response = ui.client.post(
        ui.path(f"/v2/projects/{project.id}/tasks"),
        json={"title": "Must not be added", "idempotency_key": "archived-browser-write"},
    )
    assert response.status_code == 409
    ui.page.locator("#np-back").click()
    expect(ui.page.locator("#np-projects")).not_to_contain_text(project.name)
    ui.page.locator("#np-filter").select_option("archived")
    ui.page.get_by_role("link", name=project.name, exact=True).click()
    ui.page.locator("#np-edit-project").click()
    ui.page.locator("#np-project-status").select_option("active")
    ui.page.locator("#np-project-save").click()
    expect(ui.page.locator("#np-project-dialog")).not_to_be_visible()
    expect(ui.page.locator("#np-new-task")).to_be_enabled()
    expect(
        task_card(ui.page, task).get_by_role("button", name="Pick up task", exact=True)
    ).to_be_enabled()


def test_search_and_assignment_filters_include_records_after_first_api_page(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui, "Original project beyond first page")
    for index in range(101):
        seed_project(ui, f"Other project {index:03}")
    first_task = seed_task(ui, project, "Original task beyond first page")
    for index in range(101):
        seed_task(ui, project, f"Other task {index:03}")
    ui.page.goto(ui.url)
    ui.page.locator("#np-search").fill("Original project")
    ui.page.get_by_role("link", name=project.name, exact=True).click()
    expect(ui.page.locator("#np-name")).to_have_text(project.name)
    ui.page.locator("#np-task-search").fill("Original task")
    expect(task_card(ui.page, first_task)).to_be_visible()
    expect(ui.page.locator("[data-task-id]")).to_have_count(1)
    ui.page.locator("#np-task-filter").select_option("mine")
    expect(ui.page.locator("[data-task-id]")).to_have_count(0)
    ui.page.locator("#np-task-filter").select_option("pool")
    task_card(ui.page, first_task).get_by_role("button", name="Pick up task", exact=True).click()
    expect(ui.page.locator("[data-task-id]")).to_have_count(0)
    ui.page.locator("#np-task-filter").select_option("mine")
    expect(task_card(ui.page, first_task)).to_be_visible()
    paged = [url for method, url in ui.requests if method == "GET" and "offset=100" in url]
    assert any("/tasks" in url for url in paged)
    assert any("/tasks" not in url and "/v2/projects?" in url for url in paged)


def test_mobile_keyboard_and_untrusted_text_are_safe(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    payload = '<img src=x onerror="window.nativeInjected=true">'
    project = create_project(ui, name=payload, objective="Literal text: " + payload)
    task = create_task(ui, project, title=payload)
    expect(ui.page.locator("#np-name")).to_have_text(payload)
    expect(task_card(ui.page, task)).to_contain_text(payload)
    assert ui.page.locator("#np-name img, #np-objective img, #np-board img").count() == 0
    assert ui.page.evaluate("window.nativeInjected === undefined")
    screenshots = Path(".local/native-projects-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.screenshot(path=str(screenshots / "desktop.png"), full_page=True)
    ui.page.set_viewport_size({"width": 390, "height": 844})
    assert ui.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    ui.page.locator("#np-new-task").focus()
    ui.page.keyboard.press("Enter")
    expect(ui.page.locator("#np-task-dialog")).to_be_visible()
    expect(ui.page.locator("#np-task-title")).to_be_focused()
    ui.page.locator("#np-task-title").fill("A keyboard-created mobile task")
    ui.page.locator("#np-task-save").focus()
    ui.page.keyboard.press("Enter")
    expect(ui.page.locator("#np-task-dialog")).not_to_be_visible()
    expect(ui.page.locator("#np-board")).to_contain_text("A keyboard-created mobile task")
    ui.page.locator("#np-edit-project").click()
    expect(ui.page.locator("#np-project-dialog")).to_be_visible()
    box = ui.page.locator("#np-project-dialog").bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 391
    ui.page.keyboard.press("Escape")
    expect(ui.page.locator("#np-project-dialog")).not_to_be_visible()
    expect(ui.page.locator("#np-edit-project")).to_be_focused()
    assert ui.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    ui.page.screenshot(path=str(screenshots / "mobile.png"), full_page=True)


@pytest.mark.parametrize("native_ui", ["/simon"], indirect=True)
def test_deep_links_assets_and_mutations_respect_public_base_path(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = create_project(ui, name="Mounted workspace")
    task = create_task(ui, project)
    response = ui.page.reload()
    assert response and response.status == 200
    assert response.header_value("cache-control") == "no-store"
    csp = response.header_value("content-security-policy")
    assert csp and "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    expect(ui.page.locator("#np-name")).to_have_text(project.name)
    expect(task_card(ui.page, task)).to_contain_text(task.title)
    expect(ui.page.get_by_role("link", name="Conversations", exact=True)).to_have_attribute(
        "href", "/simon/chat"
    )
    expect(ui.page.get_by_role("link", name="Legacy work", exact=True)).to_have_attribute(
        "href", "/simon/chat?view=work"
    )
    for _, url in ui.requests:
        if url.startswith(ui.origin):
            assert urlsplit(url).path.startswith("/simon/"), url
