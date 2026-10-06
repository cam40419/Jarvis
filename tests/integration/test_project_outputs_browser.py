"""Reuse real saved agent outputs through local project storage."""

from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest

from tests.integration.test_project_command_browser import create_project, refresh
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


def test_saved_output_download_reuse_and_conflict_are_visible(project_ui, tmp_path):
    from playwright.sync_api import expect

    page, dispatcher, scheduler, container, _ = project_ui
    project_id = create_project(page, "Reusable project results")
    page.locator("#pc-command").fill("Prepare a sourced project report.")
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
    page.locator("#pc-command").fill("Expand the report with a cost comparison.")
    # Publish explicit named deliverables alongside the ordinary model responses.
    # The synthetic tool-free model produces answers, not authored workspace files.
    listing = page.evaluate("id => api('/v1/projects/' + id + '/outputs')", project_id)
    state = page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"]
    actor = container.agent_runs.actor_resolver(
        UUID(state["actor_id"]), UUID(state["workspace_id"])
    )
    for task_id, filename in (("brief", "launch-brief.md"), ("research", "research-notes.md")):
        source = next(item for item in listing["items"] if item["task_id"] == task_id)
        _, metadata = container.project_outputs.source(
            actor, UUID(project_id), UUID(source["run_id"]), UUID(source["id"])
        )
        artifact = container.project_outputs.artifacts.publish_bytes(
            workspace_id=actor.workspace_id,
            actor_id=actor.actor_id,
            run_id=metadata.run_id,
            task_id=metadata.task_id,
            name=filename,
            media_type="text/markdown",
            content=b"# Launch report\nA useful, saved launch brief with checked assumptions.",
        )
        run = container.agent_runs.get(actor, metadata.run_id)
        run = run.model_copy(
            update={
                "tasks": tuple(
                    task.model_copy(update={"artifacts": (*task.artifacts, artifact)})
                    if task.id == task_id
                    else task
                    for task in run.tasks
                )
            }
        )
        job = container.store.get_job(run.id)
        container.store.save_job(
            job.model_copy(
                update={
                    "input": {**job.input, "initial_state": run.model_dump(mode="json")},
                    "result": run.model_dump(mode="json"),
                }
            ),
            job.version,
        )
    page.locator("#pc-tab-files").click()
    listing = page.evaluate(
        "id => api('/v1/projects/' + id + '/outputs?kind=deliverable')", project_id
    )
    assert all(item["kind"] == "deliverable" for item in listing["items"])
    brief = next(item for item in listing["items"] if item["name"] == "launch-brief.md")
    research = next(item for item in listing["items"] if item["name"] == "research-notes.md")
    expect(page.locator(".po-output")).to_have_count(2)
    row = page.locator(f'.po-output[data-output="{brief["id"]}"]')
    expect(row).to_be_visible()
    row.get_by_role("button", name="Create editable copy: " + brief["name"], exact=True).click()
    expect(row).to_contain_text("Editable local copy")
    with page.expect_download() as copied:
        row.get_by_role("link", name="Download project copy", exact=True).click()
    with page.expect_download() as original:
        row.get_by_role("link", name="Download source", exact=True).click()
    assert Path(copied.value.path()).read_bytes() == Path(original.value.path()).read_bytes()
    assert b"saved launch brief" in Path(copied.value.path()).read_bytes()
    with page.expect_download() as word_document:
        row.get_by_role("link", name="Download Word document", exact=True).click()
    assert word_document.value.suggested_filename.endswith(".docx")
    with ZipFile(word_document.value.path()) as package:
        assert b"saved launch brief" in package.read("word/document.xml")
        assert "word/styles.xml" in package.namelist()
    row.get_by_role("button", name="Use in next task", exact=True).click()
    draft = page.locator("#pc-command").input_value()
    assert draft.startswith("Expand the report with a cost comparison.")
    assert "outputs/" in draft
    expect(page.locator("#pc-command")).to_be_focused()
    assert (
        page.evaluate("id => api('/v1/projects/' + id + '/command')", project_id)["state"][
            "active_cycle"
        ]
        is None
    )
    page.reload()
    page.locator("#pc-tab-files").click()
    expect(row).to_contain_text("Editable local copy")
    row.get_by_role("button", name="Open local folder", exact=True).click()
    expect(page.locator("#local-files-panel")).to_contain_text(
        brief["id"][:8] + "-" + brief["name"]
    )
    page.keyboard.press("Escape")
    destination = "outputs/" + research["id"][:8] + "-" + research["name"]
    page.evaluate(
        """({id,path}) => api('/v1/local-files/action', {
          name:'local_file_write',arguments:{root:'project:'+id,path,
            content:'Human-edited file must remain intact'},
          idempotency_key:'output-conflict-file'})""",
        {"id": project_id, "path": destination},
    )
    conflict = page.locator(f'.po-output[data-output="{research["id"]}"]')
    conflict.get_by_role(
        "button", name="Create editable copy: " + research["name"], exact=True
    ).click()
    expect(conflict).to_contain_text("Existing files are kept")
    with page.expect_download() as retained:
        page.evaluate(
            """({id,path}) => { const a=document.createElement('a');
              const query=new URLSearchParams({root:'project:'+id,path});
              a.href=appPath('/v1/local-files/download?'+query);
              a.download='existing.txt'; document.body.append(a); a.click(); a.remove(); }""",
            {"id": project_id, "path": destination},
        )
    assert Path(retained.value.path()).read_text() == "Human-edited file must remain intact"
    page.locator("#project-outputs").scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "project-outputs-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    row.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "project-outputs-mobile.png"), animations="disabled")


