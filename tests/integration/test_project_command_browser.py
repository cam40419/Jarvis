"""Exercise the Work UI against real API services without network model calls."""

import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from simon.adapters.tool_transports import TransportRegistry
from simon.api.app import AppContainer, create_app
from simon.config import Settings
from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.external_actions import CallDetails, ExternalActionDraft
from simon.domain.model_routing import ModelEndpoint, TextGenerationResult
from simon.services.agent_dispatcher import AgentDispatcher
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_worker import AgentWorker
from simon.services.project_autonomy import ProjectAutonomyService

pytestmark = pytest.mark.browser


@pytest.fixture
def project_ui(tmp_path):
    if os.environ.get("SIMON_BROWSER_TESTS") != "1":
        pytest.skip("set SIMON_BROWSER_TESTS=1")
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    container = AppContainer(
        settings=Settings(
            _env_file=None,
            model_provider="local",
            openai_api_key=None,
            agent_manifest_file=None,
            environment="test",
            storage_backend="memory",
            public_origin=origin,
            rp_id="127.0.0.1",
            dev_login_enabled=True,
            dev_login_token=SecretStr("test-development-secret-32-characters"),
            agent_state_dir=tmp_path / "agents",
            local_files_dir=tmp_path / "files",
            local_files_enabled=True,
        )
    )
    manifest = PlatformManifest(
        agents=(
            AgentProfile(
                id="writer",
                name="Report writer",
                description="Research evidence, draft documents, and check the finished results.",
                instructions="Write clearly.",
                max_output_tokens=4096,
            ),
        ),
        teams=(TeamTemplate(id="studio", name="Research studio", agent_ids=("writer",)),),
        models=(
            ModelEndpoint(
                id="local",
                model="synthetic-local",
                provider="openai_compatible",
                base_url="http://localhost:11434/v1",
                local=True,
                tier="economy",
            ),
        ),
    )
    configured = AgentPlatformService(container.store, manifest, state_dir=tmp_path, environ={})
    container.agent_platform.__dict__.update(configured.__dict__)
    container.agent_runs.enabled = True

    container.agent_platform.project_team_resolver = container.project_coordinator.resolve_team
    container.agent_platform.project_profile_resolver = container.project_work.member_profiles
    container.agent_platform.project_visibility_resolver = container.project_work.project_resolver
    container.project_work.role_capture = container.agent_platform.agent_profiles.capture_role
    container.project_work.role_resolver = container.agent_platform.agent_profiles.resolve_role
    scheduler = ProjectAutonomyService(
        container.project_work, container.project_coordinator, enabled=True
    )
    synthetic = {
        "decision": {
            "status": "plan",
            "summary": "Prepare the research and a launch brief.",
            "tasks": [
                {
                    "id": "research",
                    "title": "Research plan",
                    "agent_id": "writer",
                    "objective": "Outline the evidence needed.",
                    "depends_on": [],
                    "tool_ids": [],
                },
                {
                    "id": "brief",
                    "title": "Launch brief",
                    "agent_id": "writer",
                    "objective": "Use the research to write a brief.",
                    "depends_on": ["research"],
                    "tool_ids": [],
                },
            ],
        }
    }

    class Model:
        def generate(self, decision, request):
            return TextGenerationResult(
                endpoint_id=decision.endpoint_id,
                model=decision.model,
                text=json.dumps(synthetic["decision"])
                if "Plan the next useful" in request.prompt
                else "## Completed report\nA useful, saved launch brief.",
                input_tokens=20,
                output_tokens=8,
            )

    dispatcher = AgentDispatcher(
        container.agent_runs,
        worker_factory=lambda *_: AgentWorker(
            Model(),
            container.agent_platform.tools,
            TransportRegistry(),
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(container),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    with httpx.Client(base_url=origin, trust_env=False) as client:
        response = client.post(
            "/auth/dev-login",
            headers={"Origin": origin},
            json={"token": "test-development-secret-32-characters"},
        )
        assert response.status_code == 200
        token = client.cookies.get("simon_session")
    try:
        with sync_playwright() as playwright:
            options = {"headless": True}
            if os.environ.get("SIMON_BROWSER_CHANNEL"):
                options["channel"] = os.environ["SIMON_BROWSER_CHANNEL"]
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1440, "height": 1080})
            context.add_cookies(
                [
                    {"name": "simon_session", "value": token, "url": origin, "sameSite": "Strict"},
                ]
            )
            errors = []
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/chat")
            expect(page.locator("#connections-open")).to_be_enabled()
            page.get_by_role("button", name="Work", exact=True).click()
            expect(page.locator("#pc-status")).to_contain_text("Saved work")
            try:
                yield page, dispatcher, scheduler, container, synthetic
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def create_project(page, name="Launch studio"):
    from playwright.sync_api import expect

    page.locator("#pc-new-project").click()
    page.locator("#pc-create-dialog").get_by_label("Project name", exact=True).fill(name)
    page.get_by_label("What does success look like?").fill(
        "Prepare a practical launch brief with clear next steps."
    )
    page.get_by_label("Team name", exact=True).fill("Launch team")
    page.locator("#pc-create-dialog summary").click()
    page.get_by_label("Report writer responsibilities", exact=True).fill(
        "Coordinate research and prepare the brief."
    )
    page.get_by_role("button", name="Create project", exact=True).click()
    expect(page.locator("#pc-create-dialog")).not_to_be_visible()
    expect(page.locator("#pc-project-title")).to_have_text(name)
    expect(page.locator("#pc-context")).to_contain_text("Coordinate research")
    return page.url.split("project=")[1]


