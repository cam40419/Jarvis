"""Reusable skill-based agents through the real authenticated browser API."""

import json

import pytest

from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_platform import AgentPlatformService
from tests.integration.test_project_command_browser import project_ui as project_ui

pytestmark = pytest.mark.browser


@pytest.fixture
def agent_library_ui(project_ui, tmp_path):
    page, _, _, container, _ = project_ui
    original = container.agent_platform.manifest
    tools = (
        ToolDefinition(
            id="native.local_file_read",
            description="Read local files",
            transport="native",
            configured=True,
            required_scopes=frozenset({"jobs:read"}),
        ),
        ToolDefinition(
            id="native.local_file_write",
            description="Write local documents",
            transport="native",
            configured=True,
            side_effect=True,
            action_policy="write",
            required_scopes=frozenset({"jobs:write"}),
        ),
        ToolDefinition(
            id="native.drive_read_file",
            description="Read Drive files",
            transport="native",
            configured=False,
            required_scopes=frozenset({"jobs:read"}),
        ),
    )
    manifest = PlatformManifest(
        tools=tools,
        agents=(
            original.agents[0],
            AgentProfile(
                id="file-reader",
                name="File research",
                description="Read and compare local sources.",
                instructions="Research authorized files.",
                tool_ids=(tools[0].id,),
                tool_scopes=frozenset({"jobs:read"}),
            ),
            AgentProfile(
                id="file-writer",
                name="Document writing",
                description="Write and revise reports.",
                instructions="Write and check local documents.",
                tool_ids=(tools[1].id,),
                tool_scopes=frozenset({"jobs:read", "jobs:write"}),
                max_action="write",
            ),
            AgentProfile(
                id="google-reader",
                name="Drive research",
                description="Read connected Drive files.",
                instructions="Research connected Drive files.",
                tool_ids=(tools[2].id,),
                tool_scopes=frozenset({"jobs:read"}),
            ),
        ),
        teams=(
            original.teams[0],
            TeamTemplate(
                id="documents",
                name="Documents",
                agent_ids=("file-reader", "file-writer", "google-reader"),
            ),
        ),
        models=original.models,
    )
    configured = AgentPlatformService(
        container.store,
        manifest,
        state_dir=tmp_path,
        environ={},
        integrations=container.connected.integrations,
        available_transports=("native",),
    )
    container.agent_platform.__dict__.update(configured.__dict__)
    container.agent_platform.project_team_resolver = container.project_coordinator.resolve_team
    container.agent_platform.project_profile_resolver = container.project_work.member_profiles
    container.agent_platform.project_visibility_resolver = container.project_work.project_resolver
    container.project_work.role_capture = container.agent_platform.agent_profiles.capture_role
    container.project_work.role_resolver = container.agent_platform.agent_profiles.resolve_role
    page.reload()
    page.locator("#work-open").click()
    return page, container


def fill_agent(page, name="Research coordinator"):
    page.locator("#agent-editor").get_by_label("Role title", exact=True).fill(name)
    page.locator("#agent-editor").get_by_label("Working instructions", exact=True).fill(
        "Research the source material, write the report, and verify the saved document."
    )


