"""Exact-file delivery snapshots, portable exports and current access in real browsers."""

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from simon.domain.native_projects import CreateNativeTask
from simon.services.intake_sources import IntakeSourceBytes
from tests.integration.test_native_artifacts_browser import add_intake_source, upload_api
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def packet_path(ui, project, suffix=""):
    return ui.path(f"/v2/projects/{project.id}/delivery-packets" + suffix)


def packet_view(ui, project):
    response = ui.client.get(packet_path(ui, project))
    assert response.status_code == 200, response.text
    return response.json()


def packet_detail(ui, project, identifier):
    response = ui.client.get(packet_path(ui, project, "/" + identifier))
    assert response.status_code == 200, response.text
    return response.json()


def seed_outputs(ui, *, project=None):
    project = project or seed_project(ui)
    details = []
    originals = [
        ("outline.md", "text/markdown", b"# Launch outline\nEditable milestone draft."),
        ("reference.json", "application/json", b'{"audience":"test subscribers"}'),
        ("attachment.bin", "application/octet-stream", b"\x00\x01Original binary attachment\xff"),
    ]
    tasks = []
    for filename, media_type, content in originals:
        task = ui.service.create_task(
            ui.actor,
            project.id,
            CreateNativeTask(title="Prepare " + filename, idempotency_key=str(uuid4())),
        )
        tasks.append(task)
        details.append(
            upload_api(
                ui,
                SimpleNamespace(project=project, task=task),
                content,
                filename=filename,
                media_type=media_type,
            )
        )
    return SimpleNamespace(project=project, details=details, originals=originals, tasks=tasks)


def packet_body(details, title="Launch handoff", **changes):
    components = []
    for index, detail in enumerate(details):
        revision = next(
            item
            for item in detail["revisions"]
            if item["id"] == detail["artifact"]["current_revision_id"]
        )
        components.append(
            {
                "artifact_id": detail["artifact"]["id"],
                "revision_id": revision["id"],
                "sha256": revision["sha256"],
                "role": "source" if index < 2 else "attachment",
                "derived_from_revision_id": None,
            }
        )
    return {
        "title": title,
        "components": components,
        "limitations": [],
        "previous_packet_id": None,
        "idempotency_key": str(uuid4()),
        **changes,
    }


