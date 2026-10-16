"""Authorized immutable handoffs through real human sessions and native workers."""

import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from simon.domain.models import utc_now
from simon.domain.native_execution import RunnerLeaseCommand
from simon.domain.native_projects import NativeProjectMember
from tests.integration.test_native_acceptance_browser import seed_package
from tests.integration.test_native_execution_browser import (
    advance,
    enroll_runner,
    seed_execution,
)
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def path(ui, project, suffix=""):
    return ui.path(f"/v2/projects/{project.id}/handoffs" + suffix)


def open_handoffs(ui, project, *, page=None):
    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-handoffs").click()
    page.locator("#nh-refresh:not([disabled])").wait_for()


def create_handoff(ui, project, recipient, *, title="Authorized continuation"):
    ui.page.locator("#nh-create").click()
    ui.page.locator("#nh-title").fill(title)
    ui.page.locator("#nh-recipient").select_option(str(recipient.actor.actor_id))
    ui.page.locator("#nh-create-save").click()
    ui.page.locator("#nh-detail-heading").filter(has_text=title).wait_for()
    response = ui.client.get(path(ui, project))
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def inspect(page, handoff):
    page.locator(f'#nh-items [data-handoff-id="{handoff["id"]}"]').get_by_role(
        "button", name="Inspect authorized handoff"
    ).click()
    page.locator("#nh-detail").wait_for()
    page.locator("#nh-refresh:not([disabled])").wait_for()


def export(page, tmp_path, filename="handoff.zip"):
    with page.expect_download() as event:
        page.locator("#nh-download").click()
    target = tmp_path / filename
    event.value.save_as(target)
    return target


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_handoff_snapshot_named_reader_export_verify_and_current_package_link(native_ui, tmp_path):
    ui = native_ui
    h = seed_package(ui)
    reader = ui.user(name="Named recipient")
    share(ui, h.project, reader)
    saved_fact = ui.client.post(
        ui.path(f"/v2/projects/{h.project.id}/dependencies/facts"),
        json={
            "key": "unit-price",
            "expected_version": 0,
            "value_type": "number",
            "value": 20,
            "unit": "USD",
            "sources": [],
            "idempotency_key": str(uuid4()),
        },
    )
    assert saved_fact.status_code == 201, saved_fact.text
    title = '<img src=x onerror="window.handoffInjected=true"> Reviewed takeover'
    open_handoffs(ui, h.project)
    handoff = create_handoff(ui, h.project, reader, title=title)
    assert ui.page.locator("#nh-dialog img, #nh-dialog script").count() == 0
    assert ui.page.evaluate("window.handoffInjected === undefined")
    ui.page.locator("#nh-close").click()
    open_handoffs(ui, h.project, page=reader.page)
    assert reader.page.locator("#nh-create").is_disabled()
    inspect(reader.page, handoff)
    text = reader.page.locator("#nh-snapshot").inner_text()
    assert "Captured facts and requirements" in text and "unit-price" in text and "20 USD" in text
    assert "Captured delivery packages" in text and "Editable specification package" in text
    assert "Current resource status" in text and "separate from the captured snapshot" in text
    assert "Recovery quarantine: inactive" in text
    assert reader.page.locator("#nh-resume-edit").is_disabled()
    archive_path = export(reader.page, tmp_path)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        current = json.loads(archive.read("current-status.json"))
        assert manifest["title"] == title
        assert len(manifest["snapshot"]["delivery_packets"]) == 1
        assert (
            current["current_resources"]["storage_usage"]["used_bytes"]
            > manifest["snapshot"]["storage"]["usage"]["used_bytes"]
        )
        assert len([name for name in archive.namelist() if name.startswith("files/")]) == 3
    reader.page.locator("#nh-archive").set_input_files(archive_path)
    reader.page.locator("#nh-verify").click()
    reader.page.locator("#nh-feedback").filter(has_text="Archive verified").wait_for()
    modified = io.BytesIO()
    with zipfile.ZipFile(archive_path) as original, zipfile.ZipFile(modified, "w") as changed:
        for entry in original.infolist():
            content = original.read(entry.filename)
            if entry.filename == "manifest.json":
                content = content.replace(b"Reviewed takeover", b"Altered takeover")
            changed.writestr(entry.filename, content)
    reader.page.locator("#nh-archive").set_input_files(
        {"name": "altered.zip", "mimeType": "application/zip", "buffer": modified.getvalue()}
    )
    reader.page.locator("#nh-verify").click()
    reader.page.locator('#nh-feedback[data-tone="error"]').wait_for()
    screenshots = Path(".local/native-continuity-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    reader.page.locator("#nh-detail-heading").scroll_into_view_if_needed()
    reader.page.screenshot(path=str(screenshots / "handoff-desktop.png"))
    reader.page.set_viewport_size({"width": 390, "height": 844})
    assert reader.page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth && "
        "document.querySelector('#nh-dialog').scrollWidth <= "
        "document.querySelector('#nh-dialog').clientWidth"
    )
    reader.page.screenshot(path=str(screenshots / "handoff-mobile.png"))
    reader.page.get_by_role("button", name="Inspect current package evidence").click()
    reader.page.locator("#nv-packet").filter(
        has_text=h.packet["packet"]["manifest"]["title"]
    ).wait_for()
    assert reader.page.locator("#nv-profile-edit").is_disabled()


