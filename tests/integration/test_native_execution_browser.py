"""Durable project execution through real sessions, workers and synthetic models."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from simon.domain.native_agents import CreateNativeAgent
from simon.domain.native_projects import CreateNativeTask, TaskAssignment, VersionedNativeCommand
from tests.integration.test_native_models_browser import configure_catalog, enroll, qualify
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import open_project, seed_project, share

pytestmark = pytest.mark.browser


def execution_view(ui, project, *, client=None):
    response = (client or ui.client).get(ui.path(f"/v2/projects/{project.id}/execution"))
    assert response.status_code == 200, response.text
    return response.json()


def execution_policy(ui, project, **changes):
    current = execution_view(ui, project)["policy"]
    body = {
        key: value
        for key, value in current.items()
        if key not in {"workspace_id", "project_id", "issued_by", "version", "updated_at"}
    }
    response = ui.client.put(
        ui.path(f"/v2/projects/{project.id}/execution/policy"),
        json={
            **body,
            **changes,
            "expected_version": current["version"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def seed_execution(ui, *, response=None, enabled=True):
    state = SimpleNamespace(actions=[], calls=[])

    def model_reply(request):
        body = request.content.decode()
        if "simon_model_probe" in body:
            return {"simon_model_probe": True, "version": 1}
        state.calls.append(json.loads(request.content))
        return (
            response(state)
            if response
            else {
                "kind": "draft",
                "summary": "Prepared a candidate for owner review.",
                "output": "Candidate launch outline. Review its evidence before acceptance.",
            }
        )

    project = seed_project(ui)
    configure_catalog(ui, respond=model_reply)
    qualify(ui, project, enroll(ui, project))
    agent = ui.container.native_teams.create(
        ui.actor,
        project.id,
        CreateNativeAgent(
            name="Project researcher",
            role_key="research",
            instructions="Prepare source-backed candidate documents for review.",
            success_criteria="Clear assumptions and supporting evidence.",
            rationale="The project needs research and drafting.",
            idempotency_key=str(uuid4()),
        ),
    )
    task = ui.service.create_task(
        ui.actor,
        project.id,
        CreateNativeTask(
            title="Prepare a launch outline",
            description="Draft the first launch milestone for review. Do not publish it.",
            assignment=TaskAssignment(kind="agent", agent_id=agent.id),
            idempotency_key=str(uuid4()),
        ),
    )
    if enabled:
        execution_policy(ui, project, enabled=True)
    return SimpleNamespace(project=project, task=task, agent=agent, state=state)


def open_execution(ui, project, *, page=None):
    from playwright.sync_api import expect

    page = page or ui.page
    open_project(ui, project, page=page)
    page.locator("#np-execution").click()
    expect(page.locator("#ne-dialog")).to_be_visible()
    expect(page.locator("#ne-refresh")).to_be_enabled()


def queue_task(ui, h):
    from playwright.sync_api import expect

    ui.page.locator("#ne-task").select_option(str(h.task.id))
    ui.page.locator("#ne-start").click()
    expect(ui.page.locator("#ne-agent")).to_have_value(str(h.agent.id))
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-run-detail")).to_be_visible()
    expect(ui.page.locator("#ne-run-summary")).to_contain_text("queued")
    runs = execution_view(ui, h.project)["runs"]["items"]
    assert len(runs) == 1
    return runs[0]


def enroll_runner(ui, project):
    response = ui.client.post(
        ui.path(f"/v2/projects/{project.id}/execution/runners"),
        json={"name": "Synthetic browser runner", "idempotency_key": str(uuid4())},
    )
    assert response.status_code in {200, 201}, response.text
    assert response.json()["token"]
    return response.json()


def advance(ui, credential, *, lease=None):
    with httpx.Client(
        base_url=ui.origin,
        trust_env=False,
        timeout=30,
        headers={"Authorization": "Bearer " + credential["token"]},
    ) as worker:
        if lease is None:
            claimed = worker.post(
                ui.path("/v2/execution-worker/claim"), json={"idempotency_key": str(uuid4())}
            )
            assert claimed.status_code == 200, claimed.text
            lease = claimed.json()
            assert lease is not None
        result = worker.post(
            ui.path("/v2/execution-worker/step"),
            json={
                "run_id": lease["run_id"],
                "fence": lease["fence"],
                "lease_token": lease["lease_token"],
                "idempotency_key": str(uuid4()),
            },
        )
        assert result.status_code == 200, result.text
        return result.json(), lease


@pytest.mark.parametrize("native_ui", ["", "/simon"], indirect=True)
def test_queue_execute_candidate_and_board_review_status(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    candidate = '<img src=x onerror="window.executionInjected=true"> Candidate draft.'
    h = seed_execution(
        ui,
        response=lambda _: {"kind": "draft", "summary": "Draft ready.", "output": candidate},
        enabled=False,
    )
    open_execution(ui, h.project)
    expect(ui.page.locator("#ne-start")).to_be_disabled()
    ui.page.locator("#ne-policy-edit").click()
    ui.page.locator("#ne-enabled").check()
    ui.page.locator("#ne-steps").fill("5")
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-policy-summary")).to_contain_text("Execution enabled")
    run = queue_task(ui, h)
    assert not h.state.calls
    credential = enroll_runner(ui, h.project)
    result, _ = advance(ui, credential)
    assert result["id"] == run["id"] and result["status"] == "completed"
    ui.page.locator("#ne-refresh").click()
    expect(ui.page.locator("#ne-run-summary")).to_contain_text("Candidate ready")
    expect(ui.page.locator("#ne-result")).to_have_text(candidate)
    expect(ui.page.locator("#ne-result-section")).to_contain_text("has not been accepted")
    expect(ui.page.locator("#ne-checkpoints")).to_contain_text("completed")
    assert ui.page.locator("#ne-result img").count() == 0
    assert ui.page.evaluate("window.executionInjected === undefined")
    assert len(h.state.calls) == 1
    ui.page.locator("#ne-close").click()
    expect(ui.page.locator('[data-status="in_review"]')).to_contain_text(h.task.title)
    assert ui.service.get_task(ui.actor, h.project.id, h.task.id).status == "in_review"


def test_human_question_releases_worker_and_answer_resumes(native_ui):
    from playwright.sync_api import expect

    ui = native_ui

    def reply(state):
        if len(state.calls) == 1:
            return {
                "kind": "question",
                "summary": "Direction is missing.",
                "question": "Which launch audience should this draft address?",
            }
        return {
            "kind": "draft",
            "summary": "Applied the owner's answer.",
            "output": "A candidate launch outline for existing subscribers.",
        }

    h = seed_execution(ui, response=reply)
    open_execution(ui, h.project)
    queue_task(ui, h)
    credential = enroll_runner(ui, h.project)
    first, _ = advance(ui, credential)
    assert first["status"] == "waiting"
    ui.page.locator("#ne-refresh").click()
    expect(ui.page.locator("#ne-run-waits")).to_contain_text("Which launch audience")
    ui.page.locator("#ne-run-answer").click()
    ui.page.locator("#ne-answer").fill("Address existing subscribers first.")
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-run-summary")).to_contain_text("queued")
    final, _ = advance(ui, credential)
    assert final["status"] == "completed"
    ui.page.locator("#ne-refresh").click()
    expect(ui.page.locator("#ne-result")).to_contain_text("existing subscribers")
    expect(ui.page.locator("#ne-run-waits")).to_contain_text("Address existing subscribers first.")
    assert len(h.state.calls) == 2


def test_lost_admission_receipt_recovers_one_run_and_cancel(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_execution(ui)
    open_execution(ui, h.project)
    attempts = []

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 202
        route.abort("failed")

    ui.page.route(f"**/execution/tasks/{h.task.id}/runs", lose_response)
    ui.page.locator("#ne-start").click()
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-uncertain")).to_be_visible()
    expect(ui.page.locator("#ne-close")).to_be_disabled()
    expect(ui.page.locator("#ne-save")).to_be_disabled()
    ui.page.keyboard.press("Escape")
    expect(ui.page.locator("#ne-dialog")).to_be_visible()
    ui.page.locator("#ne-check").click()
    expect(ui.page.locator("#ne-uncertain")).to_be_hidden()
    expect(ui.page.locator("#ne-run-summary")).to_contain_text("queued")
    assert len(attempts) == 1
    assert len(execution_view(ui, h.project)["runs"]["items"]) == 1
    assert not h.state.calls
    ui.page.locator("#ne-run-cancel").click()
    expect(ui.page.locator("#ne-run-summary")).to_contain_text("cancelled")
    expect(ui.page.locator("#ne-run-cancel")).to_be_hidden()
    expect(ui.page.locator("#ne-run-retry")).to_be_hidden()


def test_dependencies_and_schedule_configuration_preserve_timezone(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_execution(ui)
    prerequisite = ui.service.create_task(
        ui.actor,
        h.project.id,
        CreateNativeTask(title="Collect approved source material", idempotency_key=str(uuid4())),
    )
    open_execution(ui, h.project)
    ui.page.locator("#ne-task").select_option(str(h.task.id))
    ui.page.locator("#ne-workflow").click()
    ui.page.locator(f'#ne-dependencies input[value="{prerequisite.id}"]').check()
    ui.page.locator("#ne-not-before").fill("2030-03-10T12:30")
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-feedback")).to_contain_text("Saved")
    workflow = ui.client.get(
        ui.path(f"/v2/projects/{h.project.id}/execution/tasks/{h.task.id}/workflow")
    ).json()["config"]
    assert workflow["dependency_ids"] == [str(prerequisite.id)]
    assert workflow["not_before"]
    ui.page.locator("#ne-schedule-new").click()
    ui.page.locator("#ne-schedule-at").fill("2030-03-11T09:00")
    ui.page.locator("#ne-schedule-zone").fill("America/New_York")
    ui.page.locator("#ne-schedule-interval").fill("86400")
    ui.page.locator("#ne-schedule-count").fill("3")
    expect(ui.page.locator("#ne-schedule-form")).to_contain_text("daylight-saving")
    expected_instant = ui.page.evaluate("new Date('2030-03-11T09:00').toISOString()")
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-schedules")).to_contain_text("America/New_York")
    schedule = execution_view(ui, h.project)["schedules"]["items"][0]
    assert schedule["next_run_at"].replace("+00:00", "Z")[:19] == expected_instant[:19]
    assert schedule["timezone"] == "America/New_York"
    assert schedule["interval_seconds"] == 86400
    ui.page.locator("#ne-schedules").get_by_role("button", name="Edit schedule").click()
    ui.page.locator("#ne-schedule-enabled").uncheck()
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-schedules")).to_contain_text("Disabled")
    assert not execution_view(ui, h.project)["schedules"]["items"][0]["enabled"]


def test_runner_credential_once_lost_enrollment_and_revocation(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_execution(ui, project)
    ui.page.locator("#ne-runner-new").click()
    ui.page.locator("#ne-runner-name").fill("My optional local runner")
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-secret")).to_be_visible()
    token = ui.page.locator("#ne-secret-value").input_value()
    assert token
    assert token not in json.dumps(execution_view(ui, project))
    assert ui.page.evaluate("localStorage.length + sessionStorage.length") == 0
    ui.page.locator("#ne-close").click()
    expect(ui.page.locator("#ne-secret-value")).to_have_value("")
    open_execution(ui, project)
    expect(ui.page.locator("#ne-secret")).to_be_hidden()
    attempts = []

    def lose_enrollment(route):
        attempts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.ok
        route.abort("failed")

    ui.page.route("**/execution/runners", lose_enrollment)
    ui.page.locator("#ne-runner-new").click()
    ui.page.locator("#ne-runner-name").fill("Lost response runner")
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-uncertain")).to_be_visible()
    ui.page.locator("#ne-check").click()
    expect(ui.page.locator("#ne-feedback")).to_contain_text(
        "one-time credential is no longer available"
    )
    expect(ui.page.locator("#ne-secret")).to_be_hidden()
    assert len(attempts) == 1
    runners = execution_view(ui, project)["runners"]
    assert len(runners) == 2
    lost = next(runner for runner in runners if runner["name"] == "Lost response runner")
    ui.page.locator(f'[data-runner-id="{lost["id"]}"]').get_by_role(
        "button", name="Revoke runner"
    ).click()
    expect(ui.page.locator(f'[data-runner-id="{lost["id"]}"]')).to_contain_text("revoked")


def test_conflict_read_only_access_loss_and_mobile_layout(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_execution(ui)
    guest = ui.user(role="guest", name="Execution observer")
    share(ui, h.project, guest)
    open_execution(ui, h.project, page=guest.page)
    expect(guest.page.locator("#ne-policy-edit")).to_be_disabled()
    expect(guest.page.locator("#ne-start")).to_be_disabled()
    expect(guest.page.locator("#ne-runner-new")).to_be_disabled()
    open_execution(ui, h.project)
    ui.page.locator("#ne-policy-edit").click()
    ui.page.locator("#ne-steps").fill("6")
    execution_policy(ui, h.project, max_steps=4)
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-conflict")).to_be_visible()
    expect(ui.page.locator("#ne-steps")).to_have_value("6")
    expect(ui.page.locator("#ne-latest")).to_contain_text('"max_steps": 4')
    ui.page.locator("#ne-rebase").click()
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-policy-summary")).to_contain_text("6 steps")
    current = ui.service.get_project(ui.actor, h.project.id)
    ui.service.remove_member(
        ui.actor,
        h.project.id,
        guest.actor.actor_id,
        VersionedNativeCommand(expected_version=current.version, idempotency_key=str(uuid4())),
    )
    guest.page.locator("#ne-refresh").click()
    expect(guest.page.locator("#ne-dialog")).not_to_be_visible()
    expect(guest.page.locator("#ne-policy-summary")).to_have_text("")
    screenshots = Path(".local/native-execution-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    ui.page.screenshot(path=str(screenshots / "execution-desktop.png"), animations="disabled")
    ui.page.set_viewport_size({"width": 390, "height": 844})
    assert ui.page.locator("#ne-dialog").evaluate("node => node.scrollWidth <= node.clientWidth")
    ui.page.screenshot(path=str(screenshots / "execution-mobile.png"), animations="disabled")


def test_navigation_discards_delayed_execution_response(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    original = seed_project(ui)
    destination = seed_project(ui, "Another execution project")
    open_project(ui, original)

    def navigate_during_response(route):
        response = route.fetch()
        assert response.ok
        ui.page.evaluate(
            "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
            "{detail: {id}})); history.pushState({}, '', '?project=' + id); "
            "window.dispatchEvent(new PopStateEvent('popstate')); }",
            str(destination.id),
        )
        route.fulfill(response=response)

    ui.page.route(f"**/v2/projects/{original.id}/execution", navigate_during_response)
    ui.page.locator("#np-execution").click()
    expect(ui.page.locator("#np-name")).to_have_text(destination.name)
    expect(ui.page.locator("#ne-dialog")).not_to_be_visible()
    expect(ui.page.locator("#ne-policy-summary")).to_have_text("")
    ui.page.locator("#np-execution").click()
    expect(ui.page.locator("#ne-project")).to_have_text(destination.name)


def test_known_failed_step_retry_keeps_run_and_checkpoint_history(native_ui):
    from playwright.sync_api import expect

    ui = native_ui

    def reply(state):
        if len(state.calls) == 1:
            return {"kind": "draft", "summary": "A malformed candidate without required output."}
        return {
            "kind": "draft",
            "summary": "Repaired the candidate.",
            "output": "A complete draft.",
        }

    h = seed_execution(ui, response=reply)
    open_execution(ui, h.project)
    original = queue_task(ui, h)
    credential = enroll_runner(ui, h.project)
    failed, _ = advance(ui, credential)
    assert failed["status"] == "failed"
    ui.page.locator("#ne-refresh").click()
    expect(ui.page.locator("#ne-run-summary")).to_contain_text("failed")
    ui.page.locator("#ne-run-retry").click()
    expect(ui.page.locator("#ne-run-summary")).to_contain_text("queued")
    completed, _ = advance(ui, credential)
    assert completed["status"] == "completed" and completed["id"] == original["id"]
    ui.page.locator("#ne-refresh").click()
    expect(ui.page.locator("#ne-checkpoints")).to_contain_text("failed")
    expect(ui.page.locator("#ne-checkpoints")).to_contain_text("completed")
    assert len(h.state.calls) == 2
    assert len(execution_view(ui, h.project)["runs"]["items"]) == 1


def test_unknown_policy_retry_keeps_exact_command(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    project = seed_project(ui)
    open_execution(ui, project)
    attempts = []

    def intercept(route):
        attempts.append(route.request.post_data_json)
        if len(attempts) == 1:
            route.abort("failed")
        else:
            route.continue_()

    ui.page.route("**/execution/policy", intercept)
    ui.page.locator("#ne-policy-edit").click()
    ui.page.locator("#ne-steps").fill("7")
    ui.page.locator("#ne-save").click()
    expect(ui.page.locator("#ne-uncertain")).to_be_visible()
    expect(ui.page.locator("#ne-steps")).to_be_disabled()
    ui.page.locator("#ne-check").click()
    expect(ui.page.locator("#ne-feedback")).to_contain_text("No saved receipt was found")
    ui.page.locator("#ne-retry").click()
    expect(ui.page.locator("#ne-policy-summary")).to_contain_text("7 steps")
    assert attempts[0] == attempts[1]
    assert execution_view(ui, project)["policy"]["version"] == 1


def test_run_history_pagination_uses_real_persisted_runs(native_ui):
    from playwright.sync_api import expect

    ui = native_ui
    h = seed_execution(ui)
    base = ui.path(f"/v2/projects/{h.project.id}/execution")
    for index in range(51):
        task = ui.service.create_task(
            ui.actor,
            h.project.id,
            CreateNativeTask(
                title=f"Historical candidate {index}",
                assignment=TaskAssignment(kind="agent", agent_id=h.agent.id),
                idempotency_key=str(uuid4()),
            ),
        )
        response = ui.client.post(
            base + f"/tasks/{task.id}/runs",
            json={"expected_task_version": task.version, "idempotency_key": str(uuid4())},
        )
        assert response.status_code == 202, response.text
        run = response.json()
        response = ui.client.post(
            base + f"/runs/{run['id']}/cancel",
            json={"expected_version": run["version"], "idempotency_key": str(uuid4())},
        )
        assert response.status_code == 200, response.text
    open_execution(ui, h.project)
    expect(ui.page.locator("#ne-runs .ni-card")).to_have_count(50)
    ui.page.locator("#ne-runs-more").click()
    expect(ui.page.locator("#ne-runs .ni-card")).to_have_count(51)
    expect(ui.page.locator("#ne-runs-more")).to_be_hidden()
    assert not h.state.calls
