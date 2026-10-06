"""Outside research cannot silently turn into internal-file-only assignments."""

import json
from copy import deepcopy

import pytest

from simon.adapters.browser_tools import browser_tool_definitions
from simon.domain.errors import ValidationError
from simon.domain.execution import EnvironmentDefinition
from simon.domain.project_coordination import LeadDecision
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_research import (
    public_web_capabilities,
    research_blocker,
    research_requirements,
    validate_research_plan,
)
from simon.services.worker_completion import PROJECT_DELIVERABLE_CONTRACT
from tests.unit.test_project_coordinator import harness

REQUEST = (
    "Research algorithmic clothing creation and profitability. Find suppliers that can "
    "manufacture entire batches to order. Do some digging into other brands that claim "
    "similar things. Produce manufacturing and competition documents."
)


def roster():
    return [
        {
            "agent_id": "lead",
            "ready_tools": [{"id": "native.drive_search_files"}],
        },
        {
            "agent_id": "research",
            "ready_tools": [
                {"id": "web.search", "public_web": ["search"]},
                {"id": "browser.read", "public_web": ["read"]},
            ],
        },
        {"agent_id": "writer", "ready_tools": []},
    ]


def decision(*, agent="research", tools=("web.search", "browser.read")):
    return LeadDecision.model_validate(
        {
            "status": "plan",
            "summary": "Research public sources, then prepare the documents.",
            "tasks": [
                {
                    "id": "sources",
                    "title": "Supplier and competitor research",
                    "agent_id": agent,
                    "objective": "Find suppliers and investigate other brands; cite sources.",
                    "tool_ids": tools,
                },
                {
                    "id": "documents",
                    "title": "Research documents",
                    "agent_id": "writer",
                    "objective": "Synthesize supplier research into manufacturing documents.",
                    "depends_on": ["sources"],
                    "tool_ids": [],
                },
            ],
        }
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (REQUEST, {"search", "read"}),
        ("Research the public web for manufacturing costs.", {"search", "read"}),
        ("Read the online source page.", {"read"}),
        ("Read https://example.com/supplier for its minimum order.", {"read"}),
        ("Create a research plan for our new collection.", set()),
        ("Review existing competitor research and write a summary.", set()),
        ("Research competitors using only the provided documents.", set()),
        ("Only use project files to assess suppliers.", set()),
        ("Read the competition document and update the project brief.", set()),
    ],
)
def test_request_source_requirements_are_explicit_and_bounded(text, expected):
    assert research_requirements(text) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "research/algorithmic-clothing-competition.md",
        "`research/algorithmic-clothing-competition.md`",
        "research-competitors.pdf",
        "`online-supplier-research.docx`",
        "./research/competition",
        "`research/competition`",
        r"C:\research\competition.md",
        "/research/competition",
        "research-competition.py",
        "web-supplier-research.js",
        "`research/competition notes.ts`",
        '"research/competition notes.md"',
        "'research/competition notes.pdf'",
    ],
)
def test_local_artifact_names_do_not_request_public_web_work(reference):
    assert research_requirements(f"Save the completed report unchanged to {reference}.") == set()
    assert research_requirements(f"Read {reference} and update the project brief.") == set()


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("Research suppliers and save findings to research/competition.md.", {"read", "search"}),
        (
            "Research competition using web sources; write `research/competition.md`.",
            {"read", "search"},
        ),
        ("Find suppliers/competitors; save research.md.", {"read", "search"}),
        ("Read https://example.com/research/competition.pdf and save report.md.", {"read"}),
        ("Inspect `https://example.com/research/competition.md` then save summary.md.", {"read"}),
        ("Research the public web and produce online-research.md.", {"read", "search"}),
        ('Research suppliers and write "research/competition notes.md".', {"read", "search"}),
        ("`Research suppliers/competition and save findings.md`", {"read", "search"}),
    ],
)
def test_artifact_references_do_not_hide_actual_research_or_source_reads(instruction, expected):
    assert research_requirements(instruction) == expected


