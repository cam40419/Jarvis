"""Project models and budgets through real sessions and synthetic provider transport."""

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import utc_now
from simon.domain.native_models import ModelTemplate, ModelUsage
from simon.domain.native_projects import PutNativeProjectMember, VersionedNativeCommand
from simon.services.project_models import ScopedModelSecrets
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def view(ui, project, *, client=None):
    response = (client or ui.client).get(ui.path(f"/v2/projects/{project.id}/models"))
    assert response.status_code == 200, response.text
    return response.json()


def configure_catalog(ui, *, cloud=False, paid=False, respond=None):
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        result = (
            respond(request) if respond is not None else {"simon_model_probe": True, "version": 1}
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 101, "completion_tokens": 31},
            },
        )

    template = ModelTemplate(
        id="planning",
        name="Approved planning model",
        workspace_ids=(ui.actor.workspace_id,),
        endpoint=ModelEndpoint(
            id="planning",
            provider="openai_compatible",
            model="synthetic-test-model",
            base_url="https://approved.example/v1" if cloud else "http://127.0.0.1:19099/v1",
            local=not cloud,
            api_key_env="MODEL_PROJECT_KEY" if cloud else None,
            context_window_tokens=128000,
            max_output_tokens=8192,
            input_cost_per_million_usd=1 if paid else 0,
            output_cost_per_million_usd=2 if paid else 0,
        ),
        credential_required=cloud,
        data_policy="Synthetic test endpoint. No real provider calls.",
        license_note="Test fixture only.",
    )
    service = ui.container.project_models
    service.templates_factory = lambda workspace_id: (template,)
    cipher = getattr(ui, "model_test_cipher", None) or Fernet(Fernet.generate_key())
    ui.model_test_cipher = cipher
    service.secrets = ScopedModelSecrets(lambda: cipher)
    service.transport = httpx.MockTransport(transport)
    return calls


