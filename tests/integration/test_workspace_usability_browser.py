"""Workspace navigation and project recovery through authenticated local services."""

from uuid import UUID

import pytest

from tests.integration.test_agent_library_browser import agent_library_ui as agent_library_ui
from tests.integration.test_project_command_browser import create_project, refresh
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def invalid_dependency_plan(summary):
    # Valid generation shape, rejected by the coordinator's dependency checks.
    # Waiting decisions now create durable questions and intentionally do not block.
    return {
        "status": "plan",
        "summary": summary,
        "tasks": [
            {
                "id": "source-review",
                "title": "Review available sources",
                "agent_id": "writer",
                "objective": "Review the supplied project sources.",
                "depends_on": ["source-review"],
                "tool_ids": [],
                "todo_id": None,
            }
        ],
    }


def test_workspace_navigation_separates_projects_agents_and_background_work(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, *_ = project_ui
    create_project(page, "Launch workspace")
    expect(page.locator("#pc-project-title")).to_be_in_viewport()
    page.locator("#project-back").click()
    expect(page.locator("#project-page")).not_to_be_visible()
    expect(page.locator("#pc-project-list")).to_contain_text("Launch workspace")
    expect(page.locator("#pc-command-form")).not_to_be_visible()
    expect(page.locator("#work-projects-open")).to_be_in_viewport()
    expect(page.locator("#work-task-form")).not_to_be_visible()
    expect(page.locator(".work-home-nav #local-files-open")).to_be_visible()
    page.locator("#sidebar-agents").click()
    expect(page.locator("#agent-tab-agents")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#al-new-agent")).to_be_visible()
    page.locator("#work-connections-open").click()
    expect(page.locator("#connections-panel")).to_be_visible()
    page.keyboard.press("Escape")
    page.locator("#work-background-open").click()
    expect(page.locator("#work-background-tools")).to_have_attribute("open", "")
    expect(page.locator("#work-task-form")).to_be_visible()
    page.locator("#work-background-tools > summary").click()
    page.locator("#sidebar-projects").click()
    expect(page.locator("#work-projects-open")).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "workspace-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "workspace-mobile.png"), animations="disabled")
    page.locator("#pc-project-list button").first.click()
    expect(page.locator("#pc-project-title")).to_have_text("Launch workspace")
    # Every section is available in the mobile navigation without horizontal scrolling.
    page.get_by_label("Project section", exact=True).select_option("team")
    expect(page.locator("#pc-team-panel")).to_contain_text("skills and connection requirements")
    page.get_by_label("Project section", exact=True).select_option("files")
    expect(page.locator("#pc-files-panel")).to_be_visible()


def test_blocked_plan_explains_access_and_retries_original_request_once(
    agent_library_ui, project_ui, tmp_path
):
    from playwright.sync_api import expect

    page, container = agent_library_ui
    container.agent_platform.models.endpoints = tuple(
        endpoint.model_copy(update={"capabilities": frozenset({"text", "tools"})})
        for endpoint in container.agent_platform.models.endpoints
    )
    _, dispatcher, scheduler, _, synthetic = project_ui
    project_id = create_project(page, "Project access review")
    original = "Read the project evidence and prepare a launch recommendation."
    synthetic["decision"] = invalid_dependency_plan(
        "I need access to the source files before I can finish this request."
    )
    page.locator("#pc-command").fill(original)
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    planning_result = dispatcher.tick()
    assert planning_result.status == "succeeded", [
        (task.id, task.error_code) for task in planning_result.tasks
    ]
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-project-state")).to_have_text("Needs attention")
    expect(page.locator("#pc-cycle")).to_contain_text(original)
    expect(page.locator("#pc-latest-result")).to_contain_text("access to the source files")
    expect(page.locator("#pc-latest-result .sr-answer-body")).not_to_contain_text('"status"')
    retry = page.get_by_role("button", name="Retry with current access", exact=True)
    expect(retry).to_be_visible()
    expect(page.locator("#pc-cycle")).to_contain_text("Scheduled work stays off")
    page.locator("#pc-tab-overview").click()
    expect(page.locator("#pc-project-title")).to_be_in_viewport()
    expect(page.locator("#pc-latest-result")).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "blocked-project-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    page.locator("#pc-latest-result").evaluate(
        "element => element.scrollIntoView({block: 'start', behavior: 'instant'})"
    )
    expect(page.locator("#pc-latest-result .sr-answer-body")).to_be_in_viewport()
    expect(retry).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "blocked-project-mobile.png"), animations="disabled")

    # Unknown outcomes must not inherit the one-click retry available for settled planning.
    unknown = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)
    unknown["state"]["last_cycle"]["phase"] = "unknown"
    unknown["presentation"]["phase"] = "unknown"
    unknown["presentation"]["status_label"] = "Outcome needs review"
    unknown["presentation"]["recovery"]["can_retry"] = False
    unknown["presentation"]["recovery"]["blocked_reasons"] = [
        "Review the uncertain planning outcome before continuing"
    ]
    pattern = "**/v1/projects/" + project_id + "/command"
    page.route(pattern, lambda route: route.fulfill(json=unknown))
    refresh(page)
    expect(retry).to_have_count(0)
    expect(page.locator("#pc-project-state")).to_have_text("Outcome needs review")
    page.unroute(pattern)
    refresh(page)

    page.get_by_role("button", name="Check team access", exact=True).click()
    expect(page.locator("#pc-team-panel")).to_be_visible()
    page.get_by_role("button", name="Edit role: Report writer", exact=True).click()
    member = page.locator("#pc-member-dialog")
    expect(member.locator("#pc-member-count")).to_have_text("All workspace tools")
    instructions = "Read the available local evidence and write a reviewed launch recommendation."
    member.get_by_label("Working instructions", exact=True).fill(instructions)
    member.get_by_role("button", name="Save team member", exact=True).click()
    page.locator("#pc-settings-dialog").get_by_role(
        "button", name="Save settings", exact=True
    ).click()
    expect(page.locator("#pc-settings-dialog")).not_to_be_visible()
    page.get_by_label("Project section", exact=True).select_option("overview")
    retry.click()
    expect(page.locator("#pc-cycle")).to_contain_text("waiting for the coordinator")
    expect(retry).to_have_count(0)
    current = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"]
    assert current["active_cycle"]["instruction"] == original
    assert current["autonomy"]["mode"] == "manual"
    assert not current["autonomy"]["paused"]
    assert current["team"]["members"]["writer"]["description"] == instructions
    scheduler.tick()
    current = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)
    assert current["state"]["active_cycle"] is not None, current["state"]["last_cycle"]["error"]
    planning = next(
        plan
        for plan in current["plans"]
        if plan["id"] == current["state"]["active_cycle"]["planning_plan_id"]
    )
    assert "native.local_file_read" in planning["tasks"][0]["tool_ids"]


