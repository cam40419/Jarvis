"""Real API regressions for durable drafts, agent cards, and upcoming work."""

import pytest

from tests.integration.test_project_command_browser import create_project, refresh
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_composer_draft_reload_switch_and_conflict(project_ui):
    from playwright.sync_api import expect

    page, *_ = project_ui
    first = create_project(page, "Saved drafts")
    field = page.locator("#pc-command")
    field.fill("Retain the launch constraints.")
    expect(page.locator("#pc-draft-status")).to_have_text("Draft saved.")
    page.reload()
    expect(field).to_have_value("Retain the launch constraints.")
    page.evaluate("SimonWork.showOverview()")
    second = create_project(page, "Independent drafts")
    field.fill("A separate project request.")
    expect(page.locator("#pc-draft-status")).to_have_text("Draft saved.")
    page.evaluate("id => SimonProjectCommand.open(id)", first)
    expect(field).to_have_value("Retain the launch constraints.")
    page.evaluate(
        """async id => { const path='/v1/projects/'+id+'/workspace/drafts/composer';
        const draft=await api(path); await api(path,{expected_version:draft.version,
        text:'Saved on another device.',idempotency_key:crypto.randomUUID()},'PUT'); }""",
        first,
    )
    field.fill("Keep this newer local revision.")
    expect(page.locator("#pc-draft-status")).to_contain_text("different draft was saved elsewhere")
    expect(field).to_have_value("Keep this newer local revision.")
    page.get_by_role("button", name="Keep my draft", exact=True).click()
    expect(page.locator("#pc-draft-status")).to_have_text("Draft saved.")
    page.reload()
    expect(field).to_have_value("Keep this newer local revision.")
    page.evaluate("id => SimonProjectCommand.open(id)", second)
    expect(field).to_have_value("A separate project request.")


def test_offline_draft_is_restored_and_read_only_does_not_sync(project_ui):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page)
    path = f"**/v1/projects/{project_id}/workspace/drafts/composer"

    def offline(route):
        if route.request.method == "PUT":
            route.fulfill(status=503, json={"error": {"message": "Draft service unavailable"}})
        else:
            route.continue_()

    page.route(path, offline)
    page.locator("#pc-command").fill("Unsynced but recoverable text.")
    expect(page.locator("#pc-draft-status")).to_contain_text("Could not sync")
    expect(page.locator("#pc-draft-status")).to_contain_text("saved on this device")
    page.reload()
    expect(page.locator("#pc-command")).to_have_value("Unsynced but recoverable text.")
    page.unroute(path, offline)
    page.get_by_role("button", name="Retry draft sync", exact=True).click()
    expect(page.locator("#pc-draft-status")).to_have_text("Draft saved.")
    page.evaluate("session.scopes=session.scopes.filter(value=>value!=='jobs:write')")
    page.locator("#pc-command").fill("Read-only account keeps a local draft.")
    expect(page.locator("#pc-draft-status")).to_contain_text("requires project write access")
    remote = page.evaluate("id=>api('/v1/projects/'+id+'/workspace/drafts/composer')", project_id)
    assert remote["text"] == "Unsynced but recoverable text."


def test_queue_reply_and_finite_schedule(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page)
    page.locator("#pc-command").fill("Prepare the initial report.")
    page.get_by_role("button", name="Ask the lead", exact=True).click()
    expect(page.get_by_role("button", name="Queue request", exact=True)).to_be_enabled()
    page.locator("#pc-command").fill("Then compare the suppliers.")
    page.get_by_role("button", name="Queue request", exact=True).click()
    expect(page.locator(".pc-queued")).to_contain_text("Then compare the suppliers.")
    page.evaluate(
        """id=>api('/v1/projects/'+id+'/waits',{
        instruction:'Prepare a cost comparison.',question:'Which launch quantity should we use?',
        idempotency_key:'browser-pending-question'})""",
        project_id,
    )
    refresh(page)
    expect(page.locator(".pc-wait")).to_contain_text("Which launch quantity")
    page.locator(".pc-wait textarea").fill("Use forty units, with estimates clearly labeled.")
    page.get_by_role("button", name="Send reply", exact=True).click()
    expect(page.locator(".pc-wait")).to_have_count(0)
    page.locator(".pc-schedule-editor > summary").click()
    editor = page.locator(".pc-schedule-form")
    editor.get_by_label("Schedule name", exact=True).fill("Weekly review")
    editor.get_by_label("Request", exact=True).fill("Review findings and report changes.")
    editor.get_by_label("Repeat", exact=True).select_option("weekly")
    editor.get_by_label("Mon", exact=True).check()
    editor.get_by_label("Time zone", exact=True).fill("America/New_York")
    editor.get_by_label("Model budget per run ($)", exact=True).fill("2.50")
    editor.get_by_label("Maximum runs", exact=True).fill("4")
    editor.get_by_role("button", name="Create schedule", exact=True).click()
    expect(page.locator(".pc-schedule-status")).to_have_text("Schedule saved.")
    expect(page.locator(".pc-schedules")).to_contain_text("0 of 4 runs")
    page.get_by_role("button", name="Pause schedule", exact=True).click()
    expect(page.locator(".pc-schedules")).to_contain_text("Paused")
    page.reload()
    expect(page.locator(".pc-queued")).to_contain_text("Then compare the suppliers.")
    expect(page.locator(".pc-schedules")).to_contain_text("Weekly review")
    page.locator("#pc-continuity").scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "upcoming-work-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#pc-continuity").scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=str(tmp_path / "upcoming-work-mobile.png"), animations="disabled")


