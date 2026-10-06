"""Conversational team/member recommendations through real services and synthetic models."""

import json

import pytest

from simon.domain.model_routing import TextGenerationResult
from tests.integration.test_agent_library_browser import agent_library_ui as agent_library_ui
from tests.integration.test_project_command_browser import create_project
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def role(name="Research and documents", *, lead=True, skills=None):
    return {
        "name": name,
        "description": (
            "Research the project evidence, write a useful report, and check the saved result."
        ),
        "skill_ids": skills or ["tool.native.local_file_read", "tool.native.local_file_write"],
        "is_lead": lead,
        "rationale": "One person owns the evidence and the finished document.",
    }


def reply(roles=None, *, message="Here is a team to review.", warnings=None):
    return {
        "message": message,
        "proposal": {"team_name": "Evidence team", "roles": roles or [role()]},
        "warnings": warnings or [],
    }


@pytest.fixture
def setup_ui(agent_library_ui):
    page, container = agent_library_ui
    responses, requests, calls = [], [], []

    def generate(decision, request):
        calls.append(request)
        return TextGenerationResult(
            endpoint_id=decision.endpoint_id,
            model=decision.model,
            text=json.dumps(responses.pop(0)),
            input_tokens=10,
            output_tokens=10,
        )

    container.agent_setup_assistant.generate = generate
    page.on(
        "request",
        lambda request: (
            requests.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/setup-assistant")
            else None
        ),
    )
    return page, container, responses, requests, calls


def send(page, text):
    from playwright.sync_api import expect

    page.locator("#sa-message").fill(text)
    expect(page.locator("#sa-send")).to_be_enabled()
    page.locator("#sa-send").click()
    expect(page.locator("#sa-send")).to_be_enabled()


def test_team_setup_conversation_reviews_then_fills_draft_without_saving(setup_ui, tmp_path):
    from playwright.sync_api import expect

    page, _, responses, requests, calls = setup_ui
    project_id = create_project(page, "Conversation-led team")
    before = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"]
    page.locator("#pc-settings").click()
    settings = page.locator("#pc-settings-dialog")
    settings.get_by_label("Team name", exact=True).fill("My unsaved team")
    settings.get_by_label("Parallel tasks", exact=True).fill("3")
    settings.get_by_label("Model budget per cycle (USD, optional)", exact=True).fill("3.50")
    settings.get_by_role("button", name="Describe a team", exact=True).click()
    expect(page.locator("#setup-assistant")).to_be_visible()
    expect(page.locator("#sa-message")).to_contain_text("")
    assert not calls
    responses.append(
        {"message": "Will the team use local files or Drive?", "proposal": None, "warnings": []}
    )
    send(page, "I need a small team to research our brand and prepare documents.")
    expect(page.locator("#sa-transcript")).to_contain_text("local files or Drive")
    expect(page.locator("#sa-apply")).not_to_be_visible()
    responses.append(
        reply(
            [
                role(),
                role("Independent reviewer", lead=False, skills=["tool.native.drive_read_file"]),
            ],
            warnings=["Check source evidence before approving the report."],
        )
    )
    send(page, "Use local files for research and writing, with a separate Drive evidence review.")
    expect(page.locator(".sa-role")).to_have_count(2)
    expect(page.locator(".sa-skills")).to_contain_text(
        ["Read a local file", "Read a Google Drive file"]
    )
    expect(page.locator(".sa-skills")).not_to_contain_text(["native.drive_read_file"])
    expect(page.locator(".sa-warnings")).to_contain_text("needs setup or permission")
    expect(page.locator("#sa-apply-note")).to_contain_text("Replaces the team in this editor")
    expect(settings.get_by_label("Team name", exact=True)).to_have_value("My unsaved team")
    assert requests[1]["messages"][1]["role"] == "assistant"
    assert requests[1]["project_id"] == project_id
    assert requests[1]["privacy"] == "allow_cloud"
    expect(page.locator("#sa-apply")).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "setup-team-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sa-message")).to_be_in_viewport()
    expect(page.locator("#sa-apply")).to_be_in_viewport()
    assert page.locator("#setup-assistant").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=str(tmp_path / "setup-team-mobile.png"), animations="disabled")
    page.locator("#sa-reset").click()
    expect(page.locator(".sa-warnings")).to_contain_text("Check source evidence")
    expect(page.locator(".sa-warnings")).to_contain_text("needs setup or permission")
    responses.append(reply(message="A single capable lead can handle the complete outcome."))
    send(page, "Combine the work into one local research and documents lead.")
    assert len(requests[2]["messages"]) == 1
    assert len(requests[2]["current_draft"]["roles"]) == 2
    expect(page.locator(".sa-role")).to_have_count(1)
    page.locator("#sa-apply").click()
    expect(page.locator("#setup-assistant")).not_to_be_visible()
    expect(settings.get_by_label("Team name", exact=True)).to_have_value("Evidence team")
    expect(settings.get_by_label("Parallel tasks", exact=True)).to_have_value("3")
    expect(
        settings.get_by_label("Model budget per cycle (USD, optional)", exact=True)
    ).to_have_value("3.50")
    expect(settings.get_by_label("Team name", exact=True)).to_be_focused()
    assert (
        page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"] == before
    )
    with page.expect_response("**/team") as saved:
        settings.get_by_role("button", name="Save settings", exact=True).click()
    assert saved.value.status == 200, saved.value.json()
    expect(settings).not_to_be_visible()
    after = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"]
    assert after["team"]["name"] == "Evidence team" and after["team"]["max_parallel"] == 3
    assert after["autonomy"]["model_budget_usd"] == 3.5
    member = after["team"]["members"][after["team"]["lead_agent_id"]]
    assert member["skill_ids"] == ["tool.native.local_file_read", "tool.native.local_file_write"]