def test_new_request_is_reachable_and_draft_survives_active_work(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, _ = project_ui
    project_id = create_project(page, "Request workspace")
    page.locator("#pc-tab-files").click()
    expect(page.locator("#pc-command")).not_to_be_visible()
    page.get_by_role("button", name="New request", exact=True).click()
    expect(page.locator("#pc-command")).to_be_focused()
    expect(page.locator("#pc-command")).to_be_in_viewport()
    page.locator("#pc-command").fill("Prepare the first brief.")
    page.locator("#pc-command-submit").click()
    # Queued follow-ups are available after the accepted cycle renders;
    # in-flight edits have a separate regression.
    expect(page.locator("#pc-command-note")).to_contain_text("saved in the queue")
    page.locator("#pc-command").fill("Assess the next product launch.")
    expect(page.locator("#pc-command-status")).to_have_text("")
    expect(page.locator("#pc-command-note")).to_contain_text("saved in the queue")
    expect(page.get_by_role("button", name="Review current request", exact=True)).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-command-note")).to_contain_text("saved in the queue")
    page.get_by_role("button", name="Start delegated work", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("Approved")
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-command-submit")).to_be_enabled()
    expect(page.locator("#pc-command")).to_have_value("Assess the next product launch.")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Project section", exact=True).select_option("history")
    page.get_by_role("button", name="New request", exact=True).click()
    expect(page.locator("#pc-command")).to_be_focused()
    expect(page.locator("#pc-command-submit")).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "next-request-mobile.png"), animations="disabled")
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-cycle")).to_contain_text("waiting for the coordinator")
    state = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"]
    assert state["active_cycle"]["instruction"] == "Assess the next product launch."
    assert state["active_cycle"]["automatic"] is False