def test_output_list_error_can_be_retried_and_read_only_copy_controls_are_hidden(project_ui):
    from playwright.sync_api import expect

    page, _, _, _, _ = project_ui
    project_id = create_project(page)
    endpoint = f"**/v1/projects/{project_id}/outputs?*"

    def unavailable(route):
        route.fulfill(
            status=503, json={"error": {"message": "Saved outputs are temporarily unavailable."}}
        )

    page.route(endpoint, unavailable)
    page.locator("#pc-tab-files").click()
    expect(page.locator("#po-status")).to_contain_text("temporarily unavailable")
    page.unroute(endpoint, unavailable)
    page.get_by_role("button", name="Refresh outputs", exact=True).click()
    expect(page.locator("#po-list")).to_contain_text("No deliverable files yet")
    page.evaluate("() => { session.scopes=session.scopes.filter(scope=>scope!=='jobs:write'); }")
    page.get_by_role("button", name="Refresh outputs", exact=True).click()
    expect(page.locator("#po-status")).to_contain_text("requires project write access")
    expect(
        page.locator("#project-outputs").get_by_role("button", name="Create editable copy")
    ).to_have_count(0)


def test_files_uses_server_classification_and_links_answers_to_history(project_ui):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page)
    requests = []

    def outputs(route):
        requests.append(route.request.url)
        common = {
            "run_id": "run-1",
            "task_id": "report",
            "media_type": "text/plain",
            "created_at": "2026-10-02T12:00:00Z",
            "size": 100,
            "project_copy": None,
            "status": "accepted",
            "source": "artifact",
        }
        route.fulfill(
            json={
                "project_id": project_id,
                "next_cursor": None,
                "can_promote": False,
                "items": [
                    {
                        **common,
                        "id": "conversation",
                        "kind": "response",
                        "title": "Ordinary project answer",
                        "name": "supplier-overview.md",
                        "download_url": "/v1/agent-platform/runs/run-1/artifacts/conversation",
                    },
                    {
                        **common,
                        "id": "authored-file",
                        "kind": "deliverable",
                        "title": "Requested questionnaire answers",
                        "name": "answer.txt",
                        "download_url": "/v1/agent-platform/runs/run-1/artifacts/authored-file",
                    },
                    {
                        **common,
                        "id": "named-report",
                        "kind": "response",
                        "source": "candidate",
                        "title": "Internal draft title",
                        "name": "answer.txt",
                        "download_url": "/v1/agent-platform/runs/run-1/artifacts/named-report",
                        "document_url": (
                            f"/v1/projects/{project_id}/outputs/run-1/named-report/document"
                        ),
                        "document_name": "Supplier Comparison.docx",
                    },
                ],
            }
        )

    page.route(f"**/v1/projects/{project_id}/outputs?*", outputs)
    page.locator("#pc-tab-files").click()
    expect(page.locator("#project-outputs")).to_contain_text("Deliverable files")
    expect(page.locator(".po-output")).to_have_count(2)
    expect(page.locator("#po-list")).to_contain_text("Requested questionnaire answers")
    expect(page.locator("#po-list")).to_contain_text("answer.txt")
    expect(page.locator("#po-list")).not_to_contain_text("Ordinary project answer")
    named = page.locator('[data-output="named-report"]')
    expect(named.locator("strong").first).to_have_text("Supplier Comparison.docx")
    expect(named).to_contain_text("Word document")
    expect(named).not_to_contain_text("Internal draft title")
    expect(named).not_to_contain_text("answer.txt")
    expect(named.get_by_role("link", name="Download Word document")).to_have_attribute(
        "href", f"/v1/projects/{project_id}/outputs/run-1/named-report/document"
    )
    expect(named.get_by_role("link", name="Download source")).to_have_attribute(
        "href", "/v1/agent-platform/runs/run-1/artifacts/named-report"
    )
    assert requests and all("kind=deliverable" in url for url in requests)
    page.get_by_role("button", name="Read answers in Run history", exact=True).click()
    expect(page.locator("#pc-history-panel")).to_be_visible()