def test_completed_report_writer_needs_no_new_web_research_from_output_paths():
    objective = (
        "Save the two completed reports unchanged as two separate editable project files. "
        "First read the NEW successful manufacturing artifact supplied in this execution's "
        "controller handoff via project.output_read; then project.output_save it to "
        "research/algorithmic-clothing-manufacturing-strategy.md. Do not use the earlier "
        "failed manufacturing draft. The competition report is ALREADY DONE and must not "
        "be rerun, rewritten, or summarized into a substitute. Its exact successful artifact "
        "is run_id=00000000-0000-0000-0000-000000000001, "
        "artifact_id=00000000-0000-0000-0000-000000000002, sha256=" + "a" * 64 + ". "
        "Use project.output_read on that exact source, then project.output_save unchanged "
        "to research/algorithmic-clothing-competition.md. Preserve both dependencies: new "
        "manufacturing completion plus the existing done competition task. Verify both "
        "source reads and successful copy receipts (paths/revisions/byte counts), report "
        "actual paths, and make no publication claim without receipts. Do not overwrite "
        "an existing different file without the normal revision protection. No discovery "
        "or competitor rerun is needed. Final status may link the retained competition "
        "sources without changing its report: https://example.com/ ; "
        "https://example.org/odds/generator?popup=how-it-works . Distinguish this source "
        "index from new research."
    )
    assert research_requirements(objective) == set()
    planned = LeadDecision.model_validate(
        {
            "status": "plan",
            "summary": "Save the completed source artifacts unchanged.",
            "tasks": [
                {
                    "id": "documents",
                    "title": "Save completed documents",
                    "agent_id": "writer",
                    "objective": objective,
                    "tool_ids": [],
                }
            ],
        }
    )
    validate_research_plan("Save the completed reports.", planned, roster())
    with pytest.raises(ValidationError, match="omits the public-web research"):
        validate_research_plan(REQUEST, planned, roster())


def test_exact_public_capabilities_do_not_treat_drive_search_or_screenshots_as_web_research():
    assert (
        public_web_capabilities(
            ToolDefinition(
                id="native.drive_search_files", description="Search documents", transport="native"
            )
        )
        == ()
    )
    browser = {tool.id: tool for tool in browser_tool_definitions(enabled=True)}
    assert public_web_capabilities(browser["browser.read"]) == ("read",)
    assert public_web_capabilities(browser["browser.screenshot"]) == ()
    assert public_web_capabilities(browser["browser.render_html"]) == ()
    assert public_web_capabilities(
        ToolDefinition(
            id="web.search",
            description="Find public web sources",
            transport="test",
            capabilities=frozenset({"web.search"}),
        )
    ) == ("search",)


def test_read_only_browser_does_not_supply_discovery():
    members = roster()
    members[1]["ready_tools"] = members[1]["ready_tools"][1:]
    reason = research_blocker(REQUEST, members)
    assert "public-web search" in reason
    assert "public-web page reading" not in reason


def test_correct_member_and_real_research_dependencies_are_valid():
    validate_research_plan(REQUEST, decision(), roster())


@pytest.mark.parametrize(
    "tools", [(), ("native.drive_search_files",), ("web.search", "browser.read")]
)
def test_lead_cannot_substitute_drive_or_borrow_another_members_web_tools(tools):
    with pytest.raises(ValidationError, match="omits the public-web research"):
        validate_research_plan(REQUEST, decision(agent="lead", tools=tools), roster())


def test_one_research_assignment_does_not_cover_independent_drive_only_research():
    planned = decision()
    tasks = list(planned.tasks)
    tasks[1] = tasks[1].model_copy(
        update={
            "objective": "Investigate other brands and assess competitors.",
            "depends_on": (),
            "agent_id": "lead",
            "tool_ids": ("native.drive_search_files",),
        }
    )
    with pytest.raises(ValidationError, match="prerequisite tasks do not supply"):
        validate_research_plan(
            REQUEST, planned.model_copy(update={"tasks": tuple(tasks)}), roster()
        )


def test_complete_without_executed_research_is_blocked_but_waiting_is_valid():
    for status in ("complete", "waiting"):
        result = LeadDecision(status=status, summary="Research sources are ready.", tasks=())
        if status == "complete":
            with pytest.raises(ValidationError, match="omits the public-web research"):
                validate_research_plan(REQUEST, result, roster())
        else:
            validate_research_plan(REQUEST, result, [])