def test_member_setup_cancel_apply_and_stale_form_keep_project_draft_safe(setup_ui):
    from playwright.sync_api import expect

    page, _, responses, requests, _ = setup_ui
    page.locator("#pc-new-project").click()
    project = page.locator("#pc-create-dialog")
    project.get_by_label("Project name", exact=True).fill("Keep these project details")
    project.get_by_role("button", name="Add agent", exact=True).click()
    member = page.locator("#pc-member-dialog")
    member.get_by_label("Role title", exact=True).fill("My agent draft")
    member.get_by_label("Role description", exact=True).fill(
        "Research and prepare our brand documents."
    )
    member.get_by_role("button", name="Describe this agent", exact=True).click()
    expect(page.locator("#sa-message")).to_have_value(
        "My agent draft\nResearch and prepare our brand documents."
    )
    page.keyboard.press("Escape")
    expect(page.locator("#setup-assistant")).not_to_be_visible()
    expect(member.get_by_role("button", name="Describe this agent", exact=True)).to_be_focused()
    expect(member.get_by_label("Role title", exact=True)).to_have_value("My agent draft")
    member.get_by_role("button", name="Describe this agent", exact=True).click()
    responses.append(reply())
    send(page, "Recommend local research and document-writing skills.")
    assert requests[-1]["mode"] == "member" and requests[-1]["project_id"] is None
    assert requests[-1]["current_draft"]["roles"] == []
    page.locator("#sa-apply").click()
    expect(member.get_by_label("Role title", exact=True)).to_have_value("Research and documents")
    expect(member.get_by_role("checkbox", name="Read a local file", exact=True)).to_be_checked()
    expect(member.get_by_role("checkbox", name="Write a local file", exact=True)).to_be_checked()
    expect(project).not_to_contain_text("Research and documents")
    member.get_by_role("button", name="Describe this agent", exact=True).click()
    responses.append(reply([role("Changed recommendation")]))
    send(page, "Revise this role.")
    member.get_by_label("Role title", exact=True).evaluate(
        "input => input.value = 'Newer unsaved title'"
    )
    page.locator("#sa-apply").click()
    expect(page.locator("#sa-error")).to_contain_text("editor or available skills changed")
    expect(member.get_by_label("Role title", exact=True)).to_have_value("Newer unsaved title")
    page.keyboard.press("Escape")
    member.get_by_role("button", name="Save team member", exact=True).click()
    expect(project).to_contain_text("Newer unsaved title")
    expect(project.get_by_label("Project name", exact=True)).to_have_value(
        "Keep these project details"
    )


