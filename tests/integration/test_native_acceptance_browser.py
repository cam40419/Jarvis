"""Explicit local package validation and owner acceptance in isolated browsers."""

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.models import utc_now
from simon.domain.native_projects import CreateNativeTask
from tests.api.test_native_artifacts_api import target_command
from tests.api.test_native_dependencies_api import declare
from tests.contract.test_native_intake_store import source
from tests.integration.test_native_artifacts_browser import review_policy, upload_api
from tests.integration.test_native_delivery_browser import (
    download_zip,
    inspect,
    open_delivery,
    packet_body,
    packet_path,
)
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import seed_project, share

pytestmark = pytest.mark.browser

SCHEMA = {
    "type": "object",
    "properties": {"unit_price": {"type": "number", "minimum": 0}, "color": {"type": "string"}},
    "required": ["unit_price", "color"],
    "additionalProperties": False,
}


def seed_package(ui, *, evidence=False):
    project = seed_project(ui)
    if evidence:
        item = source(
            SimpleNamespace(
                workspace=ui.actor.workspace_id, first=project, owner=ui.actor.actor_id
            ),
            text="Private package evidence",
        )
        ui.container.store.insert_native_intake_source(item)
    else:
        item = None
    review_policy(ui, project, require_agent_review=False, require_human_approval=True)
    details = []
    for filename, media_type, text in (
        ("spec.json", "application/json", '{"unit_price":20,"color":"navy"}\n'),
        (
            "rationale.md",
            "text/markdown",
            "# Rationale\nOwner-defined scope and remaining limitations.\n",
        ),
        ("schema.json", "application/json", json.dumps({"schema_version": 1, "schema": SCHEMA})),
    ):
        task = ui.service.create_task(
            ui.actor,
            project.id,
            CreateNativeTask(title="Prepare " + filename, idempotency_key=str(uuid4())),
        )
        detail = upload_api(
            ui,
            SimpleNamespace(project=project, task=task),
            text,
            filename=filename,
            media_type=media_type,
        )
        declare(
            SimpleNamespace(
                client=ui.client,
                headers=dict(ui.client.headers),
                path=ui.path(f"/v2/projects/{project.id}/dependencies"),
            ),
            detail,
        )
        detail = ui.client.get(
            ui.path(f"/v2/projects/{project.id}/artifacts/{detail['artifact']['id']}")
        ).json()
        response = ui.client.post(
            ui.path(f"/v2/projects/{project.id}/artifacts/{detail['artifact']['id']}/decisions"),
            json=target_command(detail, decision="approved"),
        )
        assert response.status_code == 200, response.text
        details.append(response.json())
    body = packet_body(details, title="Editable specification package")
    for component in body["components"]:
        component["role"] = "source"
    response = ui.client.post(packet_path(ui, project), json=body)
    assert response.status_code == 201, response.text
    return SimpleNamespace(project=project, details=details, packet=response.json(), source=item)


def acceptance_path(ui, h, suffix=""):
    return packet_path(ui, h.project, "/" + h.packet["packet"]["id"] + "/acceptance" + suffix)


def package_state(ui, h):
    response = ui.client.get(acceptance_path(ui, h))
    assert response.status_code == 200, response.text
    return response.json()


def open_acceptance(ui, h, *, page=None):
    page = page or ui.page
    open_delivery(ui, h.project, page=page)
    inspect(ui, h.packet, page=page)
    page.locator("#nd-acceptance").click()
    page.locator("#nv-profile-edit:not([disabled])").wait_for()


def fill_assignment(page, h, *, duplicate=False):
    page.locator("#nv-profile-edit").click()
    for index, role in enumerate(
        ("json_specification", "markdown_rationale", "schema_and_check_report")
    ):
        identifier = h.details[0 if duplicate and index == 2 else index]["artifact"][
            "current_revision_id"
        ]
        page.locator("#nv-role-" + role).select_option(identifier)
    page.locator("#nv-probe-path").fill("/unit_price")
    page.locator("#nv-probe-before").fill("20")
    page.locator("#nv-probe-after").fill("25")


