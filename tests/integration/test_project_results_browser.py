"""Read answers and planning questions without downloading opaque records."""

import json

import pytest

from simon.domain.agent_worker import WorkerResult
from tests.integration.test_project_command_browser import create_project, refresh
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_empty_protocol_failure_explains_problem_and_exposes_technical_record(project_ui):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, _ = project_ui

    class Worker:
        def execute(self, **_kwargs):
            return WorkerResult(status="failed", error_code="invalid_controller_response", steps=1)

    dispatcher.worker_factory = lambda *_: Worker()
    create_project(page, "Protocol failure")
    page.locator("#pc-command").fill("Review the project files.")
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "failed"
    scheduler.tick()
    refresh(page)
    latest = page.locator("#pc-latest-result")
    expect(latest).to_have_attribute("data-response-kind", "error")
    expect(latest).to_contain_text("Planning could not finish")
    expect(latest).to_contain_text("Simon status")
    expect(latest.locator(".sr-answer-body")).to_contain_text(
        "could not interpret as a tool action or final answer"
    )
    expect(latest).to_contain_text("No answer was saved")
    expect(latest.get_by_role("button", name="Copy answer", exact=True)).to_have_count(0)
    expect(latest.get_by_role("button", name="Copy error details", exact=True)).to_be_visible()
    expect(latest.locator(".sr-plan-source dd").first).not_to_be_visible()
    latest.get_by_text("Technical error details", exact=True).click()
    expect(latest.locator(".sr-plan-source")).to_contain_text("invalid_controller_response")
    expect(latest.locator(".sr-plan-source")).to_contain_text("Recorded tool calls")
    expect(latest.locator(".sr-plan-source dd").nth(3)).to_have_text("0")
    expect(page.get_by_role("button", name="Retry with current access", exact=True)).to_be_visible()


@pytest.mark.parametrize(
    ("raw", "readable"),
    [
        ('{"status":"plan","tasks":[', "plan"),
        ('{"status":"unexpected","summary":"Choose the launch date."}', "Choose the launch date."),
        ("I need a launch date before planning.", "I need a launch date before planning."),
    ],
    ids=["malformed-json", "unknown-status-missing-tasks", "plain-language"],
)
def test_malformed_planning_records_stay_technical_in_project_and_agent_views(
    project_ui, raw, readable
):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, _ = project_ui

    class Worker:
        def execute(self, **_kwargs):
            return WorkerResult(status="succeeded", output=raw, steps=1)

    dispatcher.worker_factory = lambda *_: Worker()
    create_project(page, "Malformed planning response")
    page.locator("#pc-command").fill("Prepare the launch plan.")
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    latest = page.locator("#pc-latest-result")
    expect(latest.locator(".sr-answer-body")).to_contain_text(readable)
    expect(latest.locator(".sr-answer-body")).not_to_contain_text('"status"')
    expect(latest.locator(".sr-plan-source pre")).to_have_text(raw)
    expect(latest.locator(".sr-plan-source")).not_to_have_attribute("open", "")
    expect(latest.locator(".sr-plan-source a")).not_to_be_visible()
    expect(page.get_by_role("button", name="Retry with current access", exact=True)).to_be_visible()
    latest.get_by_text("Technical planning record", exact=True).click()
    expect(latest.locator(".sr-plan-source pre")).to_be_visible()
    expect(latest.locator(".sr-plan-source a")).to_contain_text("Download planning record")

    # The global run viewer must keep the same semantics without presentation metadata.
    page.locator("#sidebar-agents").click()
    page.locator("#agent-tab-runs").click()
    page.locator("#agent-refresh").click()
    result = page.locator("#agent-detail .sr-task-result")
    expect(result.locator(".sr-answer-body")).to_contain_text(readable)
    expect(result.locator(".sr-answer-body")).not_to_contain_text('"status"')
    expect(result.locator(".sr-plan-source pre")).to_have_text(raw)
    expect(result.locator(".sr-plan-source a")).not_to_be_visible()


