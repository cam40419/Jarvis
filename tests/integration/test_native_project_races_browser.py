"""Late native responses cannot restore old views or unlock an unresolved edit."""

from contextlib import contextmanager
from uuid import uuid4

import pytest

from simon.domain.native_projects import UpdateNativeTask
from tests.integration.test_native_projects_browser import (
    native_ui as native_ui,
)
from tests.integration.test_native_projects_browser import (
    open_project,
    seed_project,
    seed_task,
    task_card,
)

pytestmark = pytest.mark.browser


@contextmanager
def delayed_reply(ui, path, *, method="GET"):
    """Commit/fetch the server result, then let the test control delivery to the page."""
    url = ui.origin + ui.path(path)
    held = {}

    def hold(route):
        if route.request.method != method or held:
            route.continue_()
            return
        response = route.fetch()
        assert response.status == 200
        held.update(route=route, response=response, released=False)
        ui.page.evaluate("document.documentElement.dataset.nativeDelayedReply = 'ready'")

    def release():
        assert held and not held["released"]
        held["released"] = True
        with ui.page.expect_response(
            lambda response: response.url == url and response.request.method == method
        ):
            held["route"].fulfill(response=held["response"])
        # Let response callbacks and the next paint run before checking the retained view.
        ui.page.evaluate(
            "async () => { await new Promise(requestAnimationFrame); "
            "await new Promise(requestAnimationFrame); }"
        )

    ui.page.route(url, hold)
    try:
        yield release
    finally:
        if held and not held["released"]:
            held["route"].fulfill(response=held["response"])
        ui.page.unroute(url, hold)


def test_late_successful_claim_does_not_replace_newly_selected_project(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    original = seed_project(ui, "Original claim project")
    task = seed_task(ui, original, "Claim before navigating")
    destination = seed_project(ui, "Newly selected project")
    destination_task = seed_task(ui, destination, "Keep this destination visible")
    open_project(ui, original)
    with delayed_reply(
        ui, f"/v2/projects/{original.id}/tasks/{task.id}/claim", method="POST"
    ) as release:
        task_card(ui.page, task).get_by_role("button", name="Pick up task", exact=True).click()
        expect(ui.page.locator("html")).to_have_attribute("data-native-delayed-reply", "ready")
        committed = ui.service.get_task(ui.actor, original.id, task.id)
        assert committed.status == "in_progress"
        assert committed.assignment.actor_id == ui.actor.actor_id
        ui.page.locator("#np-back").click()
        expect(ui.page.locator("#np-list")).to_be_visible()
        ui.page.get_by_role("link", name=destination.name, exact=True).click()
        expect(ui.page.locator("#np-name")).to_have_text(destination.name)
        expect(task_card(ui.page, destination_task)).to_be_visible()
        release()
        expect(ui.page.locator("#np-detail")).to_be_visible()
        expect(ui.page.locator("#np-name")).to_have_text(destination.name)
        expect(task_card(ui.page, destination_task)).to_be_visible()
        expect(task_card(ui.page, task)).to_have_count(0)
        assert ui.service.get_task(ui.actor, original.id, task.id).version == 2


def test_late_team_access_response_does_not_open_dialog_after_navigating_back(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_project(ui, project)
    with delayed_reply(ui, f"/v2/projects/{project.id}/access") as release:
        ui.page.locator("#np-team").click()
        expect(ui.page.locator("html")).to_have_attribute("data-native-delayed-reply", "ready")
        expect(ui.page.locator("#np-team")).to_be_disabled()
        expect(ui.page.locator("#np-team-dialog")).not_to_be_visible()
        ui.page.locator("#np-back").click()
        expect(ui.page.locator("#np-list")).to_be_visible()
        release()
        expect(ui.page.locator("#np-team-dialog")).not_to_be_visible()
        expect(ui.page.locator("#np-detail")).not_to_be_visible()
        expect(ui.page.locator("#np-list")).to_be_visible()
        expect(ui.page.get_by_role("link", name=project.name, exact=True)).to_be_visible()


def test_conflict_lookup_keeps_dialog_locked_until_latest_record_arrives(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    edited = seed_task(ui, project, "Task being edited")
    other = seed_task(ui, project, "A different task")
    open_project(ui, project)
    task_card(ui.page, edited).locator(".np-task-open").click()
    draft = "Keep this draft tied to its original task while the conflict is loading."
    ui.page.locator("#np-task-description").fill(draft)
    updated = ui.service.update_task(
        ui.actor,
        project.id,
        edited.id,
        UpdateNativeTask(
            title=edited.title,
            description="A colleague's latest record.",
            status="todo",
            assignment=edited.assignment,
            expected_version=edited.version,
            idempotency_key=str(uuid4()),
        ),
    )
    with delayed_reply(ui, f"/v2/projects/{project.id}/tasks/{edited.id}") as release:
        ui.page.locator("#np-task-save").click()
        expect(ui.page.locator("html")).to_have_attribute("data-native-delayed-reply", "ready")
        expect(ui.page.locator("#np-task-form")).to_have_attribute("aria-busy", "true")
        for selector in (
            "#np-task-title",
            "#np-task-description",
            "#np-task-assignee",
            "#np-task-status",
            "#np-task-save",
            '[data-close="np-task-dialog"]',
        ):
            expect(ui.page.locator(selector)).to_be_disabled()
        ui.page.keyboard.press("Escape")
        expect(ui.page.locator("#np-task-dialog")).to_be_visible()
        expect(ui.page.locator("#np-task-description")).to_have_value(draft)
        assert ui.service.get_task(ui.actor, project.id, edited.id) == updated
        assert ui.service.get_task(ui.actor, project.id, other.id) == other
        release()
        expect(ui.page.locator("#np-task-conflict")).to_be_visible()
        expect(ui.page.locator("#np-task-latest")).to_contain_text("colleague's latest record")
        expect(ui.page.locator("#np-task-form")).to_have_attribute("aria-busy", "false")
        expect(ui.page.locator("#np-task-save")).to_be_disabled()
        expect(ui.page.locator("#np-task-rebase")).to_be_enabled()
        expect(ui.page.locator("#np-task-description")).to_have_value(draft)
        ui.page.locator('[data-close="np-task-dialog"]').click()
        task_card(ui.page, other).locator(".np-task-open").click()
        expect(ui.page.locator("#np-task-title")).to_have_value(other.title)
        expect(ui.page.locator("#np-task-description")).to_have_value(other.description)
        expect(ui.page.locator("#np-task-conflict")).not_to_be_visible()
        ui.page.locator("#np-task-description").fill("An independent edit to the other task.")
        ui.page.locator("#np-task-save").click()
        expect(ui.page.locator("#np-task-dialog")).not_to_be_visible()
        assert ui.service.get_task(ui.actor, project.id, edited.id) == updated
        assert ui.service.get_task(ui.actor, project.id, other.id).description == (
            "An independent edit to the other task."
        )
