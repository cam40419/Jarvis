"""Typed evidence and exact selective repair in isolated authenticated browsers."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.api.test_native_artifacts_api import target_command
from tests.api.test_native_dependencies_api import (
    artifact_head,
    declare,
    edge,
    fact_head,
    output,
    save_fact,
    seed_graph,
    seed_source,
)
from tests.integration.test_native_projects_browser import native_ui as native_ui
from tests.integration.test_native_projects_browser import seed_project, share

pytestmark = pytest.mark.browser


@pytest.fixture
def dependencies_ui(native_ui):
    ui = native_ui
    project = seed_project(ui)
    h = SimpleNamespace(
        ui=ui,
        client=ui.client,
        container=ui.container,
        headers=dict(ui.client.headers),
        project=project.model_dump(mode="json"),
        project_record=project,
        project_path=ui.path("/v2/projects/" + str(project.id)),
    )
    h.path = h.project_path + "/dependencies"
    response = ui.client.put(
        h.project_path + "/intake", json={"expected_version": 0, "idempotency_key": str(uuid4())}
    )
    assert response.status_code == 200, response.text
    h.source = seed_source(h)
    return h


def open_dependencies(h, *, page=None):
    page = page or h.ui.page
    page.goto(h.ui.url + "?project=" + h.project["id"])
    page.locator("#np-name").wait_for()
    page.locator("#np-dependencies").click()
    page.locator("#ng-fact-new:not([disabled])").wait_for()


def inspect_output(page, identifier):
    page.locator(f'#ng-outputs [data-output-id="{identifier}"]').get_by_role(
        "button", name="Inspect output dependencies"
    ).click()
    page.locator("#ng-detail").wait_for()


def inspect_fact(page, identifier):
    page.locator(f'#ng-facts [data-fact-id="{identifier}"]').get_by_role(
        "button", name="Inspect fact revisions"
    ).click()
    page.locator("#ng-detail").wait_for()


def declare_in_ui(
    page,
    identifier,
    *,
    input_kind=None,
    input_id=None,
    input_revision=None,
    input_path=None,
    output_path=None,
):
    inspect_output(page, identifier)
    page.get_by_role("button", name="Declare output dependencies", exact=True).click()
    page.locator("#ng-coverage").select_option("complete")
    if input_kind:
        page.get_by_role("button", name="Add dependency", exact=True).click()
        row = page.locator("[data-dependency-binding]").last
        row.locator("[data-input-kind]").select_option(input_kind)
        row.locator("[data-input-identity]").select_option(input_id + ":" + input_revision)
        for name, pointer in (("input", input_path), ("output", output_path)):
            if pointer is not None:
                region = row.locator(f"[data-{name}-selector]")
                region.locator("[data-selector-kind]").select_option("json_pointer")
                region.locator("[data-selector-path]").fill(pointer)
    page.locator("#ng-save").click()
    page.locator("#ng-feedback").filter(has_text="Saved.").wait_for()
    page.locator("#ng-overview").wait_for()


def prepare_spec_repair(h):
    page = h.ui.page
    page.locator(
        f'#ng-assessments [data-assessment-id="{h.assessment["assessment"]["id"]}"]'
    ).get_by_role("button", name="Inspect change assessment").click()
    page.get_by_role("button", name="Prepare bounded repair", exact=True).click()
    page.locator("#ng-repair-rationale").fill(
        "Update the exact price field and preserve all remaining data for review."
    )
    card = page.locator(f'#ng-repair-outputs [data-artifact-id="{h.spec["artifact"]["id"]}"]')
    card.locator("[data-repair-selected]").check()
    card.locator("[data-before]").wait_for()
    assert card.locator("[data-selector-kind]").input_value() == "json_pointer"
    assert card.locator("[data-selector-path]").input_value() == "/unit_price"
    assert card.locator("[data-before]").input_value() == "20"
    card.locator("[data-after]").fill("25")
    return card


def test_browser_fact_declarations_cycle_assessment_and_two_output_repair(dependencies_ui):
    h = dependencies_ui
    h.spec = output(h, "spec.json", '{"unit_price":20,"color":"navy"}\n', "application/json")
    h.sheet = output(h, "line-sheet.md", "Unit price: USD 20. Color: navy.\n", "text/markdown")
    h.brand = output(h, "brand.txt", "Approved wordmark: STDOUT\n")
    h.notes = output(h, "notes.md", "Pricing discussion awaiting review\n", "text/markdown")
    page = h.ui.page
    open_dependencies(h)
    page.locator("#ng-fact-new").click()
    page.locator("#ng-fact-key").fill("unit-price")
    page.locator("#ng-fact-value").fill("20")
    page.locator("#ng-fact-unit").fill("USD")
    page.get_by_role("button", name="Add source binding", exact=True).click()
    source = page.locator("[data-source-binding]")
    source.locator("[data-source-id]").select_option(str(h.source.id))
    source.locator("[data-selector-kind]").select_option("json_pointer")
    source.locator("[data-selector-path]").fill("/unit_price")
    source.get_by_role("button", name="Inspect permitted source text").click()
    source.locator("pre").filter(has_text='"unit_price": 20').wait_for()
    page.locator("#ng-save").click()
    page.locator("#ng-detail-heading").filter(has_text="unit-price").wait_for()
    view = h.client.get(h.path).json()
    first_id = view["facts"][0]["fact"]["id"]
    h.fact = h.client.get(h.path + "/facts/" + first_id).json()
    page.locator("#ng-back").click()
    declare_in_ui(
        page,
        h.spec["artifact"]["id"],
        input_kind="fact",
        input_id=first_id,
        input_revision=fact_head(h.fact)["id"],
        output_path="/unit_price",
    )
    declare_in_ui(
        page,
        h.sheet["artifact"]["id"],
        input_kind="artifact",
        input_id=h.spec["artifact"]["id"],
        input_revision=artifact_head(h.spec)["id"],
        input_path="/unit_price",
    )
    declare_in_ui(page, h.brand["artifact"]["id"])
    declare(h, h.notes, coverage="partial")
    declare(
        h,
        h.spec,
        [
            edge(h.fact, output_path="/unit_price"),
            edge(h.sheet, kind="artifact", output_path="/unit_price"),
        ],
    )
    policy = h.client.put(
        h.project_path + "/artifacts/policy",
        json={
            "expected_version": 0,
            "require_agent_review": False,
            "require_human_approval": True,
            "idempotency_key": str(uuid4()),
        },
    )
    assert policy.status_code == 200, policy.text
    brand_detail = h.client.get(h.project_path + "/artifacts/" + h.brand["artifact"]["id"]).json()
    accepted = h.client.post(
        h.project_path + "/artifacts/" + h.brand["artifact"]["id"] + "/decisions",
        json=target_command(brand_detail, decision="approved"),
    )
    assert accepted.status_code == 200, accepted.text
    accepted_brand = accepted.json()
    changed_source = seed_source(h, 25, 2)
    page.locator("#ng-refresh").click()
    page.locator("#ng-feedback").filter(has_text="refreshed").wait_for()
    inspect_fact(page, first_id)
    page.get_by_role("button", name="Record new fact revision", exact=True).click()
    page.locator("#ng-fact-value").fill("25")
    page.locator("[data-source-id]").select_option(str(changed_source.id))
    page.locator("#ng-save").click()
    page.get_by_role("button", name="Assess a fact change", exact=True).wait_for()
    page.get_by_role("button", name="Assess a fact change", exact=True).click()
    page.locator("#ng-save").click()
    page.get_by_role("button", name="Prepare bounded repair", exact=True).wait_for()
    assert page.locator('[data-impact-status="affected"]').count() == 2
    assert page.locator('[data-impact-status="unaffected"]').count() == 1
    assert page.locator('[data-impact-status="uncertain"]').count() == 1
    page.get_by_role("button", name="Prepare bounded repair", exact=True).click()
    page.locator("#ng-repair-rationale").fill(
        "Update the exact price field and its line sheet while preserving brand approval."
    )
    spec = page.locator(f'#ng-repair-outputs [data-artifact-id="{h.spec["artifact"]["id"]}"]')
    spec.locator("[data-repair-selected]").check()
    spec.locator("[data-before]").wait_for()
    assert spec.locator("[data-before]").input_value() == "20"
    spec.locator("[data-after]").fill("25")
    sheet = page.locator(f'#ng-repair-outputs [data-artifact-id="{h.sheet["artifact"]["id"]}"]')
    sheet.locator("[data-repair-selected]").check()
    sheet.locator("[data-before]").wait_for()
    assert sheet.locator("[data-before]").input_value() == "Unit price: USD 20. Color: navy.\n"
    sheet.locator("[data-after]").fill("Unit price: USD 25. Color: navy.\n")
    page.locator("#ng-save").click()
    page.get_by_role("button", name="Commit these exact repairs", exact=True).wait_for()
    assert page.locator("[data-repair-output]").count() == 2
    saved_previews = h.client.get(h.path).json()["repairs"]
    assert len(saved_previews) == 1
    page.locator("#ng-close").click()
    page.wait_for_function(
        "id => window.SimonNativeProjects.getProject()?.id === id", arg=h.project["id"]
    )
    page.locator("#np-dependencies").click()
    page.locator(f'#ng-repairs [data-proposal-id="{saved_previews[0]["id"]}"]').get_by_role(
        "button", name="Inspect saved repair preview", exact=True
    ).click()
    page.get_by_role("button", name="Commit these exact repairs", exact=True).wait_for()
    assert page.locator("[data-repair-output]").count() == 2
    page.get_by_role("button", name="Commit these exact repairs", exact=True).click()
    page.locator("#ng-feedback").filter(has_text="Repair committed atomically").wait_for()
    assert page.get_by_role("button", name="Commit these exact repairs", exact=True).count() == 0
    for record in (h.spec, h.sheet):
        saved = h.client.get(h.project_path + "/artifacts/" + record["artifact"]["id"]).json()
        assert saved["artifact"]["version"] == record["artifact"]["version"] + 1
        assert saved["status"] != "accepted"
    brand_after = h.client.get(h.project_path + "/artifacts/" + h.brand["artifact"]["id"]).json()
    assert brand_after["artifact"] == accepted_brand["artifact"]
    assert brand_after["decisions"] == accepted_brand["decisions"]
    assert brand_after["status"] == "accepted"
    assert "notes.md" in page.locator("#ng-detail-content").inner_text()
    assert page.locator("#ng-detail-content").get_by_text("Color: navy.", exact=False).count() > 0
    screenshots = Path(".local/native-dependencies-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / "repair-desktop.png"))


def test_browser_preview_uncertain_response_checks_receipt_without_duplicate_repair(
    dependencies_ui,
):
    h = seed_graph(dependencies_ui)
    open_dependencies(h)
    prepare_spec_repair(h)
    calls = []

    def lose_response(route):
        if route.request.method != "POST":
            route.continue_()
            return
        calls.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 201
        route.abort("failed")

    h.ui.page.route("**/dependencies/repairs", lose_response)
    h.ui.page.locator("#ng-save").click()
    h.ui.page.locator("#ng-uncertain").wait_for()
    h.ui.page.locator("#ng-check").click()
    h.ui.page.get_by_role("button", name="Commit these exact repairs", exact=True).wait_for()
    assert len(calls) == 1
    assert (
        len(
            h.container.store.repair_proposals(
                h.ui.actor.workspace_id, h.project_record.id, limit=100
            )
        )
        == 1
    )
    assert (
        h.client.get(h.project_path + "/artifacts/" + h.spec["artifact"]["id"]).json()["artifact"]
        == h.spec["artifact"]
    )


def test_browser_conflict_preserves_replacements_and_reassesses_current_heads(dependencies_ui):
    h = seed_graph(dependencies_ui)
    open_dependencies(h)
    card = prepare_spec_repair(h)
    current = h.client.get(h.project_path + "/artifacts/" + h.spec["artifact"]["id"]).json()
    task = h.client.get(h.project_path + "/tasks/" + current["task"]["id"]).json()
    content = json.dumps({"unit_price": 20, "color": "human-edited-green"}) + "\n"
    response = h.client.post(
        h.project_path + "/artifacts/upload",
        json={
            "artifact_id": current["artifact"]["id"],
            "task_id": task["id"],
            "expected_version": current["artifact"]["version"],
            "expected_task_version": task["version"],
            "filename": "spec.json",
            "media_type": "application/json",
            "content_base64": base64.b64encode(content.encode()).decode(),
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    updated = response.json()
    old_declaration = next(
        row["declaration"]
        for row in h.client.get(h.path).json()["outputs"]
        if row["artifact"]["id"] == h.spec["artifact"]["id"]
    )
    # The new head has no declaration until its owner records current coverage.
    assert old_declaration is None
    declare(
        h,
        updated,
        [
            edge(h.fact, output_path="/unit_price"),
            edge(h.sheet, kind="artifact", output_path="/unit_price"),
        ],
    )
    h.ui.page.locator("#ng-save").click()
    h.ui.page.locator("#ng-conflict").wait_for()
    assert card.locator("[data-before]").input_value() == "20"
    assert card.locator("[data-after]").input_value() == "25"
    assert h.ui.page.locator("#ng-save").is_disabled()
    reassessments = []

    def lose_reassessment_response(route):
        reassessments.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 201
        route.abort("failed")

    h.ui.page.route("**/dependencies/assessments", lose_reassessment_response)
    h.ui.page.locator("#ng-rebase").click()
    h.ui.page.locator("#ng-uncertain").wait_for()
    assert card.locator("[data-after]").input_value() == "25"
    h.ui.page.locator("#ng-check").click()
    h.ui.page.locator("#ng-feedback").filter(has_text="draft remains available").wait_for()
    assert len(reassessments) == 1
    assert len(h.client.get(h.path).json()["assessments"]) == 2
    assert card.locator("[data-after]").input_value() == "25"
    h.ui.page.locator("#ng-save").click()
    h.ui.page.get_by_role("button", name="Commit these exact repairs", exact=True).wait_for()
    assert "human-edited-green" in h.ui.page.locator("#ng-detail-content").inner_text()


def test_browser_revoked_fact_history_and_preview_are_withheld(dependencies_ui):
    h = seed_graph(dependencies_ui)
    open_dependencies(h)
    prepare_spec_repair(h)
    h.ui.page.locator("#ng-save").click()
    h.ui.page.get_by_role("button", name="Commit these exact repairs", exact=True).wait_for()
    intake = h.client.get(h.project_path + "/intake").json()
    response = h.client.post(
        h.project_path + "/intake/sources/" + str(h.source.id) + "/revoke",
        json={"expected_version": intake["intake"]["version"], "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    h.ui.page.get_by_role("button", name="Commit these exact repairs", exact=True).click()
    h.ui.page.locator("#ng-dialog").wait_for(state="hidden")
    open_dependencies(h)
    inspect_fact(h.ui.page, h.fact["fact"]["id"])
    assert "withheld" in h.ui.page.locator("#ng-detail-content").inner_text()
    assert h.ui.page.locator("[data-fact-revision-id]").count() == 0
    assert h.ui.page.get_by_role("button", name="Record new fact revision", exact=True).count() == 0


def test_browser_member_sees_coverage_and_malicious_values_as_inert_text(dependencies_ui):
    h = seed_graph(dependencies_ui)
    malicious = (
        '<img src=x onerror="window.dependencyExecuted=true">'
        "<script>window.dependencyExecuted=true</script>"
    )
    dangerous, _ = save_fact(
        h, malicious, key="inert-text", value_type="string", unit="", sources=[]
    )
    member = h.ui.user()
    share(h.ui, h.project_record, member)
    page = member.page
    page.goto(h.ui.url + "?project=" + h.project["id"])
    page.locator("#np-name").wait_for()
    page.locator("#np-dependencies").click()
    page.locator(f'#ng-facts [data-fact-id="{dangerous["fact"]["id"]}"]').wait_for()
    assert page.locator("#ng-fact-new").is_disabled()
    assert malicious in page.locator("#ng-facts").inner_text()
    assert page.locator("#ng-dialog img, #ng-dialog script").count() == 0
    assert page.evaluate("Boolean(window.dependencyExecuted)") is False
    inspect_output(page, h.spec["artifact"]["id"])
    assert page.get_by_role("button", name="Declare output dependencies", exact=True).is_disabled()
    assert "complete" in page.locator("#ng-detail-summary").inner_text()
    page.locator("#ng-close").click()
    page.wait_for_function(
        "id => window.SimonNativeProjects.getProject()?.id === id", arg=h.project["id"]
    )
    page.locator("#np-artifacts").click()
    page.locator(f'#na-artifacts [data-artifact-id="{h.spec["artifact"]["id"]}"]').get_by_role(
        "button", name="Inspect artifact", exact=True
    ).click()
    page.locator("#na-dependencies").click()
    page.locator("#ng-detail-heading").filter(has_text="spec.json").wait_for()
    assert "complete" in page.locator("#ng-detail-summary").inner_text()
    assert page.get_by_role("button", name="Declare output dependencies", exact=True).is_disabled()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshots = Path(".local/native-dependencies-browser")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / "dependencies-mobile.png"))


def test_browser_project_switch_fences_late_dependency_content_and_clears_draft(dependencies_ui):
    h = seed_graph(dependencies_ui)
    other = seed_project(h.ui, name="Separate evidence project")
    open_dependencies(h)
    inspect_fact(h.ui.page, h.fact["fact"]["id"])
    h.ui.page.get_by_role("button", name="Record new fact revision", exact=True).click()
    h.ui.page.locator("#ng-fact-value").fill("999")
    h.ui.page.evaluate(
        "id => { window.dispatchEvent(new CustomEvent('native-project-loading', "
        "{ detail: { id } })); history.pushState({}, '', '?project=' + id); "
        "window.dispatchEvent(new PopStateEvent('popstate')); }",
        str(other.id),
    )
    h.ui.page.locator("#ng-dialog").wait_for(state="hidden")
    assert h.ui.page.locator("#ng-editor-context").inner_text() == ""
    assert h.ui.page.locator("#ng-fields").inner_text() == ""
    h.ui.page.locator("#np-name").filter(has_text="Separate evidence project").wait_for()
    h.ui.page.locator("#np-dependencies").click()
    h.ui.page.locator("#ng-facts").get_by_text(
        "No typed facts or requirements registered yet."
    ).wait_for()
    assert "999" not in h.ui.page.locator("#ng-dialog").inner_text()


def test_browser_missing_receipt_retries_the_same_fact_command(dependencies_ui):
    h = dependencies_ui
    open_dependencies(h)
    page = h.ui.page
    page.locator("#ng-fact-new").click()
    page.locator("#ng-fact-key").fill("owner-asserted-price")
    page.locator("#ng-fact-value").fill("20")
    page.locator("#ng-fact-unit").fill("USD")
    requests = []

    def fail_first(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            route.abort("failed")
        else:
            route.continue_()

    page.route("**/dependencies/facts", fail_first)
    page.locator("#ng-save").click()
    page.locator("#ng-uncertain").wait_for()
    assert page.locator("#ng-fact-value").is_disabled()
    page.locator("#ng-check").click()
    page.locator("#ng-retry").wait_for()
    page.locator("#ng-retry").click()
    page.locator("#ng-detail-heading").filter(has_text="owner-asserted-price").wait_for()
    assert len(requests) == 2 and requests[0] == requests[1]
    assert len(h.client.get(h.path).json()["facts"]) == 1


def test_browser_large_numeric_facts_remain_readable_without_inexact_editing(dependencies_ui):
    h = dependencies_ui
    record, _ = save_fact(h, 10**20, key="precise-large-number", sources=[])
    open_dependencies(h)
    card = h.ui.page.locator(f'#ng-facts [data-fact-id="{record["fact"]["id"]}"]')
    assert "exceeds browser numeric precision" in card.inner_text()
    inspect_fact(h.ui.page, record["fact"]["id"])
    assert h.ui.page.get_by_role("button", name="Record new fact revision", exact=True).count() == 0
    assert (
        fact_head(h.client.get(h.path + "/facts/" + record["fact"]["id"]).json())["value"] == 10**20
    )