def test_planning_question_is_readable_on_overview_and_safe_in_file_preview(project_ui):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, _ = project_ui
    project_id = create_project(page, "Readable brand research")
    decision = {
        "status": "waiting",
        "summary": "## One detail needed\nWhich launch date should I use?\n"
        '<img src="x" onerror="window.resultInjected=true">\n'
        "[unsafe](javascript:alert) [reference](https://example.com/report)",
        # Reproduce the existing inconsistent planning record reported by the user.
        "tasks": [
            {
                "id": "research",
                "title": "Find project sources",
                "agent_id": "writer",
                "objective": "Read existing source documents.",
                "depends_on": [],
                "tool_ids": [],
            }
        ],
    }

    # Restore a legacy saved record directly, independently of today's generation schema.
    class Worker:
        def execute(self, **_kwargs):
            return WorkerResult(status="succeeded", output=json.dumps(decision), steps=1)

    dispatcher.worker_factory = lambda *_: Worker()
    page.locator("#pc-command").fill("Prepare the clothing launch plan.")
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    latest = page.locator("#pc-latest-result")
    expect(latest).to_be_visible()
    expect(latest.locator(".sr-answer-body")).to_contain_text("Which launch date should I use?")
    expect(latest).to_have_attribute("data-response-kind", "needs_input")
    expect(latest.locator(".sr-answer-body")).not_to_contain_text('"status"')
    expect(latest.locator("img")).to_have_count(0)
    expect(latest.locator('a[href^="javascript:"]')).to_have_count(0)
    expect(latest.get_by_role("link", name="reference", exact=True)).to_be_visible()
    expect(latest.locator(".sr-plan-source a")).not_to_be_visible()
    assert page.evaluate("window.resultInjected === undefined")

    # An unchanged status poll must preserve the answer DOM and selection.
    page.evaluate("document.querySelector('#pc-latest-result .sr-answer-body').dataset.kept='yes'")
    refresh(page)
    expect(latest.locator(".sr-answer-body")).to_have_attribute("data-kept", "yes")
    page.locator("#pc-tab-files").click()
    listing = page.evaluate("id => api('/v1/projects/' + id + '/outputs')", project_id)
    artifact = listing["items"][0]
    row = page.locator(f'.po-output[data-output="{artifact["id"]}"]')
    row.get_by_role("button", name="Preview answer.txt", exact=True).click()
    expect(row.locator(".sr-answer-body")).to_contain_text("Which launch date should I use?")
    expect(row.locator("img")).to_have_count(0)
    expect(row.get_by_role("link", name="Download original", exact=True)).to_be_visible()


def test_completed_answer_is_visible_before_history_or_download_and_survives_reload(
    project_ui, tmp_path
):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, _ = project_ui
    create_project(page, "Inline completed report")
    page.locator("#pc-command").fill("Prepare the launch report.")
    page.locator("#pc-command-submit").click()
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
    latest = page.locator("#pc-latest-result")
    expect(latest).to_be_visible()
    expect(latest).to_have_attribute("data-response-kind", "answer")
    expect(latest.locator(".sr-answer-body").first).to_contain_text("A useful, saved launch brief.")
    expect(latest.get_by_role("button", name="Copy answer", exact=True).first).to_be_visible()
    assert page.locator("#pc-tab-history").get_attribute("aria-selected") != "true"
    page.reload()
    expect(latest.locator(".sr-answer-body").first).to_contain_text("A useful, saved launch brief.")
    page.screenshot(path=str(tmp_path / "inline-results-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(latest).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "inline-results-mobile.png"), animations="disabled")
    page.locator("#pc-command").fill("Research the next season.")
    page.locator("#pc-command-submit").click()
    expect(latest.locator(".sr-answer-body")).to_have_count(0)
    expect(latest).not_to_contain_text("A useful, saved launch brief.")


def test_inline_preview_error_is_retryable_without_downloading(project_ui):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, _, synthetic = project_ui
    project_id = create_project(page, "Retry inline preview")
    synthetic["decision"] = {"status": "waiting", "summary": "Choose a budget.", "tasks": []}
    page.locator("#pc-command").fill("Prepare a plan.")
    page.locator("#pc-command-submit").click()
    expect(page.locator("#pc-cycle")).to_be_visible()
    scheduler.tick()
    assert dispatcher.tick().status == "succeeded"
    scheduler.tick()
    refresh(page)
    page.locator("#pc-tab-files").click()
    listing = page.evaluate("id => api('/v1/projects/' + id + '/outputs')", project_id)
    artifact = listing["items"][0]
    row = page.locator(f'.po-output[data-output="{artifact["id"]}"]')
    route = f"**/artifacts/{artifact['id']}/preview"
    page.route(
        route,
        lambda request: request.fulfill(
            status=503, json={"error": {"message": "Preview temporarily unavailable"}}
        ),
    )
    row.get_by_role("button", name="Preview answer.txt", exact=True).click()
    expect(row).to_contain_text("Preview temporarily unavailable")
    page.unroute(route)
    row.get_by_role("button", name="Retry preview", exact=True).click()
    expect(row.locator(".sr-answer-body")).to_contain_text("Choose a budget.")