def test_create_combined_agent_in_library_and_reuse_its_role(
    agent_library_ui,
    tmp_path,
):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    expect(page.locator("#agent-editor")).to_be_visible()
    fill_agent(page)
    expect(page.locator("#al-skills")).not_to_be_visible()
    expect(page.locator("#al-skills")).to_contain_text("Needs setup")
    expect(page.locator("#al-skills")).not_to_contain_text("native.drive_read_file")
    page.screenshot(path=str(tmp_path / "agent-editor-desktop.png"), animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#sidebar")).not_to_be_in_viewport()
    assert page.locator("#agent-editor").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=str(tmp_path / "agent-editor-mobile.png"), animations="disabled")
    page.get_by_role("button", name="Save agent", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    expect(page.locator("#al-agent-list")).to_contain_text("Research coordinator")
    expect(page.locator("#al-agent-list")).to_contain_text("All connected workspace tools")
    page.get_by_role("button", name="Edit agent: Research coordinator", exact=True).click()
    expect(page.locator("#agent-editor").get_by_label("Role title", exact=True)).to_have_value(
        "Research coordinator"
    )
    expect(page.locator("#al-save-note")).to_contain_text("Changes apply to future work")
    page.locator("#agent-editor").get_by_label("Role title", exact=True).fill(
        "Research & reports lead"
    )
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    expect(page.locator("#al-agent-list")).to_contain_text("Research & reports lead")
    page.reload()
    page.locator("#work-open").click()
    page.locator("#agent-tab-agents").click()
    expect(page.locator("#al-agent-list")).to_contain_text("Research & reports lead")
    page.locator("#pc-new-project").click()
    page.locator("#pc-create-dialog").get_by_role("button", name="Add agent", exact=True).click()
    expect(page.locator("#pc-member-template")).to_contain_text("Research & reports lead")


def test_agent_editor_keeps_conflicting_draft_and_marks_unconfigured_skills(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    fill_agent(page, "Cloud researcher")
    expect(page.locator("#al-save-note")).to_contain_text("Connected accounts")
    page.get_by_role("button", name="Save agent", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    expect(page.locator("#al-agent-list .al-badge")).to_have_text("Configured")
    page.get_by_role("button", name="Edit agent: Cloud researcher", exact=True).click()
    page.locator("#agent-editor").get_by_label("Role title", exact=True).fill(
        "Keep this unsaved title"
    )
    page.evaluate(
        """async () => {
          const record = (await api('/v1/agent-platform/catalog')).custom_agents[0];
          await api('/v1/agent-platform/agents/' + record.id, {name: 'Saved by another editor',
            description: record.description, skill_ids: record.skill_ids,
            expected_version: record.version, idempotency_key: 'concurrent-agent-change'}, 'PATCH');
        }"""
    )
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.locator("#al-feedback")).to_contain_text("Your draft is preserved")
    expect(page.locator("#agent-editor").get_by_label("Role title", exact=True)).to_have_value(
        "Keep this unsaved title"
    )
    page.keyboard.press("Escape")
    page.locator("#agent-refresh").click()
    expect(page.locator("#al-agent-list")).to_contain_text("Saved by another editor")
    page.locator("#al-new-agent").click()
    expect(page.locator("#al-name")).to_have_value("")
    page.keyboard.press("Escape")


def test_edit_legacy_bundles_converts_to_shared_role_without_losing_identity(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    saved = page.evaluate("""() => api('/v1/agent-platform/agents', {
      name: 'Existing researcher', description: 'Research and prepare documents.',
      skill_ids: ['profile.file-reader', 'profile.file-writer'],
      idempotency_key: 'legacy-agent-browser-create'
    })""")
    page.locator("#agent-refresh").click()
    page.locator("#agent-tab-agents").click()
    page.get_by_role("button", name="Edit agent: Existing researcher", exact=True).click()
    expect(page.locator("#al-skills")).not_to_be_visible()
    page.locator("#al-name").fill("Existing research lead")
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    edited = page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"][0]
    assert edited["id"] == saved["id"]
    assert edited["version"] == saved["version"] + 1
    assert edited["skill_ids"] == []
    assert edited["profile"]["tool_access"] == "shared"
    assert set(edited["profile"]["tool_ids"]) == {
        "native.local_file_read",
        "native.local_file_write",
        "native.drive_read_file",
    }


def test_project_creation_saves_roles_and_iteration_limits_once(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    page.locator("#pc-new-project").click()
    page.locator("#pc-create-name").fill("Persistent project")
    page.locator("#pc-create-goal").fill("Improve the report over several iterations.")
    page.locator("#pc-create-dialog").get_by_role("button", name="Add agent", exact=True).click()
    page.locator("#pc-member-name").fill("Report owner")
    page.locator("#pc-member-description").fill("Research, write and verify each report revision.")
    expect(page.locator("#pc-member-skills")).not_to_be_visible()
    page.locator("#pc-member-save").click()
    page.locator("#pc-create-continuous").check()
    page.locator("#pc-create-budget").fill("10")
    page.locator("#pc-create-cadence").fill("30")
    page.locator("#pc-create-cycles").fill("5")
    page.locator("#pc-create-submit").click()
    expect(page.locator("#pc-create-dialog")).not_to_be_visible()
    projects = page.evaluate("() => api('/v1/projects')")
    created = next(project for project in projects if project["subject"] == "Persistent project")
    saved = page.evaluate("id => api('/v1/projects/' + id + '/command')", created["id"])
    policy = saved["state"]["autonomy"]
    assert policy["mode"] == "scheduled" and policy["execution_policy"] == "bounded"
    assert policy["model_budget_usd"] == 10
    assert policy["cadence_minutes"] == 30 and policy["max_cycles"] == 5
    member = next(
        member
        for member in saved["state"]["team"]["members"].values()
        if member["name"] == "Report owner"
    )
    assert member["skill_ids"] == []


def install_setup_boundary(page):
    """Exercise the library's review/save boundary without calling a model.

    The shared assistant has separate conversation, cancel, and provider-error tests.
    Keep callbacks so these tests can also deliver a stale recommendation.
    """
    page.evaluate(
        """() => {
          window.librarySetupRequests = [];
          window.SimonSetupAssistant = {open(options) {
            window.librarySetupRequests.push(options);
          }};
        }"""
    )


def apply_library_recommendation(page, *, name="Research and reports", skills=None, index=-1):
    return page.evaluate(
        """({name, skills, index}) => window.librarySetupRequests.at(index).onApply({
          team_name: '', roles: [{name,
            description: 'Read source documents, write a useful report, and verify the result.',
            skill_ids: skills, is_lead: false, rationale: 'Own research and reporting together.'}]
        })""",
        {
            "name": name,
            "skills": skills or ["tool.native.local_file_read", "tool.native.local_file_write"],
            "index": index,
        },
    )


def test_describe_agent_requires_review_and_normal_save_for_actual_grants(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    requests = []

    def recommendation(route):
        requests.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "message": "One agent can research documents and write the report.",
                    "proposal": {
                        "team_name": "",
                        "roles": [
                            {
                                "name": "Research and reports",
                                "description": "Research sources and write a verified report.",
                                "skill_ids": [
                                    "tool.native.local_file_read",
                                    "tool.native.local_file_write",
                                    "tool.native.drive_read_file",
                                ],
                                "is_lead": False,
                                "rationale": "Keep related research and writing together.",
                            }
                        ],
                    },
                    "warnings": [],
                }
            ),
        )

    # Keep the real dialogs and save API; replace only the recommendation response.
    page.route("**/v1/agent-platform/setup-assistant", recommendation)
    page.locator("#agent-tab-agents").click()
    page.get_by_role("button", name="Describe an agent", exact=True).click()
    expect(page.locator("#setup-assistant")).to_be_visible()
    expect(page.locator("#sa-send")).to_be_enabled()
    assert requests == []
    assert page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"] == []
    page.locator("#sa-message").fill("I need an agent to research local and Drive files and write.")
    page.locator("#sa-send").click()
    expect(page.locator("#sa-apply")).to_be_enabled()
    assert requests[0] == {
        "mode": "agent",
        "project_id": None,
        "privacy": "allow_cloud",
        "messages": [
            {
                "role": "user",
                "content": "I need an agent to research local and Drive files and write.",
            }
        ],
        "current_draft": {"team_name": "", "roles": []},
    }
    expect(page.locator("#al-name")).to_have_value("")
    assert page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"] == []
    page.locator("#sa-message").fill("Keep research and writing in the same role.")
    page.locator("#sa-send").click()
    expect(page.locator("#sa-apply")).to_be_enabled()
    assert len(requests) == 2
    assert [item["role"] for item in requests[1]["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert requests[1]["current_draft"]["roles"][0]["name"] == "Research and reports"
    page.locator("#sa-apply").click()
    expect(page.locator("#setup-assistant")).not_to_be_visible()
    editor = page.locator("#agent-editor")
    expect(editor.get_by_label("Role title", exact=True)).to_have_value("Research and reports")
    expect(page.locator("#al-feedback")).to_contain_text("Review the role and working instructions")
    expect(page.locator("#al-save-note")).to_contain_text("workspace permissions")
    assert page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"] == []

    # The recommendation remains editable; the saved grants reflect this review.
    editor.get_by_label("Role title", exact=True).fill("Reviewed research role")
    editor.get_by_role("button", name="Save agent", exact=True).click()
    expect(editor).not_to_be_visible()
    records = page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"]
    assert len(records) == 1
    assert records[0]["name"] == "Reviewed research role"
    assert set(records[0]["profile"]["tool_ids"]) == {
        "native.local_file_read",
        "native.local_file_write",
        "native.drive_read_file",
    }
    assert records[0]["profile"]["max_action"] == "write"


def test_assisted_edit_preserves_saved_identity_and_grants_until_save(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    saved = page.evaluate(
        """() => api('/v1/agent-platform/agents', {
          name: 'Saved research role', description: 'Read and write local documents.',
          skill_ids: ['profile.file-reader', 'profile.file-writer'],
          idempotency_key: 'assisted-edit-existing-role'
        })"""
    )
    install_setup_boundary(page)
    page.locator("#agent-refresh").click()
    page.locator("#agent-tab-agents").click()
    page.get_by_role("button", name="Edit agent: Saved research role", exact=True).click()
    page.get_by_role("button", name="Describe changes", exact=True).click()
    requested = page.evaluate("window.librarySetupRequests[0].currentDraft.roles[0]")
    assert requested["name"] == saved["name"]
    assert set(requested["skill_ids"]) == {
        "tool.native.local_file_read",
        "tool.native.local_file_write",
    }
    # Merely asking for a recommendation never converts legacy saved groups.
    expect(page.locator('#al-skills input[value="profile.file-reader"]')).to_be_checked()
    assert apply_library_recommendation(
        page, name="Focused researcher", skills=["tool.native.local_file_read"]
    )
    before_save = page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"]
    assert len(before_save) == 1
    assert before_save[0]["version"] == saved["version"]
    assert before_save[0]["skill_ids"] == saved["skill_ids"]
    assert before_save[0]["profile"]["tool_ids"] == saved["profile"]["tool_ids"]
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.locator("#agent-editor")).not_to_be_visible()
    after_save = page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"]
    assert len(after_save) == 1
    assert after_save[0]["id"] == saved["id"]
    assert after_save[0]["version"] == saved["version"] + 1
    assert after_save[0]["name"] == "Focused researcher"
    assert after_save[0]["skill_ids"] == []
    assert set(after_save[0]["profile"]["tool_ids"]) == {
        "native.local_file_read",
        "native.local_file_write",
        "native.drive_read_file",
    }
    assert after_save[0]["profile"]["max_action"] == "write"


def test_assistant_rejects_stale_drafts_and_unknown_skills_without_losing_edits(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    install_setup_boundary(page)
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    fill_agent(page, "Keep my role")
    page.locator("#al-assist").click()
    assert not apply_library_recommendation(page, skills=["tool.unknown.write"])
    expect(page.locator("#al-feedback")).to_contain_text("draft is preserved")
    expect(page.locator("#al-name")).to_have_value("Keep my role")

    page.locator("#al-name").fill("Newer manual edit")
    assert not apply_library_recommendation(page)
    expect(page.locator("#al-feedback")).to_contain_text("Your form changed")
    expect(page.locator("#al-name")).to_have_value("Newer manual edit")

    page.locator("#al-close").click()
    page.locator("#al-new-agent").click()
    expect(page.locator("#al-name")).to_be_enabled()
    assert not apply_library_recommendation(page)
    expect(page.locator("#al-name")).to_have_value("")
    assert page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"] == []


def test_incomplete_agent_draft_prefills_assistant_and_open_failure_preserves_it(agent_library_ui):
    from playwright.sync_api import expect

    page, _ = agent_library_ui
    install_setup_boundary(page)
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    expect(page.locator("#al-name")).to_be_enabled()
    page.locator("#al-description").fill("Help research fashion suppliers and draft a comparison.")
    page.locator("#al-assist").click()
    request = page.evaluate(
        """() => ({draft: window.librarySetupRequests[0].currentDraft,
          prompt: window.librarySetupRequests[0].initialPrompt})"""
    )
    assert request == {
        "draft": {"team_name": "", "roles": []},
        "prompt": "Help research fashion suppliers and draft a comparison.",
    }
    page.evaluate(
        """() => {window.SimonSetupAssistant.open = () => {
          throw new Error('Assistant connection unavailable.');
        };}"""
    )
    page.locator("#al-assist").click()
    expect(page.locator("#al-feedback")).to_contain_text("Your draft is preserved")
    expect(page.locator("#al-description")).to_have_value(request["prompt"])
    expect(page.locator("#al-name")).to_have_value("")
    assert page.evaluate("() => api('/v1/agent-platform/catalog')")["custom_agents"] == []


def test_github_connection_form_saves_encrypted_backend_settings(project_ui):
    import httpx
    from playwright.sync_api import expect

    page, _dispatcher, _scheduler, container, _synthetic = project_ui
    service = container.project_boards.integrations
    service.clickup.http.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"login": "browser-owner"})
    )
    page.locator("#connections-open").click()
    page.locator("#integration-provider").select_option("github")
    expect(page.locator("#integration-github")).to_be_visible()
    page.locator("#integration-credential").fill("browser-github-test-token")
    page.locator("#integration-github-repositories").fill("example/allowed\nexample/another")
    page.locator("#integration-github-write").check()
    page.locator("#integration-connect").click()
    expect(page.locator("#integration-status")).to_have_text(
        "Connected. Your account is ready to use."
    )
    from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID

    record = next(
        row
        for row in container.store.integration_connections(DEV_WORKSPACE_ID, DEV_ACTOR_ID)
        if row.provider == "github"
    )
    assert record.settings["repositories"] == ["example/allowed", "example/another"]
    assert record.settings["write_enabled"]
    assert "browser-github-test-token" not in record.model_dump_json()
    page.locator("#integration-accounts").get_by_role(
        "button", name="Reconnect", exact=True
    ).click()
    expect(page.locator("#integration-github-repositories")).to_have_value(
        "example/allowed\nexample/another"
    )
    expect(page.locator("#integration-github-write")).to_be_checked()


def test_skill_errors_do_not_replace_short_tool_ids_inside_longer_ids(agent_library_ui):
    from playwright.sync_api import expect

    page, _container = agent_library_ui

    def catalog(route):
        response = route.fetch()
        value = response.json()
        value["tool_statuses"].extend(
            [
                {
                    "id": "github.issue",
                    "description": "Read one GitHub issue and its Markdown body.",
                },
                {"id": "github.issue_create", "description": "Create a GitHub issue."},
            ]
        )
        value["individual_skills"].append(
            {
                "id": "tool.github.issue_create",
                "name": "Create a GitHub issue",
                "description": "Create an issue.",
                "category": "GitHub",
                "tool_ids": ["github.issue_create"],
                "state": "disabled",
                "blocked_reasons": ["github.issue_create: integration is disabled"],
            }
        )
        route.fulfill(json=value)

    page.route("**/v1/agent-platform/catalog", catalog)
    page.locator("#agent-tab-agents").click()
    page.locator("#al-new-agent").click()
    expect(page.locator("#al-skills")).to_contain_text(
        "Create a GitHub issue: integration is disabled"
    )
    expect(page.locator("#al-skills")).not_to_contain_text("body._create")