def policy(ui, project, *, workspace=False, **changes):
    saved = view(ui, project)["workspace_policy" if workspace else "project_policy"]
    body = {
        key: value
        for key, value in saved.items()
        if key not in {"workspace_id", "project_id", "version", "updated_at"}
    }
    response = ui.client.put(
        ui.path(
            f"/v2/projects/{project.id}/models/" + ("workspace-policy" if workspace else "policy")
        ),
        json={
            **body,
            **changes,
            "expected_version": saved["version"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def allow_paid(ui, project, *, cloud=False):
    ceiling = {
        "lifetime_limit_microusd": 1000000,
        "daily_limit_microusd": 1000000,
        "monthly_limit_microusd": 1000000,
        "per_operation_limit_microusd": 1000000,
    }
    policy(ui, project, workspace=True, **ceiling)
    policy(ui, project, allow_paid=True, allow_cloud=cloud, **ceiling)


def enroll(ui, project, *, cloud=False, label="Planning"):
    response = ui.client.post(
        ui.path(f"/v2/projects/{project.id}/models/connections"),
        json={
            "template_id": "planning",
            "label": label,
            "idempotency_key": str(uuid4()),
            **({"credential": "synthetic-browser-key-only"} if cloud else {}),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def qualify(ui, project, model):
    response = ui.client.post(
        ui.path(f"/v2/projects/{project.id}/models/connections/{model['id']}/probe"),
        json={"expected_version": model["version"], "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ready"], response.text
    return response.json()


def open_models(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-models").click()
    expect(page.locator("#nm-dialog")).to_be_visible()
    expect(page.locator("#nm-refresh")).to_be_enabled()


def row(page, model):
    return page.locator(f'[data-model-id="{model["id"]}"]')


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_project_key_enrollment_rotation_qualification_and_revocation(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    calls = configure_catalog(ui, cloud=True, paid=True)
    allow_paid(ui, project, cloud=True)
    open_models(ui, project)
    ui.page.locator("#nm-add").click()
    ui.page.locator("#nm-label").fill('<img src=x onerror="window.modelInjected=true">')
    secret = "synthetic-browser-private-key"
    ui.page.locator("#nm-key").fill(secret)
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-feedback")).to_have_text("Settings saved.")
    assert not calls
    expect(ui.page.locator("#nm-key")).to_have_value("")
    assert secret not in ui.page.content()
    assert ui.page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert ui.page.evaluate("window.modelInjected === undefined")
    assert ui.page.locator("#nm-models img").count() == 0
    data = view(ui, project)
    assert secret not in json.dumps(data)
    model = data["models"][0]
    assert model["has_credential"] and not model["ready"]
    row(ui.page, model).get_by_role("button", name="Qualify model").click()
    expect(ui.page.locator("#nm-probe-description")).to_contain_text("Maximum reservation")
    ui.page.locator("#nm-probe-consent").check()
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-feedback")).to_have_text("Model qualification passed.")
    assert len(calls) == 1
    expect(ui.page.locator("#nm-usage")).to_contain_text("qualification · settled")
    row(ui.page, model).get_by_role("button", name="Edit model").click()
    expect(ui.page.locator("#nm-key")).to_have_value("")
    ui.page.locator("#nm-key").fill("synthetic-rotated-project-key")
    ui.page.locator("#nm-save").click()
    expect(row(ui.page, model)).to_contain_text("revision 2")
    assert not view(ui, project)["models"][0]["ready"]
    row(ui.page, model).get_by_role("button", name="Edit model").click()
    ui.page.locator("#nm-enabled").uncheck()
    ui.page.locator("#nm-save").click()
    expect(row(ui.page, model)).to_contain_text("Disabled")
    assert len(calls) == 1


def test_qualified_free_local_default_and_project_workspace_limits(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    configure_catalog(ui)
    model = qualify(ui, project, enroll(ui, project))
    open_models(ui, project)
    expect(row(ui.page, model)).to_contain_text("Ready · Local")
    expect(ui.page.locator("#nm-totals")).to_contain_text("$0.00")
    assert view(ui, project)["routing"]["ready"]
    ui.page.locator("#nm-project-policy").click()
    ui.page.locator("#nm-planning").select_option(model["id"])
    ui.page.locator("#nm-review").select_option(model["id"])
    ui.page.locator("#nm-lifetime").fill("2")
    ui.page.locator("#nm-daily").fill("0.5")
    ui.page.locator("#nm-monthly").fill("1.5")
    ui.page.locator("#nm-operation").fill("0.25")
    ui.page.locator("#nm-concurrent").fill("3")
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-feedback")).to_have_text("Settings saved.")
    saved = view(ui, project)["project_policy"]
    assert saved["daily_limit_microusd"] == 500000
    assert saved["planning_model_id"] == model["id"]
    assert saved["review_model_id"] == model["id"]
    ui.page.locator("#nm-close").click()
    ui.page.locator("#np-intake").click()
    expect(ui.page.locator("#ni-model-status")).to_contain_text("Planning: Planning")
    expect(ui.page.locator("#ni-analyze")).to_be_enabled()
    assert ui.page.locator("#ni-model, #ni-cloud, #ni-budget").count() == 0
    ui.page.locator("#ni-background").fill("Draft remains open while setting limits.")
    ui.page.locator("#ni-models").click()
    expect(ui.page.locator("#nm-dialog")).to_be_visible()
    ui.page.locator("#nm-close").click()
    ui.page.locator("#np-intake").click()
    expect(ui.page.locator("#ni-background")).to_have_value(
        "Draft remains open while setting limits."
    )


def test_lost_key_save_recovers_receipt_without_resending_secret(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    configure_catalog(ui, cloud=True)
    open_models(ui, project)
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.ok
        route.abort("failed")

    ui.page.route("**/models/connections", lose_response)
    ui.page.locator("#nm-add").click()
    ui.page.locator("#nm-label").fill("Private cloud model")
    ui.page.locator("#nm-key").fill("synthetic-once-only-key")
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-uncertain")).to_be_visible()
    expect(ui.page.locator("#nm-key")).to_have_value("")
    expect(ui.page.locator("#nm-close")).to_be_hidden()
    ui.page.locator("#nm-check").click()
    expect(ui.page.locator("#nm-feedback")).to_contain_text("No key was sent again")
    assert len(attempts) == 1
    assert len(view(ui, project)["models"]) == 1


def test_undelivered_key_save_reentry_retries_same_command(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    configure_catalog(ui, cloud=True)
    open_models(ui, project)
    attempts = []

    def lose_request(route):
        attempts.append(route.request.post_data_json)
        route.abort("failed") if len(attempts) == 1 else route.continue_()

    ui.page.route("**/models/connections", lose_request)
    ui.page.locator("#nm-add").click()
    ui.page.locator("#nm-label").fill("Retry model")
    ui.page.locator("#nm-key").fill("synthetic-reentered-key")
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-uncertain")).to_be_visible()
    ui.page.locator("#nm-check").click()
    expect(ui.page.locator("#nm-retry-key")).to_be_visible()
    assert not view(ui, project)["models"]
    ui.page.locator("#nm-retry-key").fill("synthetic-reentered-key")
    ui.page.locator("#nm-retry").click()
    expect(ui.page.locator("#nm-feedback")).to_have_text("Settings saved.")
    assert attempts[0] == attempts[1]
    expect(ui.page.locator("#nm-retry-key")).to_have_value("")


def test_late_original_key_write_is_recovered_after_conflicting_reentry(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    configure_catalog(ui, cloud=True)
    open_models(ui, project)
    attempts = []
    checks = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            response = route.fetch()
            assert response.ok
            route.abort("failed")
        else:
            route.continue_()

    def old_status(route):
        checks.append(route.request.url)
        if len(checks) == 1:
            route.fulfill(status=404, json={"error": {"message": "No receipt yet."}})
        else:
            route.continue_()

    ui.page.route("**/models/connections", lose_response)
    ui.page.route("**/models/operations/*", old_status)
    ui.page.locator("#nm-add").click()
    ui.page.locator("#nm-label").fill("One saved model")
    ui.page.locator("#nm-key").fill("synthetic-original-key")
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-uncertain")).to_be_visible()
    ui.page.locator("#nm-check").click()
    expect(ui.page.locator("#nm-retry-key")).to_be_visible()
    ui.page.locator("#nm-retry-key").fill("synthetic-mistaken-reentry")
    ui.page.locator("#nm-retry").click()
    expect(ui.page.locator("#nm-feedback")).to_contain_text(
        "original model settings were already saved"
    )
    models = view(ui, project)["models"]
    assert len(models) == 1 and models[0]["credential_revision"] == 1
    assert attempts[0]["idempotency_key"] == attempts[1]["idempotency_key"]
    assert len(checks) == 2


def test_policy_conflict_preserves_draft_and_readers_cannot_manage(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    configure_catalog(ui)
    guest = ui.user(role="guest", name="Model reviewer")
    share(ui, project, guest)
    open_models(ui, project, page=guest.page)
    expect(guest.page.locator("#nm-add")).to_be_hidden()
    expect(guest.page.locator("#nm-project-policy")).to_be_hidden()
    expect(guest.page.locator("#nm-workspace-policy")).to_be_hidden()
    expect(guest.page.locator("#nm-access")).to_contain_text("Read only")
    expect(guest.page.locator("#nm-totals")).to_contain_text("Workspace usage is visible")
    open_models(ui, project)
    ui.page.locator("#nm-project-policy").click()
    ui.page.locator("#nm-lifetime").fill("3")
    policy(ui, project, lifetime_limit_microusd=2000000)
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-conflict")).to_be_visible()
    expect(ui.page.locator("#nm-lifetime")).to_have_value("3")
    expect(ui.page.locator("#nm-latest")).to_contain_text('"lifetime_limit_microusd": 2000000')
    ui.page.locator("#nm-rebase").click()
    ui.page.locator("#nm-save").click()
    expect(ui.page.locator("#nm-feedback")).to_have_text("Settings saved.")
    assert view(ui, project)["project_policy"]["lifetime_limit_microusd"] == 3000000
    latest = ui.service.get_project(ui.actor, project.id)
    ui.service.remove_member(
        ui.actor,
        project.id,
        guest.actor.actor_id,
        VersionedNativeCommand(expected_version=latest.version, idempotency_key=str(uuid4())),
    )
    guest.page.locator("#nm-refresh").click()
    expect(guest.page.locator("#nm-dialog")).not_to_be_visible()
    expect(guest.page.locator("#nm-models .ni-card")).to_have_count(0)


def test_unknown_usage_requires_evidence_and_keeps_mobile_layout(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    configure_catalog(ui)
    model = enroll(ui, project)
    start = utc_now() - timedelta(minutes=10)
    unknown = ModelUsage(
        workspace_id=ui.actor.workspace_id,
        project_id=project.id,
        operation_id=uuid4(),
        phase="generation",
        requested_by=ui.actor.actor_id,
        model_id=UUID(model["id"]),
        model_version=1,
        template_id="planning",
        model="synthetic-test-model",
        endpoint_fingerprint="a" * 64,
        reserved_microusd=500000,
        held_microusd=500000,
        status="unknown",
        started_at=start,
        deadline_at=start + timedelta(minutes=5),
        error_code="dispatch_interrupted",
    )
    ui.container.store.insert_model_usage(unknown)
    open_models(ui, project)
    usage_row = ui.page.locator(f'[data-usage-id="{unknown.id}"]')
    expect(usage_row).to_contain_text("Reserved $0.50")
    usage_row.get_by_role("button", name="Reconcile charge").click()
    ui.page.locator("#nm-charge").fill("0.123456")
    ui.page.locator("#nm-reason").fill("Verified the final provider charge.")
    ui.page.locator("#nm-evidence").fill("Synthetic provider record INV-2026-001 confirms usage.")
    ui.page.locator("#nm-save").click()
    expect(usage_row).to_contain_text("generation · reconciled")
    expect(usage_row).to_contain_text("Charged $0.123456 · Reserved $0.00")
    expect(usage_row).to_contain_text("INV-2026-001")
    ui.page.set_viewport_size({"width": 390, "height": 844})
    assert ui.page.locator("#nm-dialog").evaluate("node => node.scrollWidth <= node.clientWidth")
    screenshots = Path(".local/native-models-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.screenshot(path=str(screenshots / "models-mobile.png"), animations="disabled")


def test_project_owner_cannot_change_workspace_limits_and_owner_loss_clears_key(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    configure_catalog(ui, cloud=True)
    member = ui.user(role="member", name="Project administrator")
    ui.service.put_member(
        ui.actor,
        project.id,
        PutNativeProjectMember(
            actor_id=member.actor.actor_id,
            role="owner",
            expected_version=project.version,
            idempotency_key=str(uuid4()),
        ),
    )
    open_models(ui, project, page=member.page)
    expect(member.page.locator("#nm-project-policy")).to_be_visible()
    expect(member.page.locator("#nm-workspace-policy")).to_be_hidden()
    member.page.locator("#nm-add").click()
    member.page.locator("#nm-label").fill("Unsaved private model")
    member.page.locator("#nm-key").fill("synthetic-clear-on-owner-loss")
    latest = ui.service.get_project(ui.actor, project.id)
    ui.service.put_member(
        ui.actor,
        project.id,
        PutNativeProjectMember(
            actor_id=member.actor.actor_id,
            role="member",
            expected_version=latest.version,
            idempotency_key=str(uuid4()),
        ),
    )
    member.page.locator("#nm-save").click()
    expect(member.page.locator("#nm-key")).to_have_value("")
    expect(member.page.locator("#nm-access")).to_contain_text("Read only")
    expect(member.page.locator("#nm-connection-form")).to_be_hidden()
    assert not view(ui, project)["models"]


def test_usage_pagination_and_navigation_discard_stale_models_response(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    second = seed_project(ui, "Second model project")
    configure_catalog(ui)
    model = enroll(ui, project)
    start = utc_now() - timedelta(hours=1)
    for index in range(51):
        ui.container.store.insert_model_usage(
            ModelUsage(
                workspace_id=ui.actor.workspace_id,
                project_id=project.id,
                operation_id=uuid4(),
                phase="qualification",
                requested_by=ui.actor.actor_id,
                model_id=UUID(model["id"]),
                model_version=1,
                template_id="planning",
                model="synthetic-test-model",
                endpoint_fingerprint="b" * 64,
                reserved_microusd=0,
                held_microusd=0,
                status="settled",
                started_at=start + timedelta(seconds=index),
                deadline_at=start + timedelta(minutes=5),
            )
        )
    open_models(ui, project)
    expect(ui.page.locator("#nm-usage .ni-card")).to_have_count(50)
    ui.page.locator("#nm-more").click()
    expect(ui.page.locator("#nm-usage .ni-card")).to_have_count(51)
    expect(ui.page.locator("#nm-more")).to_be_hidden()
    ui.page.locator("#nm-close").click()

    def navigate_during_response(route):
        response = route.fetch()
        assert response.ok
        ui.page.evaluate(
            "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
            "{detail: {id}})); history.pushState({}, '', '?project=' + id); "
            "window.dispatchEvent(new PopStateEvent('popstate')); }",
            str(second.id),
        )
        route.fulfill(response=response)

    ui.page.route(f"**/v2/projects/{project.id}/models", navigate_during_response)
    ui.page.locator("#np-models").click()
    expect(ui.page.locator("#np-name")).to_have_text(second.name)
    expect(ui.page.locator("#nm-dialog")).not_to_be_visible()
    expect(ui.page.locator("#nm-models .ni-card")).to_have_count(0)
    ui.page.locator("#np-models").click()
    expect(ui.page.locator("#nm-models")).to_contain_text("No project models yet")