def test_setup_pending_error_privacy_and_cancel_do_not_overwrite_drafts(setup_ui):
    from playwright.sync_api import expect

    page, _, responses, requests, calls = setup_ui
    page.locator("#pc-new-project").click()
    project = page.locator("#pc-create-dialog")
    project.get_by_label("Team name", exact=True).fill("Untouched team draft")
    project.get_by_role("button", name="Describe a team", exact=True).click()
    pending = []
    page.route("**/v1/agent-platform/setup-assistant", lambda route: pending.append(route))
    page.locator("#sa-privacy").select_option("local_only")
    page.locator("#sa-message").fill("Keep all setup processing on local models.")
    page.locator("#sa-send").click()
    expect(page.locator("#sa-send")).to_be_disabled()
    page.locator("#sa-message").fill("A newer message typed while waiting.")
    page.locator("#sa-form").evaluate("form => form.requestSubmit()")
    assert len(pending) == 1 and len(requests) == 1
    assert requests[0]["privacy"] == "local_only"
    pending.pop().fulfill(
        status=503,
        json={
            "error": {
                "code": "model_error",
                "message": "No local model is configured. Use the manual editor.",
            }
        },
    )
    expect(page.locator("#sa-error")).to_contain_text("No local model is configured")
    expect(page.locator("#sa-message")).to_have_value("A newer message typed while waiting.")
    expect(project.get_by_label("Team name", exact=True)).to_have_value("Untouched team draft")
    expect(page.locator("#sa-privacy")).to_have_value("local_only")
    responses.append(reply(message='<img src=x onerror="window.badSetup=true">Plain text reply'))
    page.locator("#sa-send").click()
    expect(page.locator("#sa-send")).to_be_disabled()
    page.keyboard.press("Escape")
    expect(page.locator("#setup-assistant")).not_to_be_visible()
    project.get_by_role("button", name="Describe a team", exact=True).click()
    expect(page.locator("#sa-privacy")).to_have_value("local_only")
    with page.expect_response("**/v1/agent-platform/setup-assistant"):
        pending.pop().continue_()
    expect(page.locator("#sa-transcript")).not_to_contain_text("Plain text reply")
    assert page.evaluate("window.badSetup") is None
    assert not page.locator("#sa-transcript img").count()
    expect(project.get_by_label("Team name", exact=True)).to_have_value("Untouched team draft")
    assert len(calls) <= 1


def test_team_assistant_accepts_an_unfinished_name_without_changing_it_on_cancel(setup_ui):
    from playwright.sync_api import expect

    page, _, responses, requests, _ = setup_ui
    page.locator("#pc-new-project").click()
    project = page.locator("#pc-create-dialog")
    project.get_by_label("Team name", exact=True).fill("")
    project.get_by_role("button", name="Describe a team", exact=True).click()
    responses.append(reply(message='<img src=x onerror="window.badSetup=true">Review this draft.'))
    send(page, "Help me name and configure a research team.")
    expect(page.locator("#sa-apply")).to_be_enabled()
    expect(page.locator("#sa-transcript")).to_contain_text("<img src=x onerror=")
    assert not page.locator("#sa-transcript img").count()
    assert page.evaluate("window.badSetup") is None
    assert requests[-1]["current_draft"]["team_name"] == "Project team"
    page.keyboard.press("Escape")
    expect(project.get_by_label("Team name", exact=True)).to_have_value("")