def test_word_export_is_offered_only_when_available_and_native_word_has_no_duplicate(project_ui):
    from playwright.sync_api import expect

    page, *_ = project_ui
    project_id = create_project(page)
    base = f"/v1/projects/{project_id}/outputs/run"
    common = {
        "run_id": "run",
        "task_id": "report",
        "kind": "deliverable",
        "source": "artifact",
        "status": "accepted",
        "created_at": "2026-10-02T12:00:00Z",
        "size": 200,
        "project_copy": None,
    }
    page.route(
        f"**/v1/projects/{project_id}/outputs?*",
        lambda route: route.fulfill(
            json={
                "project_id": project_id,
                "next_cursor": None,
                "can_promote": False,
                "items": [
                    {
                        **common,
                        "id": "text",
                        "name": "Supplier comparison.md",
                        "media_type": "text/markdown",
                        "download_url": base + "/text/download",
                        "document_url": base + "/text/document",
                        "document_name": "Supplier comparison.docx",
                    },
                    {
                        **common,
                        "id": "word",
                        "name": "Manufacturing strategy.docx",
                        "media_type": (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        "download_url": base + "/word/download",
                    },
                    {
                        **common,
                        "id": "csv",
                        "name": "Unit costs.csv",
                        "media_type": "text/csv",
                        "download_url": base + "/csv/download",
                    },
                ],
            }
        ),
    )
    page.locator("#pc-tab-files").click()
    text_row = page.locator('[data-output="text"]')
    expect(text_row.locator("strong").first).to_have_text("Supplier comparison.docx")
    expect(text_row).to_contain_text("Word document")
    expect(text_row).not_to_contain_text("Supplier comparison.md")
    expect(text_row).not_to_contain_text("200 B")
    expect(text_row.get_by_role("link", name="Download Word document")).to_have_attribute(
        "href", base + "/text/document"
    )
    expect(text_row.get_by_role("link", name="Download source")).to_have_attribute(
        "href", base + "/text/download"
    )
    word_row = page.locator('[data-output="word"]')
    expect(word_row).to_contain_text("Word document")
    expect(word_row).to_contain_text("200 B")
    expect(word_row.get_by_role("link")).to_have_count(1)
    expect(word_row.get_by_role("link", name="Download Word document")).to_have_attribute(
        "href", base + "/word/download"
    )
    expect(
        page.locator('[data-output="csv"]').get_by_role("link", name="Download Word document")
    ).to_have_count(0)
