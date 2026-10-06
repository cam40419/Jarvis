"""Project board UI against the real API, durable service and synthetic ClickUp HTTP."""

import json
from uuid import UUID

import httpx
import pytest

from simon.adapters.clickup import ClickUpAdapter
from simon.adapters.optional_http import BoundedHTTP
from simon.domain.project_boards import BoardConnection
from tests.integration.test_project_command_browser import create_project, refresh
from tests.integration.test_project_command_browser import project_ui as project_ui
from tests.unit.test_clickup import metadata, task

pytestmark = pytest.mark.browser


@pytest.fixture
def board_ui(project_ui):
    page, dispatcher, scheduler, container, synthetic = project_ui
    project_id = create_project(page, "Company launch")
    state = page.evaluate(
        "async id => (await api('/v1/projects/' + id + '/command')).state", project_id
    )
    actor_id, workspace_id = UUID(state["actor_id"]), UUID(state["workspace_id"])
    wire = {
        "tasks": {"market": task("market", name="Validate the target market")},
        "requests": [],
        "creates": [],
        "comments": [],
        "lose_next_create": False,
    }

    def send(request):
        wire["requests"].append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/v2/team":
            return httpx.Response(200, json={"teams": [{"id": "123"}]})
        if path == "/api/v2/team/123/space":
            return httpx.Response(200, json={"spaces": [{"id": "789", "archived": False}]})
        if path == "/api/v2/list/456":
            data = metadata(name="Launch delivery board")
            data["statuses"].append({"status": "blocked", "type": "custom", "color": "#886644"})
            return httpx.Response(200, json=data)
        if path == "/api/v2/list/456/task" and request.method == "GET":
            return httpx.Response(
                200, json={"tasks": list(wire["tasks"].values()), "last_page": True}
            )
        if path == "/api/v2/list/456/task" and request.method == "POST":
            body = json.loads(request.content)
            identifier = "created" + str(len(wire["creates"]) + 1)
            value = task(
                identifier,
                name=body["name"],
                description=body["description"],
                status={"status": body["status"], "type": "open"},
            )
            wire["tasks"][identifier] = value
            wire["creates"].append(identifier)
            if wire["lose_next_create"]:
                wire["lose_next_create"] = False
                raise httpx.ReadTimeout("Synthetic lost provider response", request=request)
            return httpx.Response(200, json=value)
        if path.startswith("/api/v2/task/"):
            identifier = path.split("/")[4]
            value = wire["tasks"][identifier]
            if request.method == "GET":
                return httpx.Response(200, json=value)
            body = json.loads(request.content)
            if request.method == "PUT":
                value["status"] = {
                    "status": body["status"],
                    "type": "closed" if body["status"] == "complete" else "custom",
                }
                value["date_updated"] = str(int(value["date_updated"]) + 1)
                return httpx.Response(200, json=value)
            if path.endswith("/comment"):
                wire["comments"].append(body["comment_text"])
                return httpx.Response(
                    200, json={"id": str(len(wire["comments"])), "date": "1780000001000"}
                )
            if path.endswith("/dependency"):
                value["dependencies"].append(
                    {"task_id": identifier, "depends_on": body["depends_on"], "type": 1}
                )
                return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected synthetic ClickUp call: {request.method} {path}")

    container.project_boards.adapter = ClickUpAdapter(
        BoundedHTTP(
            transport=httpx.MockTransport(send),
            environ={"PRIVATE_BOARD_TOKEN": "pk_synthetic-private-board-token"},
        )
    )
    container.project_boards.configured_connections = (
        BoardConnection(
            id="company",
            name="Company ClickUp",
            enabled=True,
            workspace_id=workspace_id,
            actor_ids=frozenset({actor_id}),
            credential_env="PRIVATE_BOARD_TOKEN",
            clickup_workspace_id="123",
            list_ids=frozenset({"456"}),
        ),
    )
    yield page, container, wire, project_id, dispatcher, scheduler, synthetic


def connect_board(page, *, sync_status=False):
    from playwright.sync_api import expect

    page.locator("#pc-tab-board").click()
    expect(page.get_by_role("button", name="Connect board", exact=True)).to_be_visible()
    page.get_by_role("button", name="Connect board", exact=True).click()
    page.get_by_label("Company connection", exact=True).select_option("company")
    expect(page.get_by_label("Project board / ClickUp list", exact=True)).to_be_enabled()
    page.get_by_label("Project board / ClickUp list", exact=True).select_option("456")
    if sync_status:
        page.locator("#pb-sync-status").check()
        for key, value in {
            "ready": "to do",
            "running": "in progress",
            "done": "complete",
            "blocked": "blocked",
        }.items():
            page.locator("#pb-map-" + key).select_option(value)
    page.get_by_role("button", name="Save board connection", exact=True).click()
    expect(page.locator("#pb-binding-dialog")).not_to_be_visible()
    expect(page.locator("#pb-heading")).to_have_text("Launch delivery board")


def add_execution_task(page, title):
    from playwright.sync_api import expect

    page.locator("#pc-tab-overview").click()
    page.get_by_role("button", name="Add task", exact=True).click()
    page.locator("#pc-todo-title").fill(title)
    page.locator("#pc-todo-objective").fill("Write a practical report with a clear next step.")
    page.get_by_role("button", name="Save task", exact=True).click()
    expect(page.locator("#pc-todo-dialog")).not_to_be_visible()