def save_and_validate(ui, h):
    fill_assignment(ui.page, h)
    ui.page.locator("#nv-profile-save").click()
    ui.page.locator("#nv-validate:not([disabled])").wait_for()
    ui.page.locator("#nv-validate").click()
    ui.page.locator("#nv-validations").get_by_text("Validation passed", exact=True).wait_for()


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_package_profile_validation_acceptance_reopen_and_export(native_ui, tmp_path):
    ui = native_ui
    h = seed_package(ui)
    open_acceptance(ui, h)
    save_and_validate(ui, h)
    state = package_state(ui, h)
    assert state["can_accept"] and not state["applicability"]["accepted"]
    assert "Unrelated data preserved" in ui.page.locator("#nv-validations").inner_text()
    originals = [detail["artifact"] for detail in h.details]
    ui.page.locator("#nv-accept").click()
    malicious = '<img src=x onerror="window.packageInjected=true"> Owner scope reviewed.'
    ui.page.locator("#nv-feedback-notes").fill(malicious)
    ui.page.locator("#nv-decision-save").click()
    ui.page.locator("#nv-applicability").filter(has_text="acceptance currently applies").wait_for()
    assert ui.page.locator("#nv-accept").is_disabled()
    assert malicious in ui.page.locator("#nv-decisions").inner_text()
    assert ui.page.locator("#nv-dialog img, #nv-dialog script").count() == 0
    assert ui.page.evaluate("window.packageInjected === undefined")
    state = package_state(ui, h)
    assert (
        state["applicability"]["accepted"]
        and len(state["validations"]) == len(state["acceptances"]) == 1
    )
    for original in originals:
        current = ui.client.get(
            ui.path(f"/v2/projects/{h.project.id}/artifacts/{original['id']}")
        ).json()
        assert current["artifact"] == original
    ui.page.locator("#nv-close").click()
    open_acceptance(ui, h)
    assert ui.page.locator("#nv-validations [data-validation-id]").count() == 1
    assert ui.page.locator("#nv-decisions [data-package-acceptance-id]").count() == 1
    screenshots = Path(".local/native-acceptance-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.locator("#nv-heading").scroll_into_view_if_needed()
    ui.page.screenshot(path=str(screenshots / "acceptance-desktop.png"))
    ui.page.locator("#nv-close").click()
    open_delivery(ui, h.project)
    inspect(ui, h.packet)
    with zipfile.ZipFile(download_zip(ui, tmp_path, filename="accepted-package.zip")) as archive:
        assert any(name.startswith("package/validation/") for name in archive.namelist())
        assert any(name.startswith("package/acceptance/") for name in archive.namelist())
        status = json.loads(archive.read("package/current-status.json"))
        assert status["applicability"]["accepted"]


def test_duplicate_package_role_records_failed_validation_and_blocks_acceptance(native_ui):
    ui = native_ui
    h = seed_package(ui)
    open_acceptance(ui, h)
    fill_assignment(ui.page, h, duplicate=True)
    ui.page.locator("#nv-profile-save").click()
    ui.page.locator("#nv-validate:not([disabled])").wait_for()
    ui.page.locator("#nv-validate").click()
    ui.page.locator("#nv-validations").get_by_text("Validation failed", exact=True).wait_for()
    assert "required roles: failed" in ui.page.locator("#nv-validations").inner_text()
    assert ui.page.locator("#nv-accept").is_disabled()
    assert not package_state(ui, h)["applicability"]["accepted"]
    ui.page.locator("#nv-close").click()
    open_acceptance(ui, h)
    assert ui.page.locator("#nv-validations [data-validation-id]").count() == 1


def test_package_assignment_uncertain_response_recovers_exact_receipt(native_ui):
    ui = native_ui
    h = seed_package(ui)
    open_acceptance(ui, h)
    fill_assignment(ui.page, h)
    requests = []

    def lose_response(route):
        requests.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 201
        route.abort("failed")

    ui.page.route("**/acceptance/profile", lose_response)
    ui.page.locator("#nv-profile-save").click()
    ui.page.locator("#nv-uncertain").wait_for()
    assert ui.page.locator("#nv-probe-after").is_disabled()
    ui.page.locator("#nv-check").click()
    ui.page.locator("#nv-validate:not([disabled])").wait_for()
    assert len(requests) == 1 and len(package_state(ui, h)["assignments"]) == 1


def test_package_reader_and_source_revocation_withhold_saved_evidence(native_ui):
    ui = native_ui
    h = seed_package(ui, evidence=True)
    member = ui.user()
    share(ui, h.project, member)
    # Sharing changes project authority; capture a current packet for its original components.
    body = packet_body(h.details, title="Current readable package")
    for component in body["components"]:
        component["role"] = "source"
    response = ui.client.post(packet_path(ui, h.project), json=body)
    assert response.status_code == 201, response.text
    h.packet = response.json()
    open_acceptance(ui, h)
    save_and_validate(ui, h)
    open_delivery(ui, h.project, page=member.page)
    inspect(ui, h.packet, page=member.page)
    member.page.locator("#nd-acceptance").click()
    member.page.locator("#nv-validations [data-validation-id]").wait_for()
    assert member.page.locator("#nv-profile-edit").is_disabled()
    assert member.page.locator("#nv-validate").is_disabled()
    assert member.page.locator("#nv-accept").is_disabled()
    member.page.set_viewport_size({"width": 390, "height": 844})
    assert member.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshots = Path(".local/native-acceptance-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    member.page.screenshot(path=str(screenshots / "acceptance-mobile.png"))
    ui.container.store.revoke_native_intake_source(
        ui.actor.workspace_id, h.project.id, h.source.id, utc_now()
    )
    member.page.locator("#nv-refresh").click()
    member.page.locator("#nv-applicability").filter(has_text="no longer permits").wait_for()
    assert member.page.locator("#nv-validations [data-validation-id]").count() == 0
    assert member.page.locator("#nv-assignment").inner_text() == ""
    assert member.page.locator("#nv-profile-form").is_hidden()


def test_package_conflict_preserves_profile_draft_and_requires_current_assignment(native_ui):
    ui = native_ui
    h = seed_package(ui)
    open_acceptance(ui, h)
    fill_assignment(ui.page, h)
    body = {
        "expected_version": 0,
        "roles": [
            {"role": role, "revision_id": detail["artifact"]["current_revision_id"]}
            for role, detail in zip(
                ("json_specification", "markdown_rationale", "schema_and_check_report"),
                h.details,
                strict=True,
            )
        ],
        "probe": {
            "selector": {"kind": "json_pointer", "path": "/unit_price"},
            "before": 20,
            "after": 30,
        },
        "idempotency_key": str(uuid4()),
    }
    response = ui.client.put(acceptance_path(ui, h, "/profile"), json=body)
    assert response.status_code == 201, response.text
    ui.page.locator("#nv-profile-save").click()
    ui.page.locator("#nv-conflict").wait_for()
    assert ui.page.locator("#nv-probe-after").input_value() == "25"
    assert ui.page.locator("#nv-profile-save").is_disabled()
    ui.page.locator("#nv-reload").click()
    ui.page.locator("#nv-feedback").filter(has_text="assignment version 1").wait_for()
    assert ui.page.locator("#nv-probe-after").input_value() == "25"
    ui.page.locator("#nv-profile-save").click()
    ui.page.locator("#nv-validate:not([disabled])").wait_for()
    assert package_state(ui, h)["assignment"]["version"] == 2
