"""Focused owner decisions, precise version navigation and permission-safe summaries."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.native_projects import CreateNativeTask
from tests.integration.test_native_artifacts_browser import (
    artifact_view,
    seed_artifacts,
    upload_api,
)
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def open_inbox(ui, project, page=None):
    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-review-inbox").click()
    page.locator("#nr-refresh:not([disabled])").wait_for()


def test_inbox_json_changes_open_the_exact_current_revision_and_keep_text_inert(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False, content='{"price":20,"unchanged":"keep"}')
    latest = upload_api(
        ui,
        h,
        '{"price":25,"unchanged":"keep","note":"<img src=x onerror=window.inboxExecuted=1>"}',
        detail=h.detail,
        filename="spec.json",
        media_type="application/json",
    )
    open_inbox(ui, h.project)
    card = ui.page.locator(f'[data-inbox-id="{h.detail["artifact"]["id"]}"]')
    expect(card).to_contain_text("/price: 20")
    expect(card).to_contain_text("25")
    expect(card).to_contain_text("unknown")
    assert '\n+{"price":25,' in card.locator("pre").inner_text()
    assert card.locator("img").count() == 0
    assert ui.page.evaluate("window.inboxExecuted === undefined")
    screenshots = Path(".local/test-artifacts")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.screenshot(path=str(screenshots / "review-inbox-desktop.png"))
    ui.page.set_viewport_size({"width": 390, "height": 844})
    ui.page.screenshot(path=str(screenshots / "review-inbox-mobile.png"))
    assert ui.page.locator("#nr-dialog").evaluate(
        "node => node.scrollWidth <= node.clientWidth + 1"
    )
    card.get_by_role("button", name="Review exact revision").click()
    expect(ui.page.locator("#nr-dialog")).not_to_be_visible()
    expect(ui.page.locator("#na-detail")).to_be_visible()
    expect(ui.page.locator("#na-revision")).to_have_value(latest["artifact"]["current_revision_id"])
    assert not h.calls


@pytest.mark.parametrize("native_ui", ["/simon"], indirect=True)
def test_inbox_owner_can_configure_review_capacity_without_losing_other_policy_fields(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    original = artifact_view(ui, h.project)["policy"]
    open_inbox(ui, h.project)
    ui.page.locator("#nr-policy").click()
    expect(ui.page.locator("#na-policy-form")).to_be_visible()
    ui.page.locator("#na-review-queue-limit").fill("7")
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-policy-form")).not_to_be_visible()
    saved = artifact_view(ui, h.project)["policy"]
    assert saved["max_pending_reviews"] == 7
    assert all(
        saved[key] == original[key]
        for key in (
            "require_human_approval",
            "require_agent_review",
            "reviewer_agent_id",
            "max_repair_cycles",
        )
    )
    ui.page.locator("#na-close").click()
    ui.page.locator("#np-review-inbox").click()
    expect(ui.page.locator("#nr-refresh")).to_be_enabled()


def test_inbox_pagination_retains_unique_rows_and_counts_displayed_items(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    for index in range(21):
        task = ui.service.create_task(
            ui.actor,
            project.id,
            CreateNativeTask(
                title=f"Output {index}",
                idempotency_key=str(uuid4()),
            ),
        )
        upload_api(
            ui,
            SimpleNamespace(project=project, task=task),
            f"Draft {index}",
            filename=f"draft-{index}.txt",
        )
    open_inbox(ui, project)
    expect(ui.page.locator("[data-inbox-kind=artifact]")).to_have_count(20)
    ui.page.locator("#nr-artifacts-more").click()
    expect(ui.page.locator("[data-inbox-kind=artifact]")).to_have_count(21)
    expect(ui.page.locator("#nr-summary")).to_contain_text("21 need review")
    expect(ui.page.locator("#nr-artifacts-more")).not_to_be_visible()
    identifiers = ui.page.locator("[data-inbox-kind=artifact]").evaluate_all(
        "rows => rows.map(row => row.dataset.inboxId)"
    )
    assert len(set(identifiers)) == 21


def test_inbox_member_is_read_only_and_project_switch_clears_old_evidence(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False, content="Private first project output")
    member = ui.user(name="Read-only reviewer")
    share(ui, h.project, member)
    open_inbox(ui, h.project, page=member.page)
    expect(member.page.locator("[data-inbox-kind=artifact]")).to_have_count(1)
    expect(member.page.locator("#nr-policy")).to_be_disabled()
    second = seed_project(ui, name="Separate project")
    open_inbox(ui, h.project)
    ui.page.evaluate(
        "window.dispatchEvent(new CustomEvent('native-project-loading', {detail:{id:'"
        + str(second.id)
        + "'}}))"
    )
    expect(ui.page.locator("#nr-dialog")).not_to_be_visible()
    assert not ui.page.locator("#nr-groups").inner_text()
    open_inbox(ui, second)
    expect(ui.page.locator("[data-inbox-kind=artifact]")).to_have_count(0)