def test_bind_import_publish_progress_and_mobile_board(board_ui, tmp_path):
    from playwright.sync_api import expect

    page, _, wire, project_id, dispatcher, scheduler, synthetic = board_ui
    assert wire["requests"] == []  # Merely loading the project does not call ClickUp.
    connect_board(page)
    assert not wire["creates"] and not wire["comments"]
    expect(page.get_by_role("link", name="Open project board", exact=True)).to_have_attribute(
        "href", "https://app.clickup.com/123/v/li/456"
    )
    page.get_by_role("button", name="Preview board tasks", exact=True).click()
    expect(page.locator("#pb-review-dialog")).to_contain_text("Validate the target market")
    page.locator("#pb-review-dialog input[value=market]").check()
    page.get_by_role("button", name="Import selected tasks", exact=True).click()
    expect(page.locator("#pb-review-dialog")).not_to_be_visible()
    page.locator("#pc-tab-overview").click()
    expect(page.locator("#pc-panel-content")).to_contain_text("Managed in ClickUp")
    expect(page.get_by_role("link", name="Edit task in ClickUp", exact=True)).to_have_attribute(
        "href", "https://app.clickup.com/t/market"
    )
    add_execution_task(page, "Draft the launch report")
    page.locator("#pc-tab-board").click()
    page.get_by_role("button", name="Publish execution tasks", exact=True).click()
    page.locator("#pb-review-dialog input[type=checkbox]").check()
    assert not wire["creates"]
    page.get_by_role("button", name="Publish selected tasks", exact=True).click()
    expect(page.locator("#pb-review-dialog")).not_to_be_visible()
    assert len(wire["creates"]) == 1
    state = page.evaluate(
        "async project => (await api('/v1/projects/' + project + '/command')).state", project_id
    )
    todo = next(item for item in state["todos"] if item["title"] == "Draft the launch report")
    synthetic["decision"] = {
        "status": "plan",
        "summary": "Complete the existing board task.",
        "tasks": [
            {
                "id": "report",
                "title": todo["title"],
                "objective": todo["objective"],
                "todo_id": todo["id"],
                "agent_id": "writer",
                "depends_on": [],
                "tool_ids": [],
            }
        ],
    }
    page.locator("#pc-tab-overview").click()
    page.locator("#pc-command").fill("Complete our existing launch report task.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    page.get_by_role("button", name="Start delegated work", exact=True).click()
    expect(page.locator("#pc-cycle")).to_contain_text("Approved")
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    expect(page.locator("#pc-cycle")).to_contain_text("This cycle is complete")
    page.locator("#pc-tab-board").click()
    with page.expect_response("**/board/sync") as synced:
        page.get_by_role("button", name="Sync now", exact=True).click()
    assert synced.value.ok
    assert any("saved launch brief" in item for item in wire["comments"])
    assert "PRIVATE_BOARD_TOKEN" not in page.content()
    assert "pk_synthetic-private-board-token" not in page.content()
    page.reload()
    expect(page.locator("#pb-heading")).to_have_text("Launch delivery board")
    expect(page.locator("#pc-direct-board")).to_be_visible()
    page.locator("#pc-tab-board").click()
    page.locator("#pc-board-slot").evaluate(
        "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
    )
    page.screenshot(path=str(tmp_path / "project-board-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "project-board-mobile.png"), animations="disabled")
    page.get_by_role("button", name="Board settings", exact=True).click()
    expect(page.locator("#pb-binding-dialog")).to_be_visible()
    assert page.locator("#pb-binding-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Board settings", exact=True)).to_be_focused()


def test_uncertain_creation_is_held_and_can_attach_verified_remote_task(board_ui):
    from playwright.sync_api import expect

    page, _, wire, _, _, _, _ = board_ui
    connect_board(page)
    add_execution_task(page, "Review launch costs")
    page.locator("#pc-tab-board").click()
    wire["lose_next_create"] = True
    page.get_by_role("button", name="Publish execution tasks", exact=True).click()
    page.locator("#pb-review-dialog input[type=checkbox]").check()
    page.get_by_role("button", name="Publish selected tasks", exact=True).click()
    expect(page.locator("#pb-review-feedback")).to_contain_text("outcome review")
    expect(page.get_by_role("button", name="Publish selected tasks", exact=True)).to_be_disabled()
    assert len(wire["creates"]) == 1
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Sync now", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Review outcome", exact=True).click()
    page.get_by_label("Existing ClickUp task ID", exact=True).fill(wire["creates"][0])
    page.get_by_label("What did you verify in ClickUp?", exact=True).fill(
        "The task exists with its matching Simon reference. Link the saved task."
    )
    page.get_by_role("button", name="Record verified outcome", exact=True).click()
    expect(page.locator("#pb-reconcile-dialog")).not_to_be_visible()
    expect(page.get_by_role("button", name="Sync now", exact=True)).to_be_enabled()
    page.locator("#pc-tab-overview").click()
    expect(page.get_by_role("link", name="Edit task in ClickUp", exact=True)).to_have_attribute(
        "href", "https://app.clickup.com/t/created1"
    )
    assert len(wire["creates"]) == 1


def test_unconfigured_connection_explains_setup_and_cannot_bind(board_ui):
    from playwright.sync_api import expect

    page, container, wire, _, _, _, _ = board_ui
    container.project_boards.adapter.http.environ.clear()
    page.locator("#pc-tab-board").click()
    page.get_by_role("button", name="Connect board", exact=True).click()
    page.get_by_label("Company connection", exact=True).select_option("company")
    expect(page.locator("#pb-connection-reasons")).to_contain_text("credential is not configured")
    expect(page.get_by_role("button", name="Save board connection", exact=True)).to_be_disabled()
    expect(page.get_by_label("Project board / ClickUp list", exact=True)).to_be_disabled()
    assert wire["requests"] == []
