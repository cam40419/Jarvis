"""Resumable native intake in a real browser with synthetic, bounded model calls."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from simon.domain.native_projects import VersionedNativeCommand
from tests.integration.test_native_models_browser import (
    allow_paid,
    configure_catalog,
    enroll,
    policy,
    qualify,
    view,
)
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import (
    open_project,
    seed_project,
    seed_task,
    share,
)

pytestmark = pytest.mark.browser


def intake(ui, project):
    response = ui.client.get(ui.path(f"/v2/projects/{project.id}/intake"))
    assert response.status_code == 200, response.text
    return response.json()


def open_intake(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-intake").click()
    expect(page.locator("#ni-dialog")).to_be_visible()
    expect(page.locator("#ni-refresh")).to_be_enabled()


def configure_planner(ui, project, proposal, *, approved=True, cloud=False):
    requests = []

    def respond(request):
        if "simon_model_probe" in request.content.decode():
            return {"simon_model_probe": True, "version": 1}
        requests.append(json.loads(request.content))
        result = (
            proposal
            if len(requests) % 2
            else {
                "approved": approved,
                "issues": [] if approved else ["The proposed work needs stronger evidence."],
            }
        )
        return result

    configure_catalog(ui, cloud=cloud, paid=True, respond=respond)
    allow_paid(ui, project, cloud=cloud)
    models = view(ui, project)["models"]
    if not models:
        qualify(ui, project, enroll(ui, project, cloud=cloud))
    return requests


def proposal_document():
    return {
        "summary": "Document the launch direction and bring the choices to the owner.",
        "next_milestone": "Review a sourced launch brief.",
        "findings": [
            {"kind": "assumption", "statement": "The launch scope is provisional.", "evidence": []}
        ],
        "questions": [],
        "roles": [
            {
                "role_key": "researcher",
                "action": "create",
                "name": "Project researcher",
                "instructions": "Develop an evidence-backed launch brief for owner review.",
                "success_criteria": (
                    "The brief cites its sources and leaves unclear decisions open."
                ),
                "rationale": "The first milestone needs one accountable research owner.",
                "agent_id": None,
                "need": "specialist",
                "reuse_assessment": "There are no existing roles to reuse for this work.",
            }
        ],
        "tasks": [
            {
                "key": "launch-brief",
                "title": "Prepare the launch brief",
                "description": "Synthesize the project sources and identify owner decisions.",
                "acceptance": "Cite the source for each fact and request owner review.",
                "assignment": "agent",
                "role_key": "researcher",
                "review_role_key": None,
                "existing_task_id": None,
            }
        ],
    }


def set_planning_fields(page, *, auto=False):
    page.locator("#ni-auto").set_checked(auto)


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_intake_resumes_uploads_revisions_and_truthful_model_readiness(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_intake(ui, project)
    expect(ui.page.locator("#ni-model-status")).to_contain_text("No qualified model")
    expect(ui.page.locator("#ni-analyze")).to_be_disabled()
    ui.page.locator("#ni-background").fill("A clothing brand at the idea stage.")
    ui.page.locator("#ni-outcomes").fill("Agree on the brand direction before selecting pieces.")
    ui.page.locator("#ni-constraints").fill(
        "Keep human authorship undecided until direction review."
    )
    ui.page.locator("#ni-save").click()
    expect(ui.page.locator("#ni-feedback")).to_have_text("Intake saved.")
    ui.page.locator("#ni-files").set_input_files(
        [
            {"name": "direction.md", "mimeType": "text/markdown", "buffer": b"Direction is open."},
            {"name": "reference.pdf", "mimeType": "application/pdf", "buffer": b"%PDF unread"},
        ]
    )
    expect(ui.page.locator("#ni-sources .ni-card")).to_have_count(2)
    expect(ui.page.locator("#ni-upload-progress")).to_have_text(
        "All selected files have been processed."
    )
    expect(ui.page.locator("#ni-sources")).to_contain_text("Unread original")
    sources = intake(ui, project)["sources"]
    text_source = next(item for item in sources if item["filename"] == "direction.md")
    text_row = ui.page.locator(f'[data-source-id="{text_source["id"]}"]')
    text_row.get_by_role("button", name="Preview text").click()
    expect(ui.page.locator("#ni-preview-text")).to_have_text("Direction is open.")
    ui.page.locator("#ni-preview-close").click()
    with ui.page.expect_download() as download:
        text_row.get_by_role("button", name="Download original").click()
    assert Path(download.value.path()).read_bytes() == b"Direction is open."
    ui.page.locator("#ni-files").set_input_files(
        {"name": "direction.md", "mimeType": "text/markdown", "buffer": b"Review two directions."}
    )
    expect(ui.page.locator("#ni-sources .ni-card")).to_have_count(3)
    expect(ui.page.locator("#ni-sources")).to_contain_text("Historical revision")
    current = next(item for item in intake(ui, project)["sources"] if item["revision"] == 2)
    ui.page.locator(f'[data-source-id="{current["id"]}"]').get_by_role(
        "button", name="Revoke from context"
    ).click()
    expect(ui.page.locator(f'[data-source-id="{current["id"]}"]')).to_contain_text("Revoked")
    expect(ui.page.locator('#ni-sources input[type="checkbox"]')).to_have_count(0)
    ui.page.locator("#ni-close").click()
    ui.page.reload()
    ui.page.locator("#np-intake").click()
    expect(ui.page.locator("#ni-background")).to_have_value("A clothing brand at the idea stage.")
    assert ui.page.evaluate("localStorage.length + sessionStorage.length") == 0


def test_intake_conflict_keeps_draft_and_lost_save_reuses_exact_key(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_intake(ui, project)
    ui.page.locator("#ni-background").fill("My draft direction.")
    external = ui.client.put(
        ui.path(f"/v2/projects/{project.id}/intake"),
        json={
            "background": "Another owner updated context.",
            "expected_version": 0,
            "idempotency_key": str(uuid4()),
        },
    )
    assert external.status_code == 200, external.text
    ui.page.locator("#ni-save").click()
    expect(ui.page.locator("#ni-conflict")).to_be_visible()
    expect(ui.page.locator("#ni-latest")).to_contain_text("Another owner updated context.")
    expect(ui.page.locator("#ni-background")).to_have_value("My draft direction.")
    expect(ui.page.locator("#ni-save")).to_be_disabled()
    ui.page.locator("#ni-rebase").click()
    attempts = []

    def lose_response(route):
        if route.request.method != "PUT":
            route.continue_()
            return
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            response = route.fetch()
            assert response.ok
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/v2/projects/*/intake", lose_response)
    ui.page.locator("#ni-save").click()
    expect(ui.page.locator("#ni-uncertain")).to_be_visible()
    expect(ui.page.locator("#ni-background")).to_be_disabled()
    expect(ui.page.locator("#ni-close")).to_be_disabled()
    ui.page.locator("#ni-retry").click()
    expect(ui.page.locator("#ni-uncertain")).not_to_be_visible()
    assert attempts[0] == attempts[1]
    assert intake(ui, project)["intake"]["version"] == 2
    assert intake(ui, project)["intake"]["background"] == "My draft direction."


@pytest.mark.parametrize("auto", [False, True])
def test_reviewed_plan_applies_roles_and_board_tasks_with_cost_visibility(native_ui, auto):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    proposal = proposal_document()
    proposal["summary"] = '<img src=x onerror="window.intakeInjected=true">'
    calls = configure_planner(ui, project, proposal)
    open_intake(ui, project)
    set_planning_fields(ui.page, auto=auto)
    ui.page.locator("#ni-analyze").click()
    expect(ui.page.locator("#ni-run")).to_contain_text("Independent AI review")
    expect(ui.page.locator("#ni-run")).to_contain_text("Reuse assessment")
    expect(ui.page.locator("#ni-run")).to_contain_text("Human review")
    expect(ui.page.locator("#ni-run")).to_contain_text(proposal["summary"])
    assert ui.page.locator("#ni-run img").count() == 0
    assert ui.page.evaluate("window.intakeInjected === undefined")
    if not auto:
        expect(ui.page.locator("#ni-apply")).to_be_enabled()
        assert not ui.service.tasks(ui.actor, project.id)
        ui.page.locator("#ni-apply").click()
    expect(ui.page.locator("#ni-run")).to_contain_text(
        "Applied. The agent roles and board tasks are ready."
    )
    assert len(calls) == 2
    assert intake(ui, project)["spent_microusd"] > 0
    assert intake(ui, project)["reserved_microusd"] == 0
    ui.page.locator("#ni-close").click()
    expect(ui.page.locator("#np-board")).to_contain_text("Prepare the launch brief")
    ui.page.locator("#np-agents").click()
    expect(ui.page.locator("#nt-agents")).to_contain_text("Project researcher")


def test_hosted_consent_questions_answers_and_review_rejection_are_visible(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    proposal = proposal_document()
    proposal["questions"] = [
        {
            "key": "direction",
            "question": "Which direction should we research?",
            "why": "The brand direction is still open.",
            "blocking": True,
        }
    ]
    calls = configure_planner(ui, project, proposal, cloud=True)
    policy(ui, project, allow_cloud=False)
    open_intake(ui, project)
    set_planning_fields(ui.page, auto=True)
    expect(ui.page.locator("#ni-analyze")).to_be_disabled()
    expect(ui.page.locator("#ni-model-status")).to_contain_text("model")
    ui.page.locator("#ni-models").click()
    ui.page.locator("#nm-project-policy").click()
    ui.page.locator("#nm-cloud").check()
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-feedback")).to_have_text("Settings saved.")
    ui.page.locator("#nm-close").click()
    ui.page.locator("#np-intake").click()
    ui.page.locator("#ni-analyze").click()
    expect(ui.page.locator("#ni-run")).to_contain_text("Your answers are needed")
    expect(ui.page.locator("#ni-apply")).not_to_be_visible()
    expect(ui.page.locator("#ni-run")).to_contain_text("1 human decision task is tracked")
    ui.page.locator("#ni-close").click()
    expect(ui.page.locator("#np-board")).to_contain_text(
        "Decision: Which direction should we research?"
    )
    ui.page.locator("#np-intake").click()
    ui.page.locator("#ni-answer-direction").fill(
        "Compare two directions without selecting a winner."
    )
    ui.page.locator("#ni-save").click()
    expect(ui.page.locator("#ni-feedback")).to_have_text("Intake saved.")
    assert intake(ui, project)["intake"]["answers"]["direction"].startswith("Compare two")
    assert len(calls) == 2
    decisions = ui.service.tasks(ui.actor, project.id)
    assert len(decisions) == 1 and decisions[0].assignment.kind == "human"
    configure_planner(ui, project, proposal_document(), approved=False, cloud=True)
    ui.page.locator("#ni-analyze").click()
    expect(ui.page.locator("#ni-run")).to_contain_text("The proposed work needs stronger evidence.")
    expect(ui.page.locator("#ni-apply")).not_to_be_visible()
    expect(ui.page.locator("#ni-history option")).to_have_count(2)


def test_lost_plan_response_recovers_saved_attempt_without_duplicate_calls(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    calls = configure_planner(ui, project, proposal_document())
    open_intake(ui, project)
    set_planning_fields(ui.page)

    def lose_response(route):
        response = route.fetch()
        assert response.ok
        route.abort("failed")

    ui.page.route("**/intake/analyze", lose_response)
    ui.page.locator("#ni-analyze").click()
    expect(ui.page.locator("#ni-feedback")).to_contain_text("Planning attempt recovered")
    expect(ui.page.locator("#ni-apply")).to_be_enabled()
    expect(ui.page.locator("#ni-uncertain")).not_to_be_visible()
    assert len(calls) == 2
    assert len(intake(ui, project)["runs"]) == 1


def test_read_only_members_access_loss_and_project_navigation_clear_private_context(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    second = seed_project(ui, "Second project")
    guest = ui.user(role="guest", name="Reviewer")
    share(ui, project, guest)
    open_intake(ui, project, page=guest.page)
    expect(guest.page.locator("#ni-background")).to_be_disabled()
    expect(guest.page.locator("#ni-analyze")).not_to_be_visible()
    expect(guest.page.locator("#ni-upload-controls")).not_to_be_visible()
    latest = ui.service.get_project(ui.actor, project.id)
    ui.service.remove_member(
        ui.actor,
        project.id,
        guest.actor.actor_id,
        VersionedNativeCommand(expected_version=latest.version, idempotency_key=str(uuid4())),
    )
    guest.page.locator("#ni-refresh").click()
    expect(guest.page.locator("#ni-dialog")).not_to_be_visible()
    expect(guest.page.locator("#ni-sources .ni-card")).to_have_count(0)
    open_intake(ui, project)
    ui.page.locator("#ni-background").fill("Private unsaved direction")
    ui.page.locator("#ni-close").click()
    ui.page.locator("#np-back").click()
    ui.page.get_by_role("link", name=second.name, exact=True).click()
    ui.page.locator("#np-intake").click()
    expect(ui.page.locator("#ni-background")).to_have_value("")


def test_project_creation_entry_folder_labels_and_mobile_layout(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    ui.page.goto(ui.url)
    ui.page.locator("#np-new").click()
    ui.page.locator("#np-project-name").fill("Intake from creation")
    ui.page.locator("#np-project-objective").fill("Begin with source material and owner decisions.")
    ui.page.locator("#np-project-intake").check()
    ui.page.locator("#np-project-save").click()
    expect(ui.page.locator("#ni-dialog")).to_be_visible()
    expect(ui.page.locator("#ni-refresh")).to_be_enabled()
    folder = tmp_path / "brand"
    (folder / "direction").mkdir(parents=True)
    (folder / "direction" / "notes.md").write_text(
        "Brand direction remains open.", encoding="utf-8"
    )
    ui.page.locator("#ni-folder").set_input_files(folder)
    expect(ui.page.locator("#ni-sources")).to_contain_text("brand/direction/notes.md")
    ui.page.set_viewport_size({"width": 390, "height": 844})
    assert ui.page.locator("#ni-dialog").evaluate("node => node.scrollWidth <= node.clientWidth")
    screenshots = Path(".local/native-intake-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.screenshot(path=str(screenshots / "intake-mobile.png"), animations="disabled")
    ui.page.set_viewport_size({"width": 1440, "height": 1080})
    ui.page.screenshot(path=str(screenshots / "intake-desktop.png"), animations="disabled")


def test_lost_source_upload_replays_exact_bytes_then_continues_queue(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_intake(ui, project)
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            response = route.fetch()
            assert response.ok
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/intake/sources", lose_response)
    ui.page.locator("#ni-files").set_input_files(
        [
            {"name": "first.md", "mimeType": "text/markdown", "buffer": b"First source"},
            {"name": "second.md", "mimeType": "text/markdown", "buffer": b"Second source"},
        ]
    )
    expect(ui.page.locator("#ni-uncertain")).to_be_visible()
    expect(ui.page.locator("#ni-files")).to_be_disabled()
    assert len(intake(ui, project)["sources"]) == 1
    ui.page.locator("#ni-retry").click()
    expect(ui.page.locator("#ni-upload-progress")).to_have_text(
        "All selected files have been processed."
    )
    assert attempts[0] == attempts[1]
    assert len(attempts) == 3
    assert attempts[2]["expected_version"] == 2
    assert len(intake(ui, project)["sources"]) == 2


def test_undelivered_analysis_retries_same_key_and_cancel_is_durable(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    calls = configure_planner(ui, project, proposal_document())
    open_intake(ui, project)
    set_planning_fields(ui.page)
    attempts = []

    def lose_request(route):
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/intake/analyze", lose_request)
    ui.page.locator("#ni-analyze").click()
    expect(ui.page.locator("#ni-uncertain")).to_be_visible()
    assert not calls
    ui.page.locator("#ni-refresh").click()
    expect(ui.page.locator("#ni-uncertain")).to_be_visible()
    ui.page.locator("#ni-retry").click()
    expect(ui.page.locator("#ni-apply")).to_be_enabled()
    assert attempts[0] == attempts[1]
    assert len(calls) == 2
    ui.page.locator("#ni-cancel-run").click()
    expect(ui.page.locator("#ni-run")).to_contain_text("This attempt was cancelled.")
    expect(ui.page.locator("#ni-apply")).not_to_be_visible()
    assert intake(ui, project)["runs"][0]["status"] == "cancelled"
    assert not ui.service.tasks(ui.actor, project.id)


def test_attempt_outside_recent_history_is_loaded_by_its_project_scoped_id(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    calls = configure_planner(ui, project, proposal_document())
    open_intake(ui, project)
    set_planning_fields(ui.page)

    def omit_from_recent_page(route):
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        assert response.ok
        result = response.json()
        result["runs"] = []
        route.fulfill(response=response, json=result)

    ui.page.route("**/v2/projects/*/intake", omit_from_recent_page)
    ui.page.locator("#ni-analyze").click()
    expect(ui.page.locator("#ni-apply")).to_be_enabled()
    run = intake(ui, project)["runs"][0]
    expect(ui.page.locator("#ni-history")).to_have_value(run["id"])
    assert any(url.endswith("/intake/runs/" + run["id"]) for _, url in ui.requests)
    assert len(calls) == 2


def test_board_change_marks_manual_proposal_stale_without_partial_staffing(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    calls = configure_planner(ui, project, proposal_document())
    open_intake(ui, project)
    set_planning_fields(ui.page)
    ui.page.locator("#ni-analyze").click()
    expect(ui.page.locator("#ni-apply")).to_be_enabled()
    manual = seed_task(ui, project, "A new owner priority")
    ui.page.locator("#ni-apply").click()
    expect(ui.page.locator("#ni-run")).to_contain_text("Project context, team, or board changed.")
    expect(ui.page.locator("#ni-apply")).not_to_be_visible()
    expect(ui.page.locator("#ni-analyze")).to_be_enabled()
    assert intake(ui, project)["runs"][0]["status"] == "stale"
    assert [task.id for task in ui.service.tasks(ui.actor, project.id)] == [manual.id]
    agents = ui.client.get(ui.path(f"/v2/projects/{project.id}/team")).json()["agents"]
    assert not agents
    assert len(calls) == 2
