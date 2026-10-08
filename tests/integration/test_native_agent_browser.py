"""Project agent roles and short-lived worker authority in the real browser."""

from pathlib import Path
from uuid import uuid4

import pytest

from simon.domain.native_agents import NativeAgent
from simon.domain.native_projects import UpdateNativeProject
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def seed_agent(ui, project):
    response = ui.client.post(
        ui.path(f"/v2/projects/{project.id}/agents"),
        json={
            "name": "Design lead",
            "role_key": "design-lead",
            "instructions": "Develop the collection direction and document design decisions.",
            "success_criteria": (
                "Every proposal includes references, alternatives, and review criteria."
            ),
            "rationale": "A dedicated design owner keeps the collection consistent.",
            "can_manage_team": True,
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def open_agents(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-agents").click()
    expect(page.locator("#nt-dialog")).to_be_visible()


def fill_agent(page):
    page.locator("#nt-new-agent").click()
    page.locator("#nt-name").fill("Production researcher")
    page.locator("#nt-role-key").fill("production-research")
    page.locator("#nt-instructions").fill("Compare suppliers and document evidence.")
    page.locator("#nt-success").fill("Show sourced unit costs, minimum quantities, and lead times.")
    page.locator("#nt-rationale").fill("Reliable production evidence is needed before a launch.")


def team(ui, project):
    response = ui.client.get(ui.path(f"/v2/projects/{project.id}/team"))
    assert response.status_code == 200, response.text
    return response.json()


def test_role_creation_lifecycle_policy_and_mobile_dialog(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_agents(ui, project)
    expect(ui.page.locator("#nt-dialog")).to_contain_text("does not start a worker")
    fill_agent(ui.page)
    ui.page.locator("#nt-save").click()
    expect(ui.page.locator("#nt-overview")).to_be_visible()
    expect(ui.page.locator("#nt-agents")).to_contain_text("Production researcher")
    agent = team(ui, project)["agents"][0]
    ui.page.locator("#nt-close").click()
    ui.page.locator("#np-new-task").click()
    ui.page.locator("#np-task-title").fill("Compare suppliers")
    ui.page.locator("#np-task-assignee").select_option("agent:" + agent["id"])
    ui.page.locator("#np-task-save").click()
    expect(ui.page.locator("#np-task-dialog")).not_to_be_visible()
    task = ui.service.tasks(ui.actor, project.id)[0]
    assert str(task.assignment.agent_id) == agent["id"]
    ui.page.locator(f'[data-task-id="{task.id}"] .np-task-open').click()
    ui.page.locator("#np-task-status").select_option("in_progress")
    ui.page.locator("#np-task-save").click()
    expect(ui.page.locator("#np-task-dialog")).not_to_be_visible()
    ui.page.locator("#np-agents").click()
    for status in ("paused", "retired"):
        ui.page.get_by_role("button", name="Edit Production researcher", exact=True).click()
        expect(ui.page.locator("#nt-role-key")).to_be_disabled()
        ui.page.locator("#nt-state").select_option(status)
        ui.page.locator("#nt-save").click()
        expect(ui.page.locator("#nt-overview")).to_be_visible()
        assert team(ui, project)["agents"][0]["status"] == status
        released = ui.service.get_task(ui.actor, project.id, task.id)
        assert released.assignment.kind == "pool"
        assert released.status == "todo"
    ui.page.locator("#nt-policy-open").click()
    ui.page.locator("#nt-max-agents").fill("2")
    ui.page.locator("#nt-agents-manage").uncheck()
    ui.page.locator("#nt-save").click()
    expect(ui.page.locator("#nt-summary")).to_contain_text("0 of 2 active roles")
    assert team(ui, project)["policy"]["agents_can_manage_team"] is False
    ui.page.reload()
    ui.page.locator("#np-agents").click()
    expect(ui.page.locator(f'[data-agent-id="{agent["id"]}"]')).to_contain_text("Retired")
    screenshots = Path(".local/native-agents-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.screenshot(path=str(screenshots / "agent-roles-desktop.png"), animations="disabled")
    ui.page.set_viewport_size({"width": 390, "height": 844})
    fill_agent(ui.page)
    assert ui.page.locator("#nt-dialog").evaluate("node => node.scrollWidth <= node.clientWidth")
    ui.page.screenshot(path=str(screenshots / "agent-role-mobile.png"), animations="disabled")
    ui.page.locator("#nt-cancel").click()
    ui.page.locator("#nt-close").click()


def test_stale_agent_edit_preserves_draft_until_explicit_rebase(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    agent = seed_agent(ui, project)
    open_agents(ui, project)
    ui.page.get_by_role("button", name="Edit Design lead", exact=True).click()
    ui.page.locator("#nt-instructions").fill("My reviewed direction for the next collection.")
    fields = {
        key: agent[key]
        for key in (
            "name",
            "instructions",
            "success_criteria",
            "rationale",
            "can_manage_team",
            "status",
        )
    }
    updated = ui.client.put(
        ui.path(f"/v2/projects/{project.id}/agents/{agent['id']}"),
        json=fields
        | {
            "rationale": "Updated by another owner",
            "expected_version": agent["version"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert updated.status_code == 200
    ui.page.locator("#nt-save").click()
    expect(ui.page.locator("#nt-conflict")).to_be_visible()
    expect(ui.page.locator("#nt-latest")).to_contain_text("Updated by another owner")
    expect(ui.page.locator("#nt-instructions")).to_have_value(
        "My reviewed direction for the next collection."
    )
    expect(ui.page.locator("#nt-save")).to_be_disabled()
    ui.page.locator("#nt-rebase").click()
    ui.page.locator("#nt-save").click()
    expect(ui.page.locator("#nt-overview")).to_be_visible()
    assert (
        team(ui, project)["agents"][0]["instructions"]
        == "My reviewed direction for the next collection."
    )


def test_unknown_create_retries_identical_payload_without_duplicate_role(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_agents(ui, project)
    fill_agent(ui.page)
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            response = route.fetch()
            assert response.status == 201
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/v2/projects/*/agents", lose_response)
    ui.page.locator("#nt-save").click()
    expect(ui.page.locator("#nt-feedback")).to_contain_text("could not be confirmed")
    expect(ui.page.locator("#nt-name")).to_be_disabled()
    expect(ui.page.locator("#nt-cancel")).to_be_disabled()
    ui.page.locator("#nt-save").click()
    expect(ui.page.locator("#nt-overview")).to_be_visible()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len(team(ui, project)["agents"]) == 1


def test_worker_credentials_are_scoped_visible_once_and_revocable(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    agent = seed_agent(ui, project)
    open_agents(ui, project)
    ui.page.get_by_role("button", name="Worker access for Design lead", exact=True).click()
    expect(ui.page.locator("#nt-credentials-dialog")).to_be_visible()
    ui.page.locator("#nt-credential-minutes").fill("1")
    ui.page.locator("#nt-credential-write").uncheck()
    ui.page.locator("#nt-credential-issue").click()
    expect(ui.page.locator("#nt-credential-secret")).to_be_visible()
    secret = ui.page.locator("#nt-secret-value").input_value()
    assert secret
    path = ui.path(f"/v2/projects/{project.id}/agents/{agent['id']}/credentials")
    credentials = ui.client.get(path).json()
    assert credentials[0]["scopes"] == ["board:read"]
    assert "token" not in credentials[0] and secret not in str(credentials)
    assert ui.page.evaluate("localStorage.length") == 0
    ui.page.locator("#nt-credentials-close").click()
    expect(ui.page.locator("#nt-secret-value")).to_have_value("")
    ui.page.get_by_role("button", name="Worker access for Design lead", exact=True).click()
    expect(ui.page.locator("#nt-credential-secret")).not_to_be_visible()
    ui.page.locator("#nt-credentials-list").get_by_role("button", name="Revoke", exact=True).click()
    expect(ui.page.locator("#nt-credentials-list")).to_contain_text("Revoked credential")
    assert ui.client.get(path).json()[0]["valid"] is False
    ui.page.locator("#nt-credential-issue").click()
    expect(ui.page.locator("#nt-credential-secret")).to_be_visible()
    ui.page.locator("#nt-credentials-close").click()
    ui.page.locator("#nt-close").click()
    project = ui.service.get_project(ui.actor, project.id)
    ui.service.update_project(
        ui.actor,
        project.id,
        UpdateNativeProject(
            name=project.name,
            objective=project.objective,
            status="archived",
            expected_version=project.version,
            idempotency_key=str(uuid4()),
        ),
    )
    open_agents(ui, project)
    ui.page.get_by_role("button", name="Worker access for Design lead", exact=True).click()
    expect(ui.page.locator("#nt-credentials-form")).not_to_be_visible()
    ui.page.locator("#nt-credentials-list").get_by_role("button", name="Revoke", exact=True).click()
    expect(
        ui.page.locator("#nt-credentials-list").get_by_role("button", name="Revoke")
    ).to_have_count(0)
    assert all(credential["revoked_at"] for credential in ui.client.get(path).json())


def test_lost_worker_credential_response_does_not_pretend_token_is_recoverable(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    agent = seed_agent(ui, project)
    open_agents(ui, project)
    ui.page.get_by_role("button", name="Worker access for Design lead", exact=True).click()
    attempts = []

    def lose_response(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            response = route.fetch()
            assert response.ok
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/v2/projects/*/agents/*/credentials", lose_response)
    ui.page.locator("#nt-credential-issue").click()
    expect(ui.page.locator("#nt-credential-retry")).to_be_visible()
    expect(ui.page.locator("#nt-credentials-close")).to_be_disabled()
    ui.page.locator("#nt-credential-retry").click()
    expect(ui.page.locator("#nt-credentials-feedback")).to_contain_text("cannot be shown again")
    expect(ui.page.locator("#nt-credential-secret")).not_to_be_visible()
    assert attempts[0] == attempts[1]
    path = ui.path(f"/v2/projects/{project.id}/agents/{agent['id']}/credentials")
    assert len(ui.client.get(path).json()) == 1


def test_read_only_member_sees_roles_beyond_first_page_without_staffing_controls(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    guest = ui.user(role="guest", name="Reviewer")
    share(ui, project, guest)
    for index in range(101):
        ui.container.store.insert_native_agent(
            NativeAgent(
                workspace_id=project.workspace_id,
                project_id=project.id,
                created_by=ui.actor.actor_id,
                name=f"Archived role {index:03}",
                role_key=f"role-{index}",
                instructions="Reference work",
                success_criteria="Reviewed",
                rationale="Historical role",
                status="retired",
            )
        )
    open_agents(ui, project, page=guest.page)
    expect(guest.page.locator("#nt-agents .nt-agent")).to_have_count(101)
    expect(guest.page.locator("#nt-new-agent")).not_to_be_visible()
    expect(guest.page.locator("#nt-policy-open")).not_to_be_visible()
    expect(guest.page.locator("#nt-agents button")).to_have_count(0)
