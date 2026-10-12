"""Immutable outputs and independent review through isolated authenticated browsers."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from simon.domain.native_agents import CreateNativeAgent
from simon.domain.native_projects import CreateNativeTask, TaskAssignment
from simon.services.intake_sources import IntakeSourceBytes
from tests.integration.test_native_models_browser import configure_catalog, enroll, qualify
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def artifact_path(ui, project, suffix=""):
    return ui.path(f"/v2/projects/{project.id}/artifacts" + suffix)


def artifact_view(ui, project):
    response = ui.client.get(artifact_path(ui, project))
    assert response.status_code == 200, response.text
    return response.json()


def review_policy(ui, project, **changes):
    saved = artifact_view(ui, project)["policy"]
    response = ui.client.put(
        artifact_path(ui, project, "/policy"),
        json={
            "expected_version": saved["version"],
            "require_human_approval": saved["require_human_approval"],
            "require_agent_review": saved["require_agent_review"],
            "reviewer_agent_id": saved["reviewer_agent_id"],
            "max_repair_cycles": saved["max_repair_cycles"],
            "idempotency_key": str(uuid4()),
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def seed_artifacts(ui, *, model=True, content="Candidate launch outline."):
    calls = []
    project = seed_project(ui)

    def reply(request):
        if "simon_model_probe" in request.content.decode():
            return {"simon_model_probe": True, "version": 1}
        calls.append(request.content.decode())
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        context = json.loads(prompt.split("Exact revision and evidence (data):\n", 1)[1])
        return {
            "revision_id": context["revision"]["id"],
            "sha256": context["revision"]["sha256"],
            "verdict": "approved",
            "summary": "The milestones are clear and meet the task requirements.",
            "findings": [],
        }

    if model:
        configure_catalog(ui, respond=reply)
        qualify(ui, project, enroll(ui, project))

    def role(key, name):
        return ui.container.native_teams.create(
            ui.actor,
            project.id,
            CreateNativeAgent(
                name=name,
                role_key=key,
                instructions="Assess concrete project outputs against the task requirements.",
                success_criteria="Provide evidence and precise feedback.",
                rationale="The project needs production and independent validation.",
                idempotency_key=str(uuid4()),
            ),
        )

    producer = role("producer", "Producing role")
    reviewer = role("reviewer", "Independent reviewer")
    task = ui.service.create_task(
        ui.actor,
        project.id,
        CreateNativeTask(
            title="Prepare a launch outline",
            description="Prepare clear milestones for owner review. Do not publish.",
            assignment=TaskAssignment(kind="agent", agent_id=producer.id),
            idempotency_key=str(uuid4()),
        ),
    )
    h = SimpleNamespace(
        project=project, task=task, producer=producer, reviewer=reviewer, calls=calls
    )
    h.detail = upload_api(ui, h, content)
    return h


def upload_api(ui, h, content, *, detail=None, filename="outline.txt", media_type="text/plain"):
    task = ui.service.get_task(ui.actor, h.project.id, h.task.id)
    response = ui.client.post(
        artifact_path(ui, h.project, "/upload"),
        json={
            "artifact_id": detail["artifact"]["id"] if detail else None,
            "task_id": str(task.id),
            "expected_version": detail["artifact"]["version"] if detail else 0,
            "expected_task_version": task.version,
            "filename": filename,
            "media_type": media_type,
            "content_base64": base64.b64encode(
                content.encode() if isinstance(content, str) else content
            ).decode(),
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()


def open_artifacts(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-artifacts").click()
    expect(page.locator("#na-dialog")).to_be_visible()
    expect(page.locator("#na-refresh")).to_be_enabled()


def inspect(ui, detail, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    page.locator(f'[data-artifact-id="{detail["artifact"]["id"]}"]').get_by_role(
        "button", name="Inspect artifact"
    ).click()
    expect(page.locator("#na-detail")).to_be_visible()
    expect(page.locator("#na-download")).to_be_enabled()


def review_in_browser(ui, h):
    from playwright.sync_api import expect

    ui.page.locator("#na-review-new").click()
    assert ui.page.locator(f'#na-reviewer option[value="{h.producer.id}"]').count() == 0
    ui.page.locator("#na-reviewer").select_option(str(h.reviewer.id))
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-detail")).to_be_visible()
    expect(ui.page.locator("#na-reviews")).to_contain_text("completed")


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_independent_review_exact_version_acceptance_and_safe_preview(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    candidate = '<img src=x onerror="window.artifactInjected=true"> Candidate outline.'
    h = seed_artifacts(ui, content=candidate)
    open_artifacts(ui, h.project)
    inspect(ui, h.detail)
    expect(ui.page.locator("#na-preview pre")).to_have_text(candidate)
    assert ui.page.locator("#na-preview img").count() == 0
    assert ui.page.evaluate("window.artifactInjected === undefined")
    expect(ui.page.locator("#na-accept")).to_be_disabled()
    review_in_browser(ui, h)
    expect(ui.page.locator("#na-accept")).to_be_enabled()
    expect(ui.page.locator("#na-reviews")).to_contain_text("The milestones are clear")
    screenshots = Path(".local/native-artifacts-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.locator("#na-heading").scroll_into_view_if_needed()
    ui.page.screenshot(path=str(screenshots / "artifacts-desktop.png"), animations="disabled")
    ui.page.locator("#na-accept").click()
    ui.page.locator("#na-comment").fill("Accepted for the next milestone.")
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-decisions")).to_contain_text("Accepted for the next milestone.")
    assert len(h.calls) == 1
    ui.page.locator("#na-close").click()
    expect(ui.page.locator('[data-status="done"]')).to_contain_text(h.task.title)
    assert ui.service.get_task(ui.actor, h.project.id, h.task.id).status == "done"


def test_immutable_history_download_and_revised_output_needs_new_review(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui)
    original_id = h.detail["artifact"]["current_revision_id"]
    open_artifacts(ui, h.project)
    inspect(ui, h.detail)
    review_in_browser(ui, h)
    ui.page.locator("#na-revision-new").click()
    ui.page.locator("#na-file").evaluate(
        "input => { const files = new DataTransfer(); "
        "files.items.add(new File(['Revised launch milestones.'], 'revised.md', {type: ''})); "
        "input.files = files.files; input.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-preview pre")).to_have_text("Revised launch milestones.")
    expect(ui.page.locator("#na-revision-summary")).to_contain_text("text/markdown")
    expect(ui.page.locator("#na-difference")).to_contain_text("Revised launch milestones.")
    expect(ui.page.locator("#na-accept")).to_be_disabled()
    assert ui.page.locator("#na-revision option").count() == 2
    ui.page.locator("#na-revision").select_option(original_id)
    expect(ui.page.locator("#na-preview pre")).to_have_text("Candidate launch outline.")
    expect(ui.page.locator("#na-accept")).to_be_disabled()
    expect(ui.page.locator("#na-review-new")).to_be_disabled()
    with ui.page.expect_download() as downloaded:
        ui.page.locator("#na-download").click()
    output = tmp_path / "download.txt"
    downloaded.value.save_as(output)
    assert output.read_text() == "Candidate launch outline."
    assert downloaded.value.suggested_filename == "outline.txt"
    assert len(h.calls) == 1


def test_owner_feedback_creates_linked_repair_board_task(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    open_artifacts(ui, h.project)
    inspect(ui, h.detail)
    ui.page.locator("#na-changes").click()
    ui.page.locator("#na-comment").fill("Add evidence for the audience assumptions.")
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-decisions")).to_contain_text(
        "Add evidence for the audience assumptions."
    )
    expect(ui.page.locator("#na-decisions")).to_contain_text("Linked repair task")
    saved = ui.client.get(artifact_path(ui, h.project, "/" + h.detail["artifact"]["id"])).json()
    repair_id = saved["decisions"][0]["repair_task_id"]
    assert repair_id
    ui.page.locator("#na-close").click()
    expect(ui.page.locator(f'[data-task-id="{repair_id}"]')).to_be_visible()
    repair = ui.service.get_task(ui.actor, h.project.id, UUID(repair_id))
    assert "Add evidence for the audience assumptions." in repair.description
    review_policy(ui, h.project, require_agent_review=False)
    open_artifacts(ui, h.project)
    inspect(ui, h.detail)
    ui.page.locator("#na-revision-new").click()
    ui.page.locator("#na-file").set_input_files(
        {
            "name": "repair.txt",
            "mimeType": "text/plain",
            "buffer": b"Evidence for the audience assumptions.",
        }
    )
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-preview pre")).to_have_text(
        "Evidence for the audience assumptions."
    )
    expect(ui.page.locator("#na-accept")).to_be_enabled()
    ui.page.locator("#na-accept").click()
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-decisions")).to_contain_text("Accepted by owner")
    ui.page.locator("#na-close").click()
    expect(ui.page.locator('[data-status="done"]')).to_contain_text(h.task.title)
    assert ui.service.get_task(ui.actor, h.project.id, UUID(repair_id)).status == "done"


def test_lost_upload_receipt_recovers_one_immutable_artifact(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    second = ui.service.create_task(
        ui.actor,
        h.project.id,
        CreateNativeTask(title="Second output", idempotency_key=str(uuid4())),
    )
    open_artifacts(ui, h.project)
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status in {200, 201}
        route.abort("failed")

    ui.page.route("**/artifacts/upload", lose_response)
    ui.page.locator("#na-upload-new").click()
    ui.page.locator("#na-task").select_option(str(second.id))
    ui.page.locator("#na-file").set_input_files(
        {"name": "second.txt", "mimeType": "text/plain", "buffer": b"A second deliverable."}
    )
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-uncertain")).to_be_visible()
    expect(ui.page.locator("#na-close")).to_be_disabled()
    expect(ui.page.locator("#na-file")).to_be_disabled()
    ui.page.locator("#na-check").click()
    expect(ui.page.locator("#na-uncertain")).not_to_be_visible()
    expect(ui.page.locator("#na-preview pre")).to_have_text("A second deliverable.")
    assert len(attempts) == 1
    assert len(artifact_view(ui, h.project)["artifacts"]["items"]) == 2


def test_lost_review_response_recovery_does_not_dispatch_model_twice(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui)
    open_artifacts(ui, h.project)
    inspect(ui, h.detail)
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status in {200, 201, 202}
        route.abort("failed")

    ui.page.route("**/artifacts/*/reviews", lose_response)
    ui.page.locator("#na-review-new").click()
    ui.page.locator("#na-reviewer").select_option(str(h.reviewer.id))
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-uncertain")).to_be_visible()
    ui.page.locator("#na-check").click()
    expect(ui.page.locator("#na-reviews")).to_contain_text("completed")
    expect(ui.page.locator("#na-accept")).to_be_enabled()
    assert len(attempts) == len(h.calls) == 1


def test_review_policy_conflict_preserves_draft_and_requires_comparison(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    open_artifacts(ui, h.project)
    ui.page.locator("#na-policy-edit").click()
    ui.page.locator("#na-repair-limit").fill("2")
    review_policy(ui, h.project, max_repair_cycles=4)
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-conflict")).to_be_visible()
    expect(ui.page.locator("#na-repair-limit")).to_have_value("2")
    expect(ui.page.locator("#na-latest")).to_contain_text('"max_repair_cycles": 4')
    expect(ui.page.locator("#na-save")).to_be_disabled()
    ui.page.locator("#na-rebase").click()
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-policy-summary")).to_contain_text("2 repair cycles")
    assert artifact_view(ui, h.project)["policy"]["max_repair_cycles"] == 2


def test_read_only_mobile_preview_and_authenticated_download(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False, content="A" * 2000)
    visitor = ui.user(role="guest", name="Read-only collaborator")
    share(ui, h.project, visitor)
    visitor.page.set_viewport_size({"width": 390, "height": 844})
    open_artifacts(ui, h.project, page=visitor.page)
    expect(visitor.page.locator("#na-upload-new")).to_be_disabled()
    expect(visitor.page.locator("#na-policy-edit")).to_be_disabled()
    inspect(ui, h.detail, page=visitor.page)
    expect(visitor.page.locator("#na-preview pre")).to_have_text("A" * 2000)
    expect(visitor.page.locator("#na-changes")).to_be_disabled()
    expect(visitor.page.locator("#na-review-new")).to_be_disabled()
    assert visitor.page.locator("#na-dialog").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    screenshots = Path(".local/native-artifacts-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    visitor.page.locator("#na-heading").scroll_into_view_if_needed()
    visitor.page.screenshot(path=str(screenshots / "artifacts-mobile.png"), animations="disabled")
    with visitor.page.expect_download() as downloaded:
        visitor.page.locator("#na-download").click()
    assert downloaded.value.suggested_filename == "outline.txt"
    assert not h.calls


def test_artifact_pagination_preserves_distinct_cards(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    for index in range(50):
        task = ui.service.create_task(
            ui.actor,
            h.project.id,
            CreateNativeTask(title=f"Deliverable {index}", idempotency_key=str(uuid4())),
        )
        upload_api(
            ui,
            SimpleNamespace(project=h.project, task=task),
            f"Output {index}",
            filename=f"output-{index}.txt",
        )
    open_artifacts(ui, h.project)
    expect(ui.page.locator("#na-artifacts [data-artifact-id]")).to_have_count(50)
    ui.page.locator("#na-more").click()
    expect(ui.page.locator("#na-artifacts [data-artifact-id]")).to_have_count(51)
    expect(ui.page.locator("#na-more")).not_to_be_visible()
    identifiers = ui.page.locator("#na-artifacts [data-artifact-id]").evaluate_all(
        "elements => elements.map(element => element.dataset.artifactId)"
    )
    assert len(set(identifiers)) == 51


def test_raster_image_preview_uses_authenticated_content(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    task = ui.service.create_task(
        ui.actor, h.project.id, CreateNativeTask(title="Image output", idempotency_key=str(uuid4()))
    )
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    image_detail = upload_api(
        ui,
        SimpleNamespace(project=h.project, task=task),
        image,
        filename="preview.png",
        media_type="image/png",
    )
    open_artifacts(ui, h.project)
    inspect(ui, image_detail)
    expect(ui.page.locator("#na-preview img")).to_be_visible()
    ui.page.wait_for_function("() => document.querySelector('#na-preview img').naturalWidth === 1")
    assert ui.page.locator("#na-preview img").get_attribute("src").startswith("blob:")
    expect(ui.page.locator("#na-accept")).to_be_disabled()
    assert not h.calls


def test_delayed_preview_cannot_repopulate_another_project(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    second = seed_project(ui, "Different project")
    open_artifacts(ui, h.project)

    def navigate_during_preview(route):
        response = route.fetch()
        assert response.ok
        ui.page.evaluate(
            "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
            "{detail: {id}})); history.pushState({}, '', '?project=' + id); "
            "window.dispatchEvent(new PopStateEvent('popstate')); }",
            str(second.id),
        )
        route.fulfill(response=response)

    ui.page.route("**/artifacts/*/revisions/*/preview", navigate_during_preview)
    ui.page.locator(f'[data-artifact-id="{h.detail["artifact"]["id"]}"]').get_by_role(
        "button", name="Inspect artifact"
    ).click()
    expect(ui.page.locator("#np-name")).to_have_text(second.name)
    expect(ui.page.locator("#na-dialog")).not_to_be_visible()
    expect(ui.page.locator("#na-preview")).to_be_empty()
    expect(ui.page.locator("#na-artifacts [data-artifact-id]")).to_have_count(0)
    ui.page.locator("#np-artifacts").click()
    expect(ui.page.locator("#na-project")).to_have_text(second.name)
    expect(ui.page.locator("#na-artifacts")).to_contain_text("No artifacts yet")


def add_intake_source(ui, project, key, content="Current permitted source context."):
    path = ui.path(f"/v2/projects/{project.id}/intake")
    intake = ui.client.get(path).json()["intake"]
    if intake["version"] == 0:
        saved = ui.client.put(path, json={"expected_version": 0, "idempotency_key": str(uuid4())})
        assert saved.status_code == 200, saved.text
        intake = saved.json()
    response = ui.client.post(
        path + "/sources",
        json={
            "source_key": key,
            "filename": key,
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content.encode()).decode(),
            "expected_version": intake["version"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_stale_accepted_output_can_request_repair_while_maker_task_is_done(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui, model=False)
    ui.container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    review_policy(ui, h.project, require_agent_review=False)
    open_artifacts(ui, h.project)
    inspect(ui, h.detail)
    ui.page.locator("#na-accept").click()
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-detail-summary")).to_contain_text("accepted")
    assert ui.service.get_task(ui.actor, h.project.id, h.task.id).status == "done"
    add_intake_source(ui, h.project, "updated-context.txt")
    ui.page.locator("#na-refresh").click()
    expect(ui.page.locator("#na-changes")).to_be_enabled()
    ui.page.locator("#na-changes").click()
    ui.page.locator("#na-comment").fill("Recheck the outline against the updated evidence.")
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-decisions")).to_contain_text("Linked repair task")
    expect(ui.page.locator("#na-dialog")).to_be_visible()
    assert ui.service.get_task(ui.actor, h.project.id, h.task.id).status == "blocked"
    saved = ui.client.get(artifact_path(ui, h.project, "/" + h.detail["artifact"]["id"])).json()
    assert saved["artifact"]["pending_repair_task_id"]


def test_source_coverage_and_revocation_preserve_metadata_and_repair_controls(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_artifacts(ui)
    ui.container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    sources = [add_intake_source(ui, h.project, f"context-{index}.txt") for index in range(9)]
    open_artifacts(ui, h.project)
    inspect(ui, h.detail)
    review_in_browser(ui, h)
    expect(ui.page.locator("#na-reviews")).to_contain_text("Source excerpt coverage: incomplete")
    expect(ui.page.locator("#na-reviews")).to_contain_text("1 sources omitted")
    ui.page.locator("#na-accept").click()
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-detail-summary")).to_contain_text("accepted")
    ui.page.locator("#na-close").click()
    path = ui.path(f"/v2/projects/{h.project.id}/intake")
    intake = ui.client.get(path).json()["intake"]
    response = ui.client.post(
        path + f"/sources/{sources[-1]['id']}/revoke",
        json={"expected_version": intake["version"], "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    preview_requests = []
    ui.page.on(
        "request",
        lambda request: preview_requests.append(request.url) if "/preview" in request.url else None,
    )
    open_artifacts(ui, h.project)
    ui.page.locator(f'[data-artifact-id="{h.detail["artifact"]["id"]}"]').get_by_role(
        "button", name="Inspect artifact"
    ).click()
    expect(ui.page.locator("#na-detail")).to_be_visible()
    expect(ui.page.locator("#na-preview")).to_contain_text("Source access was revoked")
    expect(ui.page.locator("#na-download")).to_be_disabled()
    expect(ui.page.locator("#na-review-new")).to_be_disabled()
    expect(ui.page.locator("#na-accept")).to_be_disabled()
    expect(ui.page.locator("#na-changes")).to_be_enabled()
    expect(ui.page.locator("#na-reviews")).to_contain_text("Source excerpt coverage: incomplete")
    assert "Candidate launch outline." not in ui.page.locator("#na-dialog").inner_text()
    assert not preview_requests
    ui.page.locator("#na-changes").click()
    ui.page.locator("#na-comment").fill("Rebuild using only current permitted evidence.")
    ui.page.locator("#na-save").click()
    expect(ui.page.locator("#na-dialog")).to_be_visible()
    expect(ui.page.locator("#na-decisions")).to_contain_text("Linked repair task")
    expect(ui.page.locator("#na-preview")).to_contain_text("Source access was revoked")
    expect(ui.page.locator("#na-download")).to_be_disabled()
    assert not preview_requests