def test_individual_agent_cards_and_named_file_previews(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page)
    page.evaluate(
        """async id=>{const path='/v1/projects/'+id;const view=await api(path+'/command');
        await api(path+'/team',{expected_version:view.state.version,team:{
        name:'Research team',agent_ids:['writer','member-review'],lead_agent_id:'writer',
        max_parallel:1,members:{'member-review':{name:'Evidence reviewer',
        description:'Check assumptions and supporting evidence.',
        skill_ids:['analysis']}}}},'PATCH');
        await api(path+'/todos',{todo:{id:'review-costs',title:'Check the cost assumptions',
        objective:'Review supplied numbers.',agent_id:'member-review',status:'ready'},
        idempotency_key:'browser-agent-current-work'});} """,
        project_id,
    )
    refresh(page)
    page.locator("#pc-tab-team").click()
    expect(page.locator(".pc-agent-card")).to_have_count(2)
    reviewer = page.locator('.pc-agent-card[data-member-id="member-review"]')
    expect(reviewer).to_contain_text("Evidence reviewer")
    expect(reviewer).to_contain_text("Check the cost assumptions")
    reviewer.locator("summary").click()
    expect(reviewer).to_contain_text("Analysis and drafting")
    expect(reviewer.get_by_role("button", name="Edit skills: Evidence reviewer")).to_be_visible()
    page.screenshot(path=str(tmp_path / "agent-cards-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    reviewer.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=str(tmp_path / "agent-cards-mobile.png"), animations="disabled")
    base = f"/v1/projects/{project_id}/outputs/saved-run"
    outputs = [
        {
            "id": f"candidate-{i}",
            "run_id": "saved-run",
            "task_id": "review-costs",
            "title": title,
            "name": "cost-comparison.md" if i == 0 else "supplier-review.md",
            "media_type": "text/plain",
            "size": 120,
            "created_at": "2026-10-01T12:00:00Z",
            "project_copy": None,
            "source": "artifact",
            "kind": "deliverable",
            "status": state,
            "preview_url": f"{base}/candidate-{i}/preview",
            "download_url": f"{base}/candidate-{i}/download",
        }
        for i, (title, state) in enumerate(
            [("Cost comparison draft", "draft"), ("Partial supplier review", "partial")]
        )
    ]
    page.route(
        f"**/v1/projects/{project_id}/outputs?*",
        lambda route: route.fulfill(
            json={
                "project_id": project_id,
                "items": outputs,
                "next_cursor": None,
                "can_promote": False,
            }
        ),
    )
    page.route(
        "**/outputs/saved-run/candidate-0/preview",
        lambda route: route.fulfill(
            json={
                "text": "Retained draft with cost assumptions.",
                "format": "text",
                "truncated": False,
            }
        ),
    )
    page.locator("#pc-project-section").select_option("files")
    expect(page.locator("#po-list")).to_contain_text("Draft file - awaiting review")
    expect(page.locator("#po-list")).to_contain_text("Partial file - needs attention")
    draft = page.locator('[data-output="candidate-0"]')
    draft.get_by_role("button", name="Preview cost-comparison.md").click()
    expect(draft).to_contain_text("Retained draft with cost assumptions.")
    page.evaluate(
        """id=>api('/v1/projects/'+id+'/workspace/records/'+crypto.randomUUID(),{
        expected_version:0,idempotency_key:crypto.randomUUID(),kind:'supplier',
        title:'Small-batch supplier shortlist',
        summary:'Compare minimum orders and sampling costs.'},
        'PUT')""",
        project_id,
    )
    page.locator("#pc-project-section").select_option("knowledge")
    expect(page.locator("#project-workspace")).to_contain_text("Small-batch supplier shortlist")
    page.set_viewport_size({"width": 1440, "height": 1080})
    page.locator("#project-workspace").scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "workspace-records-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#project-workspace").scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=str(tmp_path / "workspace-records-mobile.png"), animations="disabled")