def configure_research(h, tmp_path, *, browser_enabled=True, environment_enabled=True):
    browser = next(
        tool
        for tool in browser_tool_definitions(enabled=browser_enabled)
        if tool.id == "browser.read"
    )
    browser = browser.model_copy(update={"settings": {"allowed_origins": ["https://example.com"]}})
    search = ToolDefinition(
        id="web.search",
        description="Search public web sources",
        transport="test",
        configured=True,
        capabilities=frozenset({"web.search"}),
        settings={"network": True},
    )
    drive = ToolDefinition(
        id="native.drive_search_files",
        description="Search connected documents",
        transport="test",
        configured=True,
    )
    manifest = h.platform.manifest.model_copy(
        update={
            "tools": (search, browser, drive),
            "environments": (
                EnvironmentDefinition(
                    id="browser-web",
                    kind="docker",
                    container_image="test-browser",
                    enabled=environment_enabled,
                    capabilities=frozenset({"browser", "python"}),
                    network="bridge",
                ),
            ),
            "models": tuple(
                model.model_copy(update={"capabilities": frozenset({"text", "tools"})})
                for model in h.platform.manifest.models
            ),
            "agents": tuple(
                profile.model_copy(
                    update={
                        "tool_ids": ("web.search", "browser.read")
                        if profile.id == "research"
                        else ("native.drive_search_files",)
                        if profile.id == "lead"
                        else (),
                        "environment_ids": ("browser-web",) if profile.id == "research" else (),
                        "tool_scopes": h.actor.scopes,
                    }
                )
                for profile in h.platform.manifest.agents
            ),
        }
    )
    h.platform = AgentPlatformService(
        h.store, manifest, state_dir=tmp_path / "research", available_transports=("test", "browser")
    )
    h.runs = AgentRunService(h.platform, enabled=True, actor_resolver=lambda *_: h.actor)
    h.coordinator = ProjectCoordinator(h.work, h.runs)
    h.work.team_validator = h.coordinator.validate_team


@pytest.mark.parametrize("browser_enabled,environment_enabled", [(False, True), (True, False)])
def test_disabled_browser_or_environment_blocks_before_any_model_work(
    tmp_path, browser_enabled, environment_enabled
):
    h = harness(tmp_path)
    configure_research(
        h, tmp_path, browser_enabled=browser_enabled, environment_enabled=environment_enabled
    )
    state = h.work.request_cycle(h.actor, h.project.id, REQUEST, "outside-research")
    members = h.coordinator._planning_roster(h.actor, state)
    assert not any(
        tool["id"] == "browser.read" for member in members for tool in member["ready_tools"]
    )
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    latest = h.work.get(h.actor, h.project.id)
    assert latest.last_cycle.phase == "blocked"
    assert latest.last_cycle.planning_run_id is None
    assert latest.last_cycle.execution_run_id is None
    assert "public-web page reading" in latest.last_cycle.error


def test_prompt_schema_and_compiled_assignments_keep_actual_tool_owners(tmp_path):
    h = harness(tmp_path)
    configure_research(h, tmp_path)
    state = h.work.request_cycle(h.actor, h.project.id, REQUEST, "ready-research")
    members = h.coordinator._planning_roster(h.actor, state)
    original = deepcopy(members)
    prompt = h.coordinator._planning_prompt(h.actor, state, members)
    payload = json.loads(prompt.split("from this user data:\n", 1)[1])
    assert payload["required_public_web"] == ["read", "search"]
    assert members == original
    assert payload["team"][1]["ready_tools"][0]["public_web"] == ["search"]
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    current = h.work.get(h.actor, h.project.id)
    planning = h.store.get_job(current.active_cycle.planning_plan_id).input["request"]["tasks"][0]
    assert planning["completion_contract"] is None
    update = h.coordinator._compile(h.actor, current, decision())
    assert update.cycle.phase == "ready"
    job = h.store.get_job(update.cycle.execution_plan_id)
    specifications = job.input["request"]["tasks"]
    assert all(
        task["completion_contract"] == PROJECT_DELIVERABLE_CONTRACT.model_dump(mode="json")
        for task in specifications
    )
    assert specifications[0]["agent_id"] == "research"
    assert specifications[0]["tool_ids"] == ["web.search", "browser.read"]
    assert specifications[1]["tool_ids"] == []
    assert specifications[1]["depends_on"] == ["sources"]
    assert "suppliers' and brands' official pages" in specifications[0]["additional_instructions"]
    assert "unique printed artwork" in specifications[0]["additional_instructions"]
    assert "instead of repeating identical reads" in specifications[0]["additional_instructions"]
    assert [task["output_tokens"] for task in specifications] == [4096, 4096, 4096]
    plan = h.platform.get(h.actor, update.cycle.execution_plan_id)
    assert [task.model.request.output_tokens for task in plan.tasks] == [4096, 4096, 4096]


def test_execution_output_allowance_remains_bounded_by_member_ceiling(tmp_path):
    h = harness(tmp_path)
    configure_research(h, tmp_path)
    h.platform.manifest = h.platform.manifest.model_copy(
        update={
            "agents": tuple(
                profile.model_copy(
                    update={
                        "max_output_tokens": 12000 if profile.id == "research" else 1024,
                    }
                )
                for profile in h.platform.manifest.agents
            ),
        }
    )
    state = h.work.request_cycle(h.actor, h.project.id, REQUEST, "output-allowance")
    update = h.coordinator._compile(h.actor, state, decision())
    plan = h.platform.get(h.actor, update.cycle.execution_plan_id)
    assert [task.model.request.output_tokens for task in plan.tasks] == [8192, 1024, 1024]