def interrupted_checkpoint(ui, h, monkeypatch):
    execution = ui.container.native_execution
    started = ui.client.post(
        ui.path(f"/v2/projects/{h.project.id}/execution/tasks/{h.task.id}/runs"),
        json={"expected_task_version": h.task.version, "idempotency_key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    credential = enroll_runner(ui, h.project)
    with httpx.Client(
        base_url=ui.origin,
        trust_env=False,
        headers={"Authorization": "Bearer " + credential["token"]},
    ) as worker:
        response = worker.post(
            ui.path("/v2/execution-worker/claim"), json={"idempotency_key": str(uuid4())}
        )
        assert response.status_code == 200, response.text
        lease = response.json()
    original = execution._advance_checkpoint

    def stop_after_checkpoint(*_args):
        raise RuntimeError("Synthetic worker stopped after durable checkpoint")

    monkeypatch.setattr(execution, "_advance_checkpoint", stop_after_checkpoint)
    with pytest.raises(RuntimeError, match="durable checkpoint"):
        execution.step(
            execution.runner(credential["token"]),
            RunnerLeaseCommand(
                run_id=lease["run_id"],
                fence=lease["fence"],
                lease_token=lease["lease_token"],
                idempotency_key=str(uuid4()),
            ),
        )
    monkeypatch.setattr(execution, "_advance_checkpoint", original)
    run = execution._run(ui.actor.workspace_id, h.project.id, UUID(started.json()["id"]))
    assert run.step_number == 1 and run.applied_step == 0
    assert len(h.state.calls) == 1
    execution._save(run, lease_until=utc_now() - timedelta(seconds=1))
    return credential, run


def test_handoff_new_human_and_worker_apply_saved_model_checkpoint_once(native_ui, monkeypatch):
    ui = native_ui
    h = seed_execution(ui)
    original, run = interrupted_checkpoint(ui, h, monkeypatch)
    recipient = ui.user(name="Successor owner")
    ui.container.store.put_native_project_member(
        NativeProjectMember(
            workspace_id=ui.actor.workspace_id,
            project_id=h.project.id,
            actor_id=recipient.actor.actor_id,
            role="owner",
        )
    )
    worker_response = recipient.client.post(
        ui.path(f"/v2/projects/{h.project.id}/execution/runners"),
        json={"name": "Successor native worker", "idempotency_key": str(uuid4())},
    )
    assert worker_response.status_code == 201, worker_response.text
    successor = worker_response.json()
    open_handoffs(ui, h.project)
    handoff = create_handoff(ui, h.project, recipient)
    assert ui.page.locator("#nh-resume-edit").is_disabled()
    open_handoffs(ui, h.project, page=recipient.page)
    inspect(recipient.page, handoff)
    assert "0 applied / 1 recorded" in recipient.page.locator("#nh-snapshot").inner_text()
    recipient.page.locator("#nh-resume-edit").click()
    recipient.page.locator("#nh-runner").select_option(successor["runner"]["id"])
    recipient.page.locator("#nh-resume-save").click()
    recipient.page.locator("#nh-feedback").filter(has_text="Prior effects").wait_for()
    assert len(h.state.calls) == 1
    with httpx.Client(
        base_url=ui.origin,
        trust_env=False,
        headers={"Authorization": "Bearer " + original["token"]},
    ) as old_worker:
        assert (
            old_worker.post(
                ui.path("/v2/execution-worker/claim"), json={"idempotency_key": str(uuid4())}
            ).json()
            is None
        )
    result, _ = advance(ui, successor)
    assert result["id"] == str(run.id) and result["status"] == "completed"
    assert len(h.state.calls) == 1
    recipient.page.locator("#nh-refresh").click()
    recipient.page.locator("#nh-refresh:not([disabled])").wait_for()
    assert "0 applied / 1 recorded" in recipient.page.locator("#nh-snapshot").inner_text()
    current = ui.container.native_execution._run(ui.actor.workspace_id, h.project.id, run.id)
    assert current.applied_step == 1
    detail = recipient.client.get(path(ui, h.project, "/" + handoff["id"])).json()
    assert detail["actions"][0]["expected_run_version"] == current.version


def test_handoff_missing_receipt_retries_identical_command_and_revocation_withholds(native_ui):
    ui = native_ui
    h = seed_package(ui, evidence=True)
    open_handoffs(ui, h.project)
    ui.page.locator("#nh-create").click()
    ui.page.locator("#nh-title").fill("Exact retry snapshot")
    ui.page.locator("#nh-recipient").select_option(str(ui.actor.actor_id))
    requests = []

    def interrupt_first(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/handoffs", interrupt_first)
    ui.page.locator("#nh-create-save").click()
    ui.page.locator("#nh-uncertain").wait_for()
    assert ui.page.locator("#nh-title").is_disabled()
    assert ui.page.locator("#nh-close").is_disabled()
    ui.page.locator("#nh-check").click()
    ui.page.locator("#nh-retry:not([disabled])").wait_for()
    ui.page.locator("#nh-retry").click()
    ui.page.locator("#nh-detail-heading").filter(has_text="Exact retry snapshot").wait_for()
    assert requests[0] == requests[1]
    assert len(ui.client.get(path(ui, h.project)).json()["items"]) == 1
    ui.container.store.revoke_native_intake_source(
        ui.actor.workspace_id, h.project.id, h.source.id, utc_now()
    )
    ui.page.locator("#nh-refresh").click()
    ui.page.locator("#nh-applicability").filter(has_text="no longer permits").wait_for()
    assert ui.page.locator("#nh-snapshot").inner_text() == ""
    assert ui.page.locator("#nh-download").is_disabled()
    assert ui.page.locator("#nh-verify").is_disabled()
    assert ui.page.locator("#nh-resume-edit").is_disabled()


def test_late_handoff_response_cannot_restore_another_project_snapshot(native_ui):
    ui = native_ui
    first, second = seed_project(ui), seed_project(ui, "Other project")
    response = ui.client.post(
        path(ui, first),
        json={
            "recipient_id": str(ui.actor.actor_id),
            "title": "Private first-project handoff",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    handoff = response.json()["handoff"]
    open_handoffs(ui, first)

    def switch_during_request(route):
        fetched = route.fetch()
        assert fetched.ok
        ui.page.evaluate(
            "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
            "{detail: {id}})); history.pushState({}, '', '?project=' + id); "
            "window.dispatchEvent(new PopStateEvent('popstate')); }",
            str(second.id),
        )
        route.fulfill(response=fetched)

    ui.page.route("**/handoffs/" + handoff["id"], switch_during_request)
    ui.page.locator(f'#nh-items [data-handoff-id="{handoff["id"]}"]').get_by_role(
        "button", name="Inspect authorized handoff"
    ).click()
    ui.page.locator("#np-name").filter(has_text=second.name).wait_for()
    assert not ui.page.locator("#nh-dialog").is_visible()
    assert ui.page.locator("#nh-snapshot").inner_text() == ""
    assert ui.page.locator("#nh-detail-heading").inner_text() == ""