def refresh(page):
    from playwright.sync_api import expect

    expect(page.locator("#pc-refresh")).to_be_enabled()
    with page.expect_response("**/command"):
        page.locator("#pc-refresh").click()
    expect(page.locator("#pc-refresh")).to_be_enabled()


def test_lead_delegation_review_results_and_return(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, _ = project_ui
    create_project(page)
    page.locator("#pc-command").fill("Research the launch and write our brief.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("waiting for the coordinator")
    expect(page.locator("#pc-command-submit")).to_be_disabled()
    assert scheduler.tick() == 1
    refresh(page)
    expect(page.locator("#pc-cycle")).to_contain_text("preparing a plan")
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-cycle")).to_contain_text("Your plan is ready")
    expect(page.locator("#pc-panel-content .pc-task")).to_have_count(2)
    expect(page.locator("#pc-panel-content")).to_contain_text("Launch brief")
    assert dispatcher.tick() is None
    page.get_by_role("button", name="Start delegated work", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("Approved")
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-cycle")).to_contain_text("This cycle is complete")
    expect(page.locator(".pc-task .pc-status.done")).to_have_count(2)
    page.locator("#pc-panel-content summary").filter(has_text="Read result").first.click()
    expect(page.locator("#pc-panel-content .pc-markdown").first).to_contain_text(
        "saved launch brief"
    )
    with page.expect_download() as downloaded:
        page.locator("#pc-panel-content").get_by_role("link", name="answer.txt").first.click()
    assert "saved launch brief" in Path(downloaded.value.path()).read_text(encoding="utf-8")
    page.locator("#pc-tab-findings").click()
    expect(page.locator("#pc-panel-content")).to_contain_text("saved launch brief")
    page.reload()
    expect(page.locator("#pc-project-title")).to_have_text("Launch studio")
    expect(page.locator("#pc-cycle")).to_contain_text("This cycle is complete")
    page.locator("#pc-command").fill("Keep this draft while progress refreshes.")
    page.locator("#pc-command").focus()
    with page.expect_response("**/command"):
        page.evaluate("window.SimonProjectCommand.refresh(true)")
    expect(page.locator("#pc-command")).to_be_focused()
    expect(page.locator("#pc-command")).to_have_value("Keep this draft while progress refreshes.")
    page.evaluate(
        "() => { window.scrollTo(0, 0); document.getElementById('work-view').scrollTop = 0; }"
    )
    page.screenshot(path=str(tmp_path / "project-command-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "project-command-mobile.png"), animations="disabled")
    page.locator("#pc-tab-tasks").press("ArrowRight")
    expect(page.locator("#pc-tab-files")).to_be_focused()
    expect(page.locator("#pc-tab-files")).to_have_attribute("aria-selected", "true")
    page.locator("#pc-tab-files").press("End")
    expect(page.locator("#pc-tab-team")).to_be_focused()


def test_project_files_full_run_history_and_sessions_navigation(project_ui, tmp_path):
    from cryptography.fernet import Fernet
    from playwright.sync_api import expect

    from simon.adapters.google import DRIVE_WRITE_SCOPE, GoogleTokens
    from simon.domain.connected_tools import GoogleConnection
    from simon.domain.project_files import ProjectFileCreate
    from tests.contract.test_project_files import FakeDrive

    page, dispatcher, scheduler, container, synthetic = project_ui
    project_id = create_project(page, "Field research")
    _, actor = container.identity.resolve(page.context.cookies()[0]["value"])
    connected = container.connected
    connected.settings = connected.settings.model_copy(
        update={
            "google_client_id": "synthetic",
            "google_client_secret": SecretStr("synthetic"),
            "google_token_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    connected.store.save_google_connection(
        GoogleConnection(
            household_id=actor.household_id,
            actor_id=actor.actor_id,
            email="synthetic@example.com",
            scopes=(DRIVE_WRITE_SCOPE,),
            encrypted_tokens=connected.encrypt(
                GoogleTokens(
                    access_token="synthetic",
                    refresh_token="synthetic",
                    expires_at=time.time() + 3600,
                ).model_dump_json()
            ),
        )
    )
    connected.projects.api = FakeDrive()
    connected.projects.ensure(actor, UUID(project_id))
    connected.projects.create_file(
        actor,
        ProjectFileCreate(
            project_id=UUID(project_id), name="shared-research.md", content="Shared evidence"
        ),
        "shared-research-file",
        lambda: actor,
    )
    page.evaluate(
        """async project => {
          const response = await fetch(appPath('/v1/local-files/upload?' + new URLSearchParams({
            root: 'project:' + project, path: 'field-notes.txt',
            idempotency_key: 'project-notes-file'
          })), {method: 'POST', credentials: 'same-origin', body: 'Saved local evidence',
            headers: {'Content-Type': 'application/octet-stream',
              'X-CSRF-Token': session.csrf_token}});
          if (!response.ok) throw Error(await response.text());
        }""",
        project_id,
    )
    page.locator("#pc-tab-files").click()
    expect(page.locator("#pc-local-files")).to_contain_text("field-notes.txt")
    expect(page.locator("#pc-drive-files")).to_contain_text("shared-research.md")
    expect(
        page.locator("#pc-drive-files").get_by_role("link", name="Open in Drive")
    ).to_have_attribute("href", re.compile(r"https://drive.google.com/"))
    with page.expect_download() as download:
        page.locator("#pc-local-files").get_by_role("link", name="Download", exact=True).click()
    assert Path(download.value.path()).read_text() == "Saved local evidence"
    page.get_by_role("button", name="Browse local files", exact=True).click()
    expect(page.locator("#local-files-panel")).to_contain_text("field-notes.txt")
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Browse local files", exact=True)).to_be_focused()
    page.locator("#pc-tab-overview").click()
    page.locator("#pc-command").fill("Write our original field report.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    dispatcher.tick()
    scheduler.tick()
    refresh(page)
    page.get_by_role("button", name="Start delegated work", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("Approved")
    scheduler.tick()
    page.locator("#pc-tab-history").click()
    pending_execution = page.locator("#pc-history-panel .pc-run-row").filter(
        has_text="Team execution"
    )
    pending_execution.get_by_text("View results & files", exact=True).click()
    expect(pending_execution).to_contain_text("Output will appear when this task finishes")
    expect(
        pending_execution.get_by_role("region", name="Activity for lead-summary")
    ).to_be_visible()
    dispatcher.tick()
    scheduler.tick()
    page.get_by_role("button", name="Refresh run history", exact=True).click()
    expect(pending_execution).to_contain_text("saved launch brief")
    synthetic["decision"] = {"status": "complete", "summary": "Evidence is complete.", "tasks": []}
    for index in range(20):
        container.project_work.request_cycle(
            actor,
            UUID(project_id),
            f"Review saved evidence {index}.",
            f"history-check-{index}",
        )
        scheduler.tick()
        assert dispatcher.tick().status == "succeeded"
        scheduler.tick()
    refresh(page)
    page.locator("#pc-tab-history").click()
    page.get_by_role("button", name="Refresh run history", exact=True).click()
    expect(page.locator("#pc-history-panel .pc-run-row")).to_have_count(20)
    page.get_by_role("button", name="Load earlier runs", exact=True).click()
    expect(page.locator("#pc-history-panel .pc-run-row")).to_have_count(22)
    execution = page.locator("#pc-history-panel .pc-run-row").filter(has_text="Team execution")
    execution.get_by_text("View results & files", exact=True).click()
    expect(execution).to_contain_text("saved launch brief")
    with page.expect_download() as old_output:
        execution.get_by_role("link", name="answer.txt", exact=True).first.click()
    assert "saved launch brief" in Path(old_output.value.path()).read_text(encoding="utf-8")
    page.locator("#pc-tab-sessions").click()
    expect(page.get_by_role("button", name="Start a session", exact=True)).to_be_visible()
    page.locator("#project-work-title").fill("Follow up on the research")
    page.locator("#project-work-instructions").fill("Keep the saved evidence organized.")
    page.get_by_role("button", name="Start background work", exact=True).click()
    expect(page.locator("#project-work-feedback")).to_contain_text("Saved")
    expect(page.locator("#project-page-tasks")).to_contain_text("Follow up on the research")
    page.locator("#pc-tab-files").click()
    expect(page.locator("#pc-local-files")).to_contain_text("field-notes.txt")
    page.locator("#work-view").evaluate("el => el.scrollTop = 0")
    page.screenshot(path=str(tmp_path / "project-files-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "project-files-mobile.png"), animations="disabled")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#pc-tab-overview").click()
    page.locator("#pc-command").fill("Preserve this draft across project sections.")
    page.locator("#pc-tab-team").click()
    expect(page.get_by_role("button", name="Pause project", exact=True)).to_be_visible()
    page.locator("#pc-tab-overview").click()
    expect(page.locator("#pc-command")).to_have_value(
        "Preserve this draft across project sections."
    )
    page.locator("#pc-command-submit").scroll_into_view_if_needed()
    expect(page.locator("#pc-command-submit")).to_be_in_viewport()
    page.locator("#pc-refresh").scroll_into_view_if_needed()
    expect(page.locator("#pc-refresh")).to_be_in_viewport()


def test_project_switch_ignores_deferred_history_and_session_responses(project_ui):
    from playwright.sync_api import expect

    page, _, scheduler, container, _ = project_ui
    first = create_project(page, "First project")
    second = page.evaluate(
        """async first => {
          const current = (await api('/v1/projects/' + first + '/command')).state;
          const project = await api('/v1/projects', {name: 'Second project',
            description: 'Keep project records separate.', idempotency_key: 'switch-project-two'});
          await api('/v1/projects/' + project.id + '/team',
            {expected_version: 0, team: current.team}, 'PATCH');
          await api('/v1/assistant-tasks', {title: 'First project task',
            instructions: 'Keep this task in the first project.', project_id: first,
            task_type: 'work', priority: 3, idempotency_key: 'first-background-task'});
          return project.id;
        }""",
        first,
    )
    page.locator("#pc-tab-sessions").click()
    expect(page.locator("#project-page-tasks")).to_contain_text("First project task")
    page.locator("#project-work-title").fill("Unsubmitted first-project draft")
    second_page = page.evaluate("async id => api('/v1/projects/' + id)", second)
    deferred_sessions = []
    second_url = page.url.split("/chat")[0] + "/v1/projects/" + second
    page.route(second_url, lambda route: deferred_sessions.append(route))
    page.evaluate("id => { window.SimonProjectCommand.open(id); }", second)
    expect(page.locator("#pc-project-title")).to_have_text("Second project")
    page.locator("#pc-tab-sessions").click()
    expect(page.locator("#project-background-resources")).not_to_be_visible()
    expect(page.locator("#pc-sessions-panel")).to_contain_text("Loading project sessions")
    page.evaluate("id => { window.SimonProjectCommand.open(id); }", first)
    expect(page.locator("#pc-project-title")).to_have_text("First project")
    page.locator("#pc-tab-sessions").click()
    expect(page.locator("#project-page-tasks")).to_contain_text("First project task")
    expect(page.locator("#project-work-title")).to_have_value("Unsubmitted first-project draft")
    assert deferred_sessions
    for route in deferred_sessions:
        route.fulfill(json=second_page)
    page.unroute(second_url)
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    expect(page.locator("#project-page-tasks")).to_contain_text("First project task")
    expect(page.locator("#pc-project-title")).to_have_text("First project")
    history_url = page.url.split("/chat")[0] + "/v1/projects/" + first + "/runs?*"
    deferred_history = []
    hold_history = {"enabled": True}

    def hold_or_continue(route):
        if hold_history["enabled"]:
            deferred_history.append(route)
        else:
            route.continue_()

    page.route(history_url, hold_or_continue)
    page.locator("#pc-tab-history").click()
    expect(page.locator("#pc-history-panel")).to_contain_text("Loading project runs")
    page.evaluate("id => { window.SimonProjectCommand.open(id); }", second)
    expect(page.locator("#pc-project-title")).to_have_text("Second project")
    _, actor = container.identity.resolve(page.context.cookies()[0]["value"])
    container.project_work.request_cycle(
        actor, UUID(first), "Prepare the first project plan.", "deferred-history-plan"
    )
    scheduler.tick()
    hold_history["enabled"] = False
    page.evaluate("id => { window.SimonProjectCommand.open(id); }", first)
    expect(page.locator("#pc-project-title")).to_have_text("First project")
    page.locator("#pc-tab-history").click()
    expect(page.locator("#pc-history-panel .pc-run-row")).to_have_count(1)
    assert deferred_history
    for route in deferred_history:
        route.fulfill(json={"project_id": first, "items": [], "next_cursor": None})
    page.unroute(history_url)
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    expect(page.locator("#pc-history-panel .pc-run-row")).to_have_count(1)
    page.evaluate("id => { window.SimonProjectCommand.open(id); }", first)
    expect(page.locator("#pc-tab-overview")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#pc-overview-panel")).to_be_visible()
    expect(page.locator("#pc-history-panel")).not_to_be_visible()


def test_backlog_notes_settings_and_pause_are_durable(project_ui):
    from playwright.sync_api import expect

    page, _, _, _, _ = project_ui
    create_project(page)
    page.get_by_role("button", name="Add task", exact=True).click()
    page.locator("#pc-todo-dialog").get_by_label("Task title", exact=True).fill(
        "Interview the first customer"
    )
    page.get_by_label("Task objective", exact=True).fill("Record goals and constraints.")
    page.get_by_label("Owner", exact=True).select_option("writer")
    page.get_by_role("button", name="Save task", exact=True).click()
    expect(page.locator("#pc-todo-dialog")).not_to_be_visible()
    page.get_by_role("button", name="Edit task: Interview the first customer", exact=True).click()
    page.get_by_label("Task status", exact=True).select_option("done")
    page.get_by_label("Result or handoff notes (optional)").fill("Customer wants a simple launch.")
    page.get_by_role("button", name="Save task", exact=True).click()
    expect(page.locator("#pc-panel-content .pc-status.done")).to_have_count(1)
    page.locator("#pc-tab-findings").click()
    page.get_by_role("button", name="Add entry", exact=True).click()
    page.get_by_label("Project note", exact=True).fill(
        "Keep the first release small. <script>bad()</script>"
    )
    page.get_by_role("button", name="Save entry", exact=True).click()
    expect(page.locator("#pc-panel-content")).to_contain_text("Keep the first release small")
    page.get_by_role("button", name="Team & autonomy", exact=True).click()
    expect(page.locator("#pc-settings-team-fields")).to_contain_text(
        "Research evidence, draft documents, and check the finished results."
    )
    expect(page.locator("#pc-settings-team-fields [data-pc-member]:checked")).to_have_count(1)
    page.locator("#pc-settings-team-fields .pc-roles summary").click()
    responsibilities = "Research reliable sources.\nDraft the report and verify its citations."
    page.locator("#pc-settings-dialog").get_by_label(
        "Report writer responsibilities", exact=True
    ).fill(responsibilities)
    page.get_by_label("Autonomy", exact=True).select_option("scheduled")
    expect(page.get_by_label("Standing objective", exact=True)).to_have_attribute("required", "")
    expect(page.get_by_label("Model budget per cycle (USD, required)")).to_have_attribute(
        "required", ""
    )
    page.get_by_label("Standing objective", exact=True).fill("Continue preparing the launch brief.")
    page.get_by_label("Check-in interval (minutes)").fill("30")
    page.get_by_label("Maximum cycles", exact=True).fill("3")
    page.get_by_label("Model budget per cycle (USD, required)").fill("2.50")
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#pc-settings-dialog")).not_to_be_visible()
    expect(page.locator("#pc-context")).to_contain_text("30 min")
    expect(page.locator("#pc-context")).to_contain_text("$2.50")
    page.locator("#pc-tab-overview").click()
    page.get_by_role("button", name="Pause project", exact=True).click()
    expect(page.locator("#pc-project-state")).to_contain_text("Paused")
    expect(page.locator("#pc-command-submit")).to_be_disabled()
    page.reload()
    expect(page.locator("#pc-project-state")).to_contain_text("Paused")
    page.get_by_role("button", name="Resume project", exact=True).click()
    expect(page.locator("#pc-command-submit")).to_be_enabled()
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Team & autonomy", exact=True).click()
    assert page.locator("#pc-settings-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
    expect(page.get_by_label("Report writer responsibilities")).to_have_value(responsibilities)
    expect(page.locator("#pc-settings-team-fields [data-pc-member]:checked")).to_have_count(1)
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Team & autonomy", exact=True)).to_be_focused()


def test_blocked_lead_plan_requires_a_recorded_review(project_ui):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, synthetic = project_ui
    create_project(page)
    synthetic["decision"] = {
        "status": "waiting",
        "summary": "A target market is needed before research.",
        "tasks": [],
    }
    page.locator("#pc-command").fill("Plan the launch.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    dispatcher.tick()
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-blockers")).to_contain_text("target market")
    expect(page.locator("#pc-command-submit")).to_be_disabled()
    expect(page.get_by_role("button", name="Start delegated work", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Review and clear hold", exact=True).click()
    page.get_by_label("What did you verify?", exact=True).fill(
        "No external action ran. We will specify the target market in the next request."
    )
    page.get_by_role("button", name="Record review and clear hold", exact=True).click()
    expect(page.locator("#pc-review-dialog")).not_to_be_visible()
    expect(page.locator("#pc-command-submit")).to_be_disabled()
    page.get_by_role("button", name="Resume project", exact=True).click()
    expect(page.locator("#pc-command-submit")).to_be_enabled()
    expect(page.locator("#pc-blockers")).not_to_be_visible()
    page.locator("#pc-tab-activity").click()
    expect(page.locator("#pc-panel-content")).to_contain_text("No external action ran")


def test_archive_activity_pagination_and_stale_settings(project_ui):
    from playwright.sync_api import expect

    page, _, _, _, _ = project_ui
    project_id = create_project(page)
    page.get_by_role("button", name="Add task", exact=True).click()
    page.locator("#pc-todo-title").fill("Finished milestone")
    page.locator("#pc-todo-objective").fill("Keep the decision for future reference.")
    page.get_by_label("Task status", exact=True).select_option("done")
    page.get_by_role("button", name="Save task", exact=True).click()
    expect(page.locator("#pc-todo-dialog")).not_to_be_visible()
    page.get_by_role("button", name="Edit task: Finished milestone", exact=True).click()
    page.get_by_label("Task status", exact=True).select_option("archived")
    page.get_by_role("button", name="Save task", exact=True).click()
    expect(page.locator("#pc-todo-dialog")).not_to_be_visible()
    expect(page.locator("#pc-panel-content .pc-task")).to_have_count(0)
    page.get_by_label("Filter project tasks").select_option("archived")
    page.get_by_role("button", name="Load archived tasks", exact=True).click()
    expect(page.locator("#pc-panel-content")).to_contain_text("Finished milestone")
    expect(page.locator("#pc-panel-content .pc-status.archived")).to_have_count(1)
    page.get_by_role("button", name="Team & autonomy", exact=True).click()
    page.locator("#pc-settings-team-name").fill("An outdated team change")
    page.evaluate(
        """async project => {
      for (let n = 0; n < 55; n++) await api('/v1/projects/' + project + '/activity', {
        entry: {kind: 'note', text: 'Timeline entry ' + n}, idempotency_key: 'timeline-entry-' + n
      });
    }""",
        project_id,
    )
    page.get_by_role("button", name="Save settings", exact=True).click()
    expect(page.locator("#pc-settings-error")).to_contain_text("Close and reopen settings")
    page.keyboard.press("Escape")
    expect(page.locator("#pc-context")).to_contain_text("Launch team")
    expect(page.locator("#pc-context")).not_to_contain_text("An outdated team change")
    page.locator("#pc-tab-activity").click()
    expect(page.locator("#pc-panel-content .pc-timeline li").first).to_contain_text(
        "Timeline entry 54"
    )
    expect(page.locator("#pc-panel-content")).not_to_contain_text("Timeline entry 0")
    page.get_by_role("button", name="Load earlier activity", exact=True).click()
    expect(page.locator("#pc-panel-content")).to_contain_text("Timeline entry 0")


def test_earlier_cycle_files_remain_available(project_ui):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, synthetic = project_ui
    create_project(page)
    page.locator("#pc-command").fill("Write our first brief.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    dispatcher.tick()
    scheduler.tick()
    refresh(page)
    page.get_by_role("button", name="Start delegated work", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("Approved")
    scheduler.tick()
    dispatcher.tick()
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-cycle")).to_contain_text("This cycle is complete")
    synthetic["decision"] = {
        "status": "complete",
        "summary": "The brief already covers the request.",
        "tasks": [],
    }
    page.locator("#pc-command").fill("Check whether we need anything else.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("waiting for the coordinator")
    scheduler.tick()
    dispatcher.tick()
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-cycle")).to_contain_text("Last cycle · 2")
    page.get_by_role("button", name="Load saved files", exact=True).first.click()
    with page.expect_download() as downloaded:
        page.locator("#pc-panel-content").get_by_role("link", name="answer.txt").first.click()
    assert "saved launch brief" in Path(downloaded.value.path()).read_text(encoding="utf-8")


def test_project_external_review_is_reachable_and_blocks_continuation(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, container, _ = project_ui
    project_id = create_project(page)
    page.locator("#pc-command").fill("Prepare the launch and propose a follow-up message.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    dispatcher.tick()
    scheduler.tick()
    refresh(page)
    page.get_by_role("button", name="Start delegated work", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("Approved")
    scheduler.tick()
    state = page.evaluate(
        "async id => (await api('/v1/projects/' + id + '/command')).state", project_id
    )
    actor = container.agent_runs.actor_resolver(
        UUID(state["actor_id"]), UUID(state["workspace_id"])
    )
    container.external_actions.propose(
        actor,
        ExternalActionDraft(
            kind="call",
            provider_id="unconfigured-phone",
            recipient_id="+12025550123",
            recipient_label="Synthetic project contact",
            summary="Review the project follow-up message",
            terms="A synthetic proposal. No phone service is configured.",
            call=CallDetails(message="This is a synthetic test message."),
        ),
        "project-browser-review",
        run_id=UUID(state["active_cycle"]["execution_run_id"]),
    )
    dispatcher.tick()
    scheduler.tick()
    page.reload()
    expect(page.locator("#pc-cycle")).to_contain_text("external action needs review")
    expect(page.locator("#pc-command-submit")).to_be_disabled()
    page.locator("#pc-cycle").get_by_role(
        "button", name="Review external action", exact=True
    ).click()
    expect(page.locator("#external-actions")).to_be_visible()
    expect(page.locator("#external-actions")).to_contain_text("Synthetic project contact")
    expect(page.get_by_role("button", name="Place reviewed call", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Cancel proposal", exact=True).click()
    expect(page.locator(".ea-detail")).to_contain_text("Proposal cancelled")
    page.screenshot(
        path=str(tmp_path / "project-overview-review-desktop.png"), animations="disabled"
    )
    page.get_by_role("button", name="Return to project", exact=True).click()
    expect(page.locator("#pc-project-title")).to_have_text("Launch studio")
    page.get_by_role("button", name="Review and clear hold", exact=True).click()
    page.get_by_label("What did you verify?", exact=True).fill(
        "Cancelled the unsent synthetic proposal."
    )
    page.get_by_role("button", name="Record review and clear hold", exact=True).click()
    expect(page.locator("#pc-review-dialog")).not_to_be_visible()
    page.get_by_role("button", name="Resume project", exact=True).click()
    expect(page.locator("#pc-command-submit")).to_be_enabled()