def test_replacing_failed_planning_preserves_draft_after_version_conflict(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, synthetic = project_ui
    project_id = create_project(page, "Replace failed planning")
    synthetic["decision"] = invalid_dependency_plan("The original request needs a launch date.")
    page.locator("#pc-command").fill("Prepare the launch.")
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    page.get_by_role("button", name="New request", exact=True).click()
    expect(page.locator("#pc-command")).to_be_focused()
    expect(page.locator("#pc-command-submit")).to_have_text("Replace failed request")
    expect(page.locator("#pc-command-submit")).to_be_enabled()
    expect(page.locator("#pc-command-note")).to_contain_text("Earlier attempts stay in History")
    replacement = "Assess available project evidence and prepare a launch for December."
    page.locator("#pc-command").fill(replacement)
    old_version = page.evaluate("SimonProjectCommand.getSnapshot().detail.state.version")
    page.evaluate(
        """id => api('/v1/projects/' + id + '/activity', {
            entry:{kind:'note',text:'Updated evidence'},
            idempotency_key:'replacement-version-change'
        })""",
        project_id,
    )
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/command")
    ) as rejected:
        page.locator("#pc-command-submit").click()
    assert rejected.value.status in {400, 409, 422}
    expect(page.locator("#pc-command-status")).to_contain_text("Your draft has been kept")
    expect(page.locator("#pc-command")).to_have_value(replacement)
    expect(page.locator("#pc-command-submit")).to_be_enabled()
    page.wait_for_function(
        "old => SimonProjectCommand.getSnapshot().detail.state.version > old", arg=old_version
    )
    page.screenshot(path=str(tmp_path / "replacement-request-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="New request", exact=True).click()
    expect(page.locator("#pc-command-submit")).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "replacement-request-mobile.png"), animations="disabled")
    with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith("/command")
    ) as submitted:
        page.locator("#pc-command-submit").click()
    assert submitted.value.post_data_json["replace_failed"] is True
    assert submitted.value.post_data_json["expected_version"] > old_version
    expect(page.locator("#pc-cycle")).to_contain_text("waiting for the coordinator")
    state = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"]
    assert state["active_cycle"]["instruction"] == replacement
    assert state["active_cycle"]["automatic"] is False
    assert not state["autonomy"]["paused"] and state["autonomy"]["mode"] == "manual"
    assert not state["blocked_reasons"]
    assert state["last_cycle"]["instruction"] == "Prepare the launch."


def test_agent_project_details_refresh_without_losing_request_draft(project_ui):
    from playwright.sync_api import expect

    from simon.domain.project_details import UpdateProjectDetails
    from simon.services.project_details import ProjectDetailsService

    page, _, _, container, _ = project_ui
    project_id = create_project(page, "Initial research project")
    page.wait_for_function("id => SimonWork.getProject()?.id === id", arg=project_id)
    state = page.evaluate("SimonProjectCommand.getSnapshot().detail.state")
    actor = container.agent_runs.actor_resolver(
        UUID(state["actor_id"]), UUID(state["workspace_id"])
    )
    details = ProjectDetailsService(container.project_work)
    draft = "Keep this request while the project details change."
    page.locator("#pc-command").fill(draft)
    page.locator("#pc-command").focus()
    updated = details.update(
        actor,
        UUID(project_id),
        UpdateProjectDetails(
            expected_version=0,
            idempotency_key="browser-agent-project-details",
            name="Updated research project",
            description="Research the audience and prepare an evidence-based launch brief.",
        ),
        agent_id="writer",
    )
    page.evaluate("SimonProjectCommand.refresh(true)")
    expect(page.locator("#pc-project-title")).to_have_text(updated.name)
    expect(page.locator("#pc-project-description")).to_have_text(updated.description)
    expect(page.locator("#chat-title")).to_have_text(updated.name)
    expect(page.locator("#pc-project-list")).to_contain_text(updated.name)
    expect(page.locator("#pc-command")).to_have_value(draft)
    expect(page.locator("#pc-command")).to_be_focused()
    assert page.evaluate("SimonWork.getProject().subject") == updated.name

    # A control action refreshes only /command, so its metadata must also update
    # the saved project list and resource cache without fetching the whole list.
    cleared = details.update(
        actor,
        UUID(project_id),
        UpdateProjectDetails(
            expected_version=updated.version,
            idempotency_key="browser-agent-project-description-clear",
            name="Revised research project",
            description="",
        ),
        agent_id="writer",
    )
    with page.expect_response("**/command"):
        page.locator(".pc-summary-actions").get_by_role(
            "button", name="Pause project", exact=True
        ).evaluate("button => button.click()")
    expect(page.locator("#pc-project-title")).to_have_text(cleared.name)
    expect(page.locator("#pc-project-description")).to_have_text("")
    expect(page.locator("#chat-title")).to_have_text(cleared.name)
    expect(page.locator("#pc-project-list")).to_contain_text(cleared.name)
    expect(page.locator("#pc-project-list")).not_to_contain_text(updated.name)
    expect(page.locator("#pc-command")).to_have_value(draft)
    expect(page.locator("#pc-command")).to_be_focused()
    assert page.evaluate("SimonWork.getProject().content") == ""