def create_packet_api(ui, h, **changes):
    body = packet_body(h.details, **changes)
    response = ui.client.post(packet_path(ui, h.project), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def open_delivery(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-delivery").click()
    expect(page.locator("#nd-dialog")).to_be_visible()
    expect(page.locator("#nd-refresh")).to_be_enabled()


def select_files(ui, details):
    for index, detail in enumerate(details):
        identifier = detail["artifact"]["id"]
        ui.page.locator("#nd-pick-" + identifier).check()
        ui.page.locator("#nd-role-" + identifier).select_option(
            "source" if index < 2 else "attachment"
        )


def create_in_browser(
    ui, h, title="Launch handoff", notes="Visual rendering still needs validation."
):
    from playwright.sync_api import expect

    ui.page.locator("#nd-create").click()
    ui.page.locator("#nd-title").fill(title)
    ui.page.locator("#nd-notes").fill(notes)
    select_files(ui, h.details)
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-detail")).to_be_visible()
    expect(ui.page.locator("#nd-download")).to_be_enabled()
    saved = packet_view(ui, h.project)["packets"]["items"][0]
    return packet_detail(ui, h.project, saved["packet"]["id"])


def inspect(ui, record, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    page.locator(f'[data-packet-id="{record["packet"]["id"]}"]').get_by_role(
        "button", name="Inspect packet"
    ).click()
    expect(page.locator("#nd-detail")).to_be_visible()
    expect(page.locator("#nd-download")).to_be_enabled()


def download_zip(ui, tmp_path, *, page=None, filename="packet.zip"):
    page = page or ui.page
    with page.expect_download() as result:
        page.locator("#nd-download").click()
    target = tmp_path / filename
    result.value.save_as(target)
    return target


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_multifile_packet_creation_exports_exact_files_and_separate_status(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    open_delivery(ui, h.project)
    record = create_in_browser(ui, h)
    expect(ui.page.locator("#nd-applicability")).to_have_attribute("data-current", "true")
    expect(ui.page.locator("#nd-applicability")).to_have_attribute("data-accepted", "false")
    expect(ui.page.locator("#nd-components .ni-card")).to_have_count(3)
    expect(ui.page.locator("#nd-previews .ni-card")).to_have_count(2)
    expect(ui.page.locator("#nd-limitations")).to_contain_text(
        "Visual rendering still needs validation."
    )
    screenshots = Path(".local/native-delivery-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.locator("#nd-heading").scroll_into_view_if_needed()
    ui.page.screenshot(path=str(screenshots / "delivery-desktop.png"), animations="disabled")
    with zipfile.ZipFile(download_zip(ui, tmp_path)) as archive:
        raw_manifest = archive.read("manifest.json")
        assert hashlib.sha256(raw_manifest).hexdigest() == record["packet"]["manifest_sha256"]
        manifest = json.loads(raw_manifest)
        assert manifest["title"] == "Launch handoff"
        paths = json.loads(archive.read("export-paths.json"))
        for detail, (_, _, content) in zip(h.details, h.originals, strict=True):
            revision_id = detail["artifact"]["current_revision_id"]
            assert archive.read(paths[revision_id]) == content
        status = json.loads(archive.read("current-status.json"))
        assert status["applicability"] == record["applicability"]
        assert status["applicability"]["current"] is True
        assert status["applicability"]["components_accepted"] is False


def test_packet_fields_source_inspection_and_export_previews_are_inert(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    content = "<script>window.deliveryInjected=true</script><img src=x onerror=alert(1)>"
    replacement = upload_api(
        ui,
        SimpleNamespace(project=h.project, task=h.tasks[0]),
        content,
        detail=h.details[0],
        filename="untrusted.html",
        media_type="text/html",
    )
    h.details[0] = replacement
    open_delivery(ui, h.project)
    ui.page.locator("#nd-create").click()
    malicious = '<img src=x onerror="window.deliveryInjected=true">'
    ui.page.locator("#nd-title").fill(malicious)
    ui.page.locator("#nd-notes").fill(malicious)
    select_files(ui, h.details)
    ui.page.locator(f'[data-artifact-id="{replacement["artifact"]["id"]}"]').get_by_role(
        "button", name="Inspect text"
    ).click()
    expect(ui.page.locator("#nd-source-text")).to_have_text(content)
    assert ui.page.locator("#nd-source-preview img, #nd-source-preview script").count() == 0
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-detail-heading")).to_have_text(malicious)
    expect(ui.page.locator("#nd-limitations")).to_contain_text(malicious)
    assert ui.page.locator("#nd-dialog img, #nd-dialog script").count() == 0
    assert ui.page.evaluate("window.deliveryInjected === undefined")
    with zipfile.ZipFile(download_zip(ui, tmp_path)) as archive:
        preview = archive.read(
            "previews/" + replacement["artifact"]["current_revision_id"] + ".html"
        ).decode()
        assert "&lt;script&gt;window.deliveryInjected=true&lt;/script&gt;" in preview
        assert "<script>" not in preview and "<img src=x" not in preview


def test_read_only_mobile_packet_inspection_and_download(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    visitor = ui.user(role="guest", name="Packet recipient")
    share(ui, h.project, visitor)
    record = create_packet_api(ui, h)
    visitor.page.set_viewport_size({"width": 390, "height": 844})
    open_delivery(ui, h.project, page=visitor.page)
    expect(visitor.page.locator("#nd-create")).to_be_disabled()
    inspect(ui, record, page=visitor.page)
    expect(visitor.page.locator("#nd-revise")).to_be_disabled()
    assert visitor.page.locator("#nd-dialog").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    screenshots = Path(".local/native-delivery-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    visitor.page.locator("#nd-heading").scroll_into_view_if_needed()
    visitor.page.screenshot(path=str(screenshots / "delivery-mobile.png"), animations="disabled")
    with zipfile.ZipFile(download_zip(ui, tmp_path, page=visitor.page)) as archive:
        assert json.loads(archive.read("manifest.json"))["title"] == "Launch handoff"
    response = visitor.client.post(packet_path(ui, h.project), json=packet_body(h.details))
    assert response.status_code == 403


def test_stale_packet_preserves_original_files_and_new_version_captures_current_heads(
    native_ui, tmp_path
):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    first = create_packet_api(ui, h, limitations=["Owner note retained across packet versions."])
    open_delivery(ui, h.project)
    inspect(ui, first)
    with zipfile.ZipFile(download_zip(ui, tmp_path, filename="original.zip")) as archive:
        original_manifest = archive.read("manifest.json")
    h.details[0] = upload_api(
        ui,
        SimpleNamespace(project=h.project, task=h.tasks[0]),
        "Revised current outline.",
        detail=h.details[0],
        filename="outline.md",
        media_type="text/markdown",
    )
    ui.page.locator("#nd-refresh").click()
    expect(ui.page.locator("#nd-applicability")).to_have_attribute("data-current", "false")
    expect(ui.page.locator("#nd-reasons")).to_contain_text("newer revision")
    with zipfile.ZipFile(download_zip(ui, tmp_path, filename="historical.zip")) as archive:
        assert archive.read("manifest.json") == original_manifest
        assert json.loads(archive.read("current-status.json"))["applicability"]["current"] is False
    ui.page.locator("#nd-revise").click()
    expect(ui.page.locator("#nd-notes")).to_have_value(
        "Owner note retained across packet versions."
    )
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-detail-summary")).to_contain_text("Packet version 2")
    expect(ui.page.locator("#nd-applicability")).to_have_attribute("data-current", "true")
    records = packet_view(ui, h.project)["packets"]["items"]
    second = next(item for item in records if item["packet"]["sequence"] == 2)
    second = packet_detail(ui, h.project, second["packet"]["id"])
    assert second["packet"]["previous_packet_id"] == first["packet"]["id"]
    assert second["packet"]["manifest"]["declared_limitations"] == [
        "Owner note retained across packet versions."
    ]
    assert (
        second["packet"]["manifest"]["components"][0]["revision_id"]
        == h.details[0]["artifact"]["current_revision_id"]
    )


def test_lost_packet_creation_response_recovers_receipt_without_duplicate(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    open_delivery(ui, h.project)
    attempts = []

    def lose_response(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 201
        route.abort("failed")

    ui.page.route("**/delivery-packets", lose_response)
    ui.page.locator("#nd-create").click()
    ui.page.locator("#nd-title").fill("Receipt-safe packet")
    select_files(ui, h.details)
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-uncertain")).to_be_visible()
    expect(ui.page.locator("#nd-close")).to_be_disabled()
    expect(ui.page.locator("#nd-title")).to_be_disabled()
    ui.page.locator("#nd-check").click()
    expect(ui.page.locator("#nd-uncertain")).not_to_be_visible()
    expect(ui.page.locator("#nd-detail-heading")).to_have_text("Receipt-safe packet")
    assert len(attempts) == len(packet_view(ui, h.project)["packets"]["items"]) == 1


def test_unsaved_packet_retries_exact_command_and_key(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    open_delivery(ui, h.project)
    attempts = []

    def lose_first_request(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts.append(route.request.post_data_json)
        route.abort("failed") if len(attempts) == 1 else route.continue_()

    ui.page.route("**/delivery-packets", lose_first_request)
    ui.page.locator("#nd-create").click()
    ui.page.locator("#nd-title").fill("Retry exact source revisions")
    select_files(ui, h.details)
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-uncertain")).to_be_visible()
    ui.page.locator("#nd-check").click()
    expect(ui.page.locator("#nd-feedback")).to_contain_text("No saved receipt")
    ui.page.locator("#nd-retry").click()
    expect(ui.page.locator("#nd-detail-heading")).to_have_text("Retry exact source revisions")
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len(packet_view(ui, h.project)["packets"]["items"]) == 1


def test_changed_source_conflict_preserves_notes_until_explicit_reload(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    open_delivery(ui, h.project)
    ui.page.locator("#nd-create").click()
    ui.page.locator("#nd-title").fill("Draft title preserved")
    ui.page.locator("#nd-notes").fill("Draft limitations preserved.")
    select_files(ui, h.details)
    h.details[0] = upload_api(
        ui,
        SimpleNamespace(project=h.project, task=h.tasks[0]),
        "Updated source head.",
        detail=h.details[0],
        filename="outline.md",
        media_type="text/markdown",
    )
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-conflict")).to_be_visible()
    expect(ui.page.locator("#nd-save")).to_be_disabled()
    expect(ui.page.locator("#nd-title")).to_have_value("Draft title preserved")
    expect(ui.page.locator("#nd-notes")).to_have_value("Draft limitations preserved.")
    ui.page.locator("#nd-reload").click()
    expect(ui.page.locator("#nd-conflict")).not_to_be_visible()
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-detail-heading")).to_have_text("Draft title preserved")
    expect(ui.page.locator("#nd-applicability")).to_have_attribute("data-current", "true")


def test_source_revocation_withholds_packet_and_blocks_pending_download(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    ui.container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    intake_path = ui.path(f"/v2/projects/{project.id}/intake")
    source = add_intake_source(ui, project, "context.txt", "Confidential source context.")
    h = seed_outputs(ui, project=project)
    record = create_packet_api(ui, h, title="Confidential packet title")
    open_delivery(ui, project)
    inspect(ui, record)
    latest = ui.client.get(intake_path).json()["intake"]
    response = ui.client.post(
        intake_path + f"/sources/{source['id']}/revoke",
        json={
            "expected_version": latest["version"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    ui.page.locator("#nd-download").click()
    expect(ui.page.locator("#nd-dialog")).not_to_be_visible()
    expect(ui.page.locator("#nd-components")).to_be_empty()
    open_delivery(ui, project)
    expect(ui.page.locator("#nd-packets")).to_contain_text("Withheld delivery packet")
    assert "Confidential packet title" not in ui.page.locator("#nd-packets").inner_text()
    assert ui.page.locator("#nd-packets button").count() == 0


def test_packet_pagination_and_late_detail_response_are_fenced(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    for index in range(51):
        create_packet_api(ui, h, title=f"Saved handoff {index}")
    second = seed_project(ui, "Another workspace project")
    open_delivery(ui, h.project)
    expect(ui.page.locator("#nd-packets [data-packet-id]")).to_have_count(50)
    ui.page.locator("#nd-more").click()
    expect(ui.page.locator("#nd-packets [data-packet-id]")).to_have_count(51)
    expect(ui.page.locator("#nd-more")).not_to_be_visible()
    first = packet_view(ui, h.project)["packets"]["items"][0]
    inspect(ui, first)
    ui.page.locator("#nd-revise").click()
    expect(ui.page.locator("#nd-editor-context")).to_contain_text(
        first["packet"]["manifest"]["title"]
    )
    ui.page.locator("#nd-cancel").click()
    ui.page.locator("#nd-back").click()

    def navigate_during_detail(route):
        response = route.fetch()
        assert response.ok
        ui.page.evaluate(
            "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
            "{detail: {id}})); history.pushState({}, '', '?project=' + id); "
            "window.dispatchEvent(new PopStateEvent('popstate')); }",
            str(second.id),
        )
        route.fulfill(response=response)

    ui.page.route("**/delivery-packets/*", navigate_during_detail)
    ui.page.locator("#nd-packets button").first.click()
    expect(ui.page.locator("#np-name")).to_have_text(second.name)
    expect(ui.page.locator("#nd-dialog")).not_to_be_visible()
    expect(ui.page.locator("#nd-components")).to_be_empty()
    expect(ui.page.locator("#nd-packets [data-packet-id]")).to_have_count(0)
    expect(ui.page.locator("#nd-editor-context")).to_be_empty()


def test_filtered_candidate_page_uses_raw_cursor_and_preserves_version_selections(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    record = create_packet_api(ui, h)
    candidates = packet_view(ui, h.project)["artifacts"]["items"]
    offsets = []

    def filtered_page(route):
        response = route.fetch()
        assert response.ok
        body = response.json()
        offset = int(parse_qs(urlsplit(route.request.url).query).get("artifact_offset", ["0"])[0])
        offsets.append(offset)
        body["artifacts"] = {
            "items": [] if offset == 0 else candidates,
            "offset": offset,
            "limit": 50,
            "has_more": offset == 0,
        }
        route.fulfill(response=response, json=body)

    ui.page.route("**/delivery-packets?*", filtered_page)
    open_delivery(ui, h.project)
    expect(ui.page.locator("#nd-create")).to_be_enabled()
    inspect(ui, record)
    ui.page.locator("#nd-revise").click()
    expect(ui.page.locator("#nd-selection-reason")).to_contain_text("3 previously selected files")
    expect(ui.page.locator("#nd-save")).to_be_disabled()
    ui.page.locator("#nd-artifacts-more").click()
    expect(ui.page.locator("#nd-candidates input:checked")).to_have_count(3)
    expect(ui.page.locator("#nd-save")).to_be_enabled()
    assert offsets[:2] == [0, 50]
    for index, item in enumerate(h.details):
        expect(ui.page.locator("#nd-role-" + item["artifact"]["id"])).to_have_value(
            "source" if index < 2 else "attachment"
        )
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-detail-summary")).to_contain_text("Packet version 2")
    saved = next(
        item
        for item in packet_view(ui, h.project)["packets"]["items"]
        if item["packet"]["sequence"] == 2
    )
    saved = packet_detail(ui, h.project, saved["packet"]["id"])
    assert len(saved["packet"]["manifest"]["components"]) == 3


def test_hidden_successor_locks_old_parent_and_conflict_reload_follows_latest(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    first = create_packet_api(ui, h, title="Original family")
    open_delivery(ui, h.project)
    inspect(ui, first)
    ui.page.locator("#nd-revise").click()
    ui.page.locator("#nd-title").fill("Owner draft title retained")
    ui.page.locator("#nd-notes").fill("Owner draft limitations retained.")
    for index in range(50):
        create_packet_api(ui, h, title=f"Other handoff {index}")
    second = create_packet_api(
        ui, h, title="Concurrent family successor", previous_packet_id=first["packet"]["id"]
    )
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-conflict")).to_be_visible()
    ui.page.locator("#nd-reload").click()
    expect(ui.page.locator("#nd-conflict")).not_to_be_visible()
    expect(ui.page.locator("#nd-title")).to_have_value("Owner draft title retained")
    expect(ui.page.locator("#nd-notes")).to_have_value("Owner draft limitations retained.")
    expect(ui.page.locator("#nd-editor-context")).to_contain_text("Version 3")
    expect(ui.page.locator("#nd-editor-context")).to_contain_text("Concurrent family successor")
    expect(ui.page.locator("#nd-candidates input:checked")).to_have_count(3)
    ui.page.locator("#nd-save").click()
    expect(ui.page.locator("#nd-detail-summary")).to_contain_text("Packet version 3")
    packets = ui.container.store.delivery_packets(ui.actor.workspace_id, h.project.id, 0, 100)
    third = next(item for item in packets if item.sequence == 3)
    assert str(third.previous_packet_id) == second["packet"]["id"]
    assert third.manifest.title == "Owner draft title retained"
    assert third.manifest.declared_limitations == ("Owner draft limitations retained.",)
    ui.page.locator("#nd-back").click()
    expect(ui.page.locator("#nd-packets [data-packet-id]")).to_have_count(50)
    assert ui.page.locator(f'[data-packet-id="{second["packet"]["id"]}"]').count() == 0
    inspect(ui, first)
    expect(ui.page.locator("#nd-revise")).to_be_disabled()
    list_queries = [urlsplit(url).query for _, url in ui.requests if "/delivery-packets?" in url]
    assert list_queries and all(
        parse_qs(query).get("offset", ["0"]) == ["0"] for query in list_queries
    )


def test_supplied_preview_bindings_remain_inspectable_and_are_not_coerced(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_outputs(ui)
    components = packet_body(h.details[:2])["components"]
    components[1]["role"] = "preview"
    components[1]["derived_from_revision_id"] = components[0]["revision_id"]
    record = create_packet_api(
        ui, h, title="Supplied preview with source binding", components=components
    )
    open_delivery(ui, h.project)
    inspect(ui, record)
    expect(ui.page.locator("#nd-revise")).to_be_disabled()
    expect(ui.page.locator("#nd-version-reason")).to_contain_text(
        "Editing those links is unavailable in this composer"
    )
    expect(ui.page.locator("#nd-components")).to_contain_text("reference.json")
    expect(ui.page.locator("#nd-download")).to_be_enabled()
    with zipfile.ZipFile(download_zip(ui, tmp_path)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        preview = manifest["components"][1]
        assert preview["role"] == "preview"
        assert preview["derived_from_revision_id"] == components[0]["revision_id"]
        paths = json.loads(archive.read("export-paths.json"))
        assert archive.read(paths[preview["revision_id"]]) == h.originals[1][2]
    saved = packet_detail(ui, h.project, record["packet"]["id"])
    assert saved["packet"]["manifest"]["components"][1]["role"] == "preview"
    assert (
        saved["packet"]["manifest"]["components"][1]["derived_from_revision_id"]
        == components[0]["revision_id"]
    )
    assert len(packet_view(ui, h.project)["packets"]["items"]) == 1
