"""Administrator storage authority and retained usage through isolated real browsers."""

import base64
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.identity import Membership
from simon.domain.native_projects import CreateNativeTask, UpdateNativeProject
from simon.domain.native_storage import StorageCharge
from simon.services.intake_sources import IntakeSourceBytes
from tests.integration.test_native_artifacts_browser import artifact_path, upload_api
from tests.integration.test_native_intake_browser import open_intake
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def storage_path(ui, project, suffix=""):
    return ui.path(f"/v2/projects/{project.id}/storage" + suffix)


def storage_view(ui, project, *, client=None):
    response = (client or ui.client).get(storage_path(ui, project))
    assert response.status_code == 200, response.text
    return response.json()


def update_api(ui, project, **changes):
    saved = storage_view(ui, project)["entitlement"]
    response = ui.client.post(
        storage_path(ui, project, "/entitlement"),
        json={
            "expected_version": saved["version"],
            "plan_key": saved["plan_key"],
            "limit_bytes": saved["limit_bytes"],
            "override_limit_bytes": saved["override_limit_bytes"],
            "override_expires_at": saved["override_expires_at"],
            "reason": "Synthetic allowance adjustment.",
            "idempotency_key": str(uuid4()),
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def seed_usage(ui):
    projects = [seed_project(ui), seed_project(ui, "Restricted project content")]
    details = []
    for index, project in enumerate(projects):
        task = ui.service.create_task(
            ui.actor,
            project.id,
            CreateNativeTask(title="Prepare retained output", idempotency_key=str(uuid4())),
        )
        details.append(
            upload_api(ui, SimpleNamespace(project=project, task=task), b"A" * (100 * (index + 1)))
        )
    reservation = StorageCharge(
        workspace_id=ui.actor.workspace_id,
        project_id=projects[0].id,
        object_kind="source",
        object_id=uuid4(),
        bytes=50,
        sha256="a" * 64,
        request_fingerprint="b" * 64,
    )
    ui.container.store.reserve_source_storage(reservation)
    return SimpleNamespace(
        project=projects[0], other=projects[1], details=details, reservation=reservation
    )


def open_storage(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-storage").click()
    expect(page.locator("#ns-dialog")).to_be_visible()
    expect(page.locator("#ns-refresh")).to_be_enabled()


def edit_storage(ui, *, limit=1000, plan="synthetic_plan", reason="A bounded synthetic allowance."):
    ui.page.locator("#ns-edit").click()
    ui.page.locator("#ns-plan-key").fill(plan)
    ui.page.locator("#ns-limit").fill(str(limit))
    ui.page.locator("#ns-reason").fill(reason)


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_admin_shared_usage_override_downgrade_and_retained_reads(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_usage(ui)
    open_storage(ui, h.project)
    for key, value in {
        "used_bytes": 350,
        "retained_bytes": 300,
        "reserved_bytes": 50,
        "project_used_bytes": 150,
    }.items():
        expect(ui.page.locator(f'[data-storage-metric="{key}"]')).to_have_attribute(
            "data-bytes", str(value)
        )
    expect(ui.page.locator("#ns-reservations")).to_contain_text("1 pending upload reservation")
    expect(ui.page.locator("#ns-accounting")).to_contain_text("Logical retained content")
    assert h.other.name not in ui.page.locator("#ns-dialog").inner_text()
    assert str(h.other.id) not in ui.page.locator("#ns-dialog").inner_html()
    reason = '<img src=x onerror="window.storageInjected=true"> Test override.'
    edit_storage(ui, limit=400, reason=reason)
    ui.page.locator("#ns-override").fill("600")
    expiry = ui.page.evaluate(
        "() => { const d = new Date(Date.now() + 86400000); "
        "const p = n => String(n).padStart(2, '0'); return d.getFullYear() + '-' + "
        "p(d.getMonth()+1) + '-' + p(d.getDate()) + 'T' + "
        "p(d.getHours()) + ':' + p(d.getMinutes()); }"
    )
    ui.page.locator("#ns-expiry").fill(expiry)
    ui.page.locator("#ns-save").click()
    expect(ui.page.locator("#ns-form")).not_to_be_visible()
    expect(ui.page.locator('[data-storage-metric="effective_limit_bytes"]')).to_have_attribute(
        "data-bytes", "600"
    )
    expect(ui.page.locator("#ns-history")).to_contain_text(reason)
    assert ui.page.locator("#ns-history img").count() == 0
    assert ui.page.evaluate("window.storageInjected === undefined")
    saved = storage_view(ui, h.project)
    assert saved["entitlement"]["version"] == 1
    assert saved["entitlement"]["plan_key"] == "synthetic_plan"
    assert saved["entitlement"]["override_expires_at"]
    screenshots = Path(".local/native-storage-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.locator("#ns-heading").scroll_into_view_if_needed()
    ui.page.screenshot(path=str(screenshots / "storage-desktop.png"), animations="disabled")
    edit_storage(ui, limit=100, reason="Downgrade preserves retained files.")
    ui.page.locator("#ns-override").fill("")
    ui.page.locator("#ns-expiry").fill("")
    ui.page.locator("#ns-save").click()
    expect(ui.page.locator("#ns-overage")).to_be_visible()
    expect(ui.page.locator("#ns-overage")).to_contain_text("250 bytes")
    expect(ui.page.locator('[data-storage-metric="available_bytes"]')).to_have_attribute(
        "data-bytes", "0"
    )
    expect(ui.page.locator("#ns-history [data-entitlement-version]")).to_have_count(2)
    artifact = h.details[0]
    response = ui.client.get(
        artifact_path(
            ui,
            h.project,
            f"/{artifact['artifact']['id']}/revisions/{artifact['current_revision_id']}/content",
        )
    )
    assert response.status_code == 200 and response.content == b"A" * 100
    task = ui.service.create_task(
        ui.actor,
        h.project.id,
        CreateNativeTask(title="Quota blocked output", idempotency_key=str(uuid4())),
    )
    response = ui.client.post(
        artifact_path(ui, h.project, "/upload"),
        json={
            "task_id": str(task.id),
            "expected_version": 0,
            "expected_task_version": task.version,
            "filename": "blocked.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(b"B").decode(),
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "storage_quota_exceeded"


@pytest.mark.parametrize("role", ["owner", "member", "guest"])
def test_non_admins_view_shared_totals_without_mutation_or_other_project_data(native_ui, role):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_usage(ui)
    update_api(ui, h.project, reason="Private administrator notes.")
    viewer = ui.user(role=role)
    share(ui, h.project, viewer)
    viewer.page.set_viewport_size({"width": 390, "height": 844})
    open_storage(ui, h.project, page=viewer.page)
    expect(viewer.page.locator("#ns-edit")).not_to_be_visible()
    expect(viewer.page.locator("#ns-form")).not_to_be_visible()
    expect(viewer.page.locator("#ns-limit")).to_be_disabled()
    expect(viewer.page.locator('[data-storage-metric="used_bytes"]')).to_have_attribute(
        "data-bytes", "350"
    )
    assert h.other.name not in viewer.page.locator("#ns-dialog").inner_text()
    assert str(h.other.id) not in viewer.page.locator("#ns-dialog").inner_html()
    if role != "owner":
        assert "Private administrator notes." not in viewer.page.locator("#ns-dialog").inner_text()
        response = storage_view(ui, h.project, client=viewer.client)
        assert response["entitlement"]["reason"] == "" and response["history"] == []
    else:
        expect(viewer.page.locator("#ns-history")).to_contain_text("Private administrator notes.")
    assert viewer.page.locator("#ns-dialog").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )
    response = viewer.client.post(
        storage_path(ui, h.project, "/entitlement"),
        json={
            "expected_version": 1,
            "plan_key": "unauthorized",
            "limit_bytes": 1000,
            "reason": "Unauthorized change",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 403, response.text
    if role == "guest":
        screenshots = Path(".local/native-storage-browser")
        screenshots.mkdir(parents=True, exist_ok=True)
        viewer.page.screenshot(path=str(screenshots / "storage-mobile.png"), animations="disabled")


def test_saved_change_receipt_recovers_lost_response_without_another_write(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_storage(ui, project)
    edit_storage(ui)
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 200
        route.abort("failed")

    ui.page.route("**/storage/entitlement", lose_response)
    ui.page.locator("#ns-save").click()
    expect(ui.page.locator("#ns-uncertain")).to_be_visible()
    expect(ui.page.locator("#ns-limit")).to_be_disabled()
    expect(ui.page.locator("#ns-close")).to_be_disabled()
    expect(ui.page.locator("#ns-retry")).not_to_be_visible()
    ui.page.locator("#ns-check").click()
    expect(ui.page.locator("#ns-uncertain")).not_to_be_visible()
    expect(ui.page.locator("#ns-feedback")).to_contain_text("version 1 is recorded")
    assert len(attempts) == 1
    assert len(storage_view(ui, project)["history"]) == 1


def test_unsaved_change_retries_same_exact_command_and_key(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_storage(ui, project)
    edit_storage(ui)
    attempts = []

    def lose_first_request(route):
        attempts.append(route.request.post_data_json)
        route.abort("failed") if len(attempts) == 1 else route.continue_()

    ui.page.route("**/storage/entitlement", lose_first_request)
    ui.page.locator("#ns-save").click()
    expect(ui.page.locator("#ns-uncertain")).to_be_visible()
    ui.page.locator("#ns-check").click()
    expect(ui.page.locator("#ns-retry")).to_be_visible()
    ui.page.locator("#ns-retry").click()
    expect(ui.page.locator("#ns-form")).not_to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len(storage_view(ui, project)["history"]) == 1


def test_version_conflict_requires_comparison_and_preserves_admin_draft(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_storage(ui, project)
    edit_storage(ui, limit=700, reason="Draft reason retained.")
    update_api(ui, project, limit_bytes=600, plan_key="concurrent-plan")
    ui.page.locator("#ns-save").click()
    expect(ui.page.locator("#ns-conflict")).to_be_visible()
    expect(ui.page.locator("#ns-latest")).to_contain_text("concurrent-plan")
    expect(ui.page.locator("#ns-limit")).to_have_value("700")
    expect(ui.page.locator("#ns-reason")).to_have_value("Draft reason retained.")
    expect(ui.page.locator("#ns-save")).to_be_disabled()
    ui.page.locator("#ns-rebase").click()
    expect(ui.page.locator("#ns-conflict")).not_to_be_visible()
    ui.page.locator("#ns-save").click()
    expect(ui.page.locator("#ns-feedback")).to_contain_text("version 2 is recorded")
    assert storage_view(ui, project)["entitlement"]["limit_bytes"] == 700


def test_revoked_admin_membership_clears_edit_state_and_blocks_grant(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_storage(ui, project)
    edit_storage(ui, reason="Sensitive unsaved administrative reason.")
    ui.container.store.put_membership(
        Membership(actor_id=ui.actor.actor_id, workspace_id=ui.actor.workspace_id, role="member")
    )
    ui.page.locator("#ns-save").click()
    expect(ui.page.locator("#ns-dialog")).not_to_be_visible()
    expect(ui.page.locator("#ns-reason")).to_have_value("")
    expect(ui.page.locator("#ns-totals")).to_be_empty()
    assert ui.container.store.storage_entitlement(ui.actor.workspace_id) is None


def test_late_usage_response_cannot_repopulate_another_project(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    first, second = seed_project(ui), seed_project(ui, "Other project")
    open_project(ui, first)

    def switch_during_request(route):
        response = route.fetch()
        assert response.ok
        ui.page.evaluate(
            "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
            "{detail: {id}})); history.pushState({}, '', '?project=' + id); "
            "window.dispatchEvent(new PopStateEvent('popstate')); }",
            str(second.id),
        )
        route.fulfill(response=response)

    ui.page.route("**/storage", switch_during_request)
    ui.page.locator("#np-storage").click()
    expect(ui.page.locator("#np-name")).to_have_text(second.name)
    expect(ui.page.locator("#ns-dialog")).not_to_be_visible()
    expect(ui.page.locator("#ns-totals")).to_be_empty()
    expect(ui.page.locator("#ns-history")).to_be_empty()


def test_archived_overquota_pending_upload_cleanup_retries_exact_receipt(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_usage(ui)
    ui.container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    update_api(ui, h.project, limit_bytes=300)
    ui.service.update_project(
        ui.actor,
        h.project.id,
        UpdateNativeProject(
            name=h.project.name,
            objective=h.project.objective,
            status="archived",
            expected_version=h.project.version,
            idempotency_key=str(uuid4()),
        ),
    )
    open_intake(ui, h.project)
    expect(ui.page.locator("#ni-save")).not_to_be_visible()
    expect(ui.page.locator("#ni-pending-section")).to_be_visible()
    expect(ui.page.locator("#ni-pending")).to_contain_text("50 reserved bytes")
    ui.page.locator("#ni-pending").get_by_role("button", name="Review cleanup").click()
    ui.page.locator("#ni-reconcile-reason").fill(
        "Verified interrupted upload; release its held capacity."
    )
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            response = route.fetch()
            assert response.ok
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/intake/sources/*/reconcile", lose_response)
    ui.page.locator("#ni-reconcile-save").click()
    expect(ui.page.locator("#ni-uncertain")).to_be_visible()
    expect(ui.page.locator("#ni-reconcile-reason")).to_be_disabled()
    expect(ui.page.locator("#ni-reconcile-cancel")).to_be_disabled()
    ui.page.locator("#ni-refresh").click()
    expect(ui.page.locator("#ni-uncertain")).to_be_visible()
    ui.page.locator("#ni-retry").click()
    expect(ui.page.locator("#ni-uncertain")).not_to_be_visible()
    expect(ui.page.locator("#ni-pending-section")).not_to_be_visible()
    expect(ui.page.locator("#ni-reconcile-reason")).to_have_value("")
    expect(ui.page.locator("#ni-feedback")).to_contain_text("Released 50 bytes")
    assert attempts[0] == attempts[1] and len(attempts) == 2
    assert attempts[0]["expected_version"] == 0
    current = storage_view(ui, h.project)
    assert current["used_bytes"] == current["retained_bytes"] == 300
    assert current["reserved_bytes"] == current["overage_bytes"] == 0
    assert current["reservation_count"] == 0


def test_cleanup_permission_and_late_response_clear_private_pending_state(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_usage(ui)
    ui.container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    member = ui.user(role="member")
    share(ui, h.project, member)
    open_intake(ui, h.project, page=member.page)
    expect(member.page.locator("#ni-pending-section")).not_to_be_visible()
    response = member.client.get(ui.path(f"/v2/projects/{h.project.id}/intake"))
    assert response.status_code == 200 and response.json()["pending_uploads"] == []
    assert str(h.reservation.object_id) not in member.page.locator("#ni-dialog").inner_html()
    response = member.client.post(
        ui.path(f"/v2/projects/{h.project.id}/intake/sources/{h.reservation.object_id}/reconcile"),
        json={
            "expected_version": 0,
            "reason": "Unauthorized cleanup",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 403, response.text
    open_intake(ui, h.project)
    ui.page.locator("#ni-pending").get_by_role("button", name="Review cleanup").click()
    ui.page.locator("#ni-reconcile-reason").fill("Sensitive cleanup reason for this project.")

    def switch_during_cleanup(route):
        response = route.fetch()
        assert response.ok
        ui.page.evaluate(
            "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
            "{detail: {id}})); history.pushState({}, '', '?project=' + id); "
            "window.dispatchEvent(new PopStateEvent('popstate')); }",
            str(h.other.id),
        )
        route.fulfill(response=response)

    ui.page.route("**/intake/sources/*/reconcile", switch_during_cleanup)
    ui.page.locator("#ni-reconcile-save").click()
    expect(ui.page.locator("#np-name")).to_have_text(h.other.name)
    expect(ui.page.locator("#ni-dialog")).not_to_be_visible()
    expect(ui.page.locator("#ni-pending")).to_be_empty()
    expect(ui.page.locator("#ni-reconcile-reason")).to_have_value("")
    expect(ui.page.locator("#ni-reconcile-target")).to_be_empty()


def test_cleanup_version_conflict_preserves_reason_until_owner_resubmits(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_usage(ui)
    ui.container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    open_intake(ui, h.project)
    ui.page.locator("#ni-pending").get_by_role("button", name="Review cleanup").click()
    ui.page.locator("#ni-reconcile-reason").fill("Preserved cleanup reason.")
    response = ui.client.put(
        ui.path(f"/v2/projects/{h.project.id}/intake"),
        json={
            "expected_version": 0,
            "background": "Updated saved context.",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    ui.page.locator("#ni-reconcile-save").click()
    expect(ui.page.locator("#ni-feedback")).to_have_attribute("data-tone", "error")
    expect(ui.page.locator("#ni-reconcile-reason")).to_have_value("Preserved cleanup reason.")
    expect(ui.page.locator("#ni-access")).to_contain_text("version 1")
    assert storage_view(ui, h.project)["reserved_bytes"] == 50
    ui.page.locator("#ni-reconcile-save").click()
    expect(ui.page.locator("#ni-pending-section")).not_to_be_visible()
    assert storage_view(ui, h.project)["reserved_bytes"] == 0


def test_cleanup_checks_live_permission_and_clears_cached_reservation_metadata(native_ui, tmp_path):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_usage(ui)
    ui.container.native_intake.source_bytes = IntakeSourceBytes(tmp_path / "source-bytes")
    open_intake(ui, h.project)
    expect(ui.page.locator("#ni-pending")).to_contain_text(str(h.reservation.object_id))
    ui.page.locator("#ni-pending").get_by_role("button", name="Review cleanup").click()
    ui.page.locator("#ni-reconcile-reason").fill("Sensitive administrative cleanup reason.")
    ui.container.store.put_membership(
        Membership(
            actor_id=ui.actor.actor_id,
            workspace_id=ui.actor.workspace_id,
            role="guest",
        )
    )
    ui.page.locator("#ni-reconcile-save").click()
    expect(ui.page.locator("#ni-dialog")).not_to_be_visible()
    expect(ui.page.locator("#ni-reconcile-reason")).to_have_value("")
    expect(ui.page.locator("#ni-reconcile-target")).to_be_empty()
    expect(ui.page.locator("#ni-pending")).to_be_empty()
    assert ui.container.store.storage_usage(ui.actor.workspace_id)["reserved_bytes"] == 50