def test_edits_while_request_is_sending_remain_a_separate_draft(project_ui):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page, "Keep the next draft")
    pending = []

    def hold_submission(route):
        if route.request.method == "POST":
            pending.append(route)
        else:
            route.continue_()

    page.route(f"**/v1/projects/{project_id}/command", hold_submission)
    original = "Prepare an evidence-based launch brief."
    newer = "Next, assess the audience for a December launch."
    page.locator("#pc-command").fill(original)
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-command-status")).to_contain_text("Sending your request")
    expect(page.locator("#pc-command-submit")).to_be_disabled()
    page.locator("#pc-command").fill(newer)
    assert len(pending) == 1
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/command")
    ) as accepted:
        pending[0].continue_()
    assert accepted.value.status == 202
    assert accepted.value.json()["active_cycle"]["instruction"] == original
    expect(page.locator("#pc-command-status")).to_contain_text("Your newer draft is still here")
    expect(page.locator("#pc-command")).to_have_value(newer)
    expect(page.locator("#pc-command")).to_be_focused()
    expect(page.locator("#pc-command-note")).to_contain_text("saved in the queue")
    # The per-project draft cache must also keep these edits when the project is reopened.
    page.locator("#project-back").click()
    page.locator("#pc-project-list button").filter(has_text="Keep the next draft").click()
    expect(page.locator("#pc-project-title")).to_have_text("Keep the next draft")
    expect(page.locator("#pc-command")).to_have_value(newer)


def test_accepted_request_with_failed_status_refresh_is_not_reported_as_rejected(project_ui):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page, "Accepted request status")
    submitted = []
    fail_status = False

    def fail_following_status(route):
        nonlocal fail_status
        if route.request.method == "POST":
            response = route.fetch()
            submitted.append(response.json())
            fail_status = True
            route.fulfill(response=response)
        elif fail_status:
            fail_status = False
            route.fulfill(status=503, json={"detail": "Synthetic status refresh unavailable"})
        else:
            route.continue_()

    page.route(f"**/v1/projects/{project_id}/command", fail_following_status)
    page.locator("#pc-command").fill("Prepare the next launch brief.")
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-command-status")).to_contain_text(
        "Request saved, but its latest status could not be loaded"
    )
    expect(page.locator("#pc-command-status")).not_to_contain_text("Your draft has been kept")
    expect(page.locator("#pc-command")).to_have_value("")
    expect(page.locator("#pc-command-submit")).to_be_disabled()
    expect(page.locator("#pc-command-note")).to_contain_text("Your request was saved")
    assert len(submitted) == 1
    assert submitted[0]["active_cycle"]["instruction"] == "Prepare the next launch brief."
    page.locator("#pc-command").fill("Draft the next request while status is unavailable.")
    page.get_by_role("button", name="Refresh request status", exact=True).click()
    expect(page.locator("#pc-command-status")).to_have_text(
        "Request saved. Project status is up to date."
    )
    expect(page.locator("#pc-command-note")).to_contain_text("saved in the queue")
    expect(page.locator("#pc-command")).to_have_value(
        "Draft the next request while status is unavailable."
    )
    assert len(submitted) == 1
