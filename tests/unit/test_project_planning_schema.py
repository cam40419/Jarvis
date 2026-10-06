"""The generated planning schema keeps each member's tools paired with that member."""

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError as PydanticError

from simon.domain.errors import ValidationError
from simon.domain.project_coordination import LeadDecision
from simon.services.project_coordinator import planning_result_schema
from tests.unit.test_project_coordinator import backlog_dependencies, harness, planning
from tests.unit.test_project_planning_recovery import source_tools


@pytest.fixture
def roster():
    return [
        {
            "agent_id": "file-writer",
            "ready_tools": [{"id": "native.local_file_read"}, {"id": "native.local_file_write"}],
        },
        {
            "agent_id": "drive-reader",
            "ready_tools": [
                {"id": "native.project_files_list"},
                {"id": "native.project_file_read"},
            ],
        },
        {"agent_id": "reviewer", "ready_tools": []},
    ]


def task(agent_id, tool_ids):
    return {
        "id": "assess",
        "title": "Assessment",
        "agent_id": agent_id,
        "objective": "Assess the verified project sources.",
        "depends_on": [],
        "tool_ids": tool_ids,
        "todo_id": None,
    }


def plan(task):
    return {"status": "plan", "summary": "Use the authorized source reader.", "tasks": [task]}


def test_schema_accepts_each_members_actual_tools_and_tool_free_work(roster):
    schema = planning_result_schema(roster)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for member in roster:
        document = plan(task(member["agent_id"], [tool["id"] for tool in member["ready_tools"]]))
        validator.validate(document)
        assert LeadDecision.model_validate(document).status == "plan"
        validator.validate(plan(task(member["agent_id"], [])))
    validator.validate({"status": "waiting", "summary": "A user decision is needed.", "tasks": []})


@pytest.mark.parametrize(
    "member,tools",
    [
        ("file-writer", ["native.project_file_read"]),
        ("drive-reader", ["native.local_file_write"]),
        ("reviewer", ["native.local_file_read"]),
        ("unknown-agent", []),
    ],
)
def test_schema_rejects_cross_member_tools_and_unlisted_agents(roster, member, tools):
    with pytest.raises(SchemaError):
        Draft202012Validator(planning_result_schema(roster)).validate(plan(task(member, tools)))


def test_schema_requires_all_fields_and_rejects_extra_keys(roster):
    schema = planning_result_schema(roster)
    validator = Draft202012Validator(schema)
    valid = plan(task("drive-reader", ["native.project_file_read"]))
    for field in valid["tasks"][0]:
        broken = deepcopy(valid)
        broken["tasks"][0].pop(field)
        with pytest.raises(SchemaError):
            validator.validate(broken)
    for container in ("root", "task"):
        broken = deepcopy(valid)
        (broken if container == "root" else broken["tasks"][0])["new_permission"] = True
        with pytest.raises(SchemaError):
            validator.validate(broken)
    assert "$defs" not in schema
    assert schema["type"] == "object"
    assert schema["properties"]["tasks"]["maxItems"] == 8


def test_generation_schema_does_not_replace_dependency_graph_validation(roster):
    document = plan(task("drive-reader", []))
    document["tasks"][0]["depends_on"] = ["assess"]
    Draft202012Validator(planning_result_schema(roster)).validate(document)
    with pytest.raises(ValueError, match="acyclic"):
        LeadDecision.model_validate(document)


def test_dependencies_use_local_task_ids_even_when_saved_todo_is_selectable(roster):
    saved_id = "c-08f2110fd4e64e2e961bd7c2da1dacea-research_strategy"
    schema = planning_result_schema(roster, ready_todo_ids=(saved_id,))
    manufacturing = task("drive-reader", ["native.project_file_read"])
    manufacturing.update(id="manufacturing_research", todo_id=saved_id)
    writer = task("file-writer", ["native.local_file_write"])
    writer.update(id="save_reports", depends_on=[saved_id])
    document = {
        "status": "plan",
        "summary": "Research then save.",
        "tasks": [manufacturing, writer],
    }
    dependency_schema = schema["properties"]["tasks"]["items"]["properties"]["depends_on"]
    assert dependency_schema["items"]["pattern"] == r"^[a-z][a-z0-9_-]{0,39}$"
    assert "never saved todo IDs" in dependency_schema["description"]
    with pytest.raises(SchemaError):
        Draft202012Validator(schema).validate(document)
    with pytest.raises(PydanticError) as rejected:
        LeadDecision.model_validate(document)
    assert any(
        error["type"] == "string_pattern_mismatch" and error["loc"] == ("tasks", 1, "depends_on", 0)
        for error in rejected.value.errors()
    )
    # Only an explicit, unambiguous local reference is accepted. No guessed
    # mapping from public backlog IDs is performed by the DTO or compiler.
    writer["depends_on"] = ["manufacturing_research"]
    Draft202012Validator(schema).validate(document)
    parsed = LeadDecision.model_validate(document)
    assert parsed.tasks[1].depends_on == ("manufacturing_research",)
    assert parsed.tasks[0].todo_id == saved_id


def test_short_but_unknown_dependency_still_fails_graph_validation(roster):
    document = plan(task("drive-reader", []))
    document["tasks"][0]["depends_on"] = ["unknown_local_task"]
    Draft202012Validator(planning_result_schema(roster)).validate(document)
    with pytest.raises(PydanticError, match="Unknown task dependency"):
        LeadDecision.model_validate(document)


@pytest.mark.parametrize(
    "status,has_tasks", [("waiting", True), ("complete", True), ("plan", False)]
)
def test_schema_requires_tasks_only_for_plans(roster, status, has_tasks):
    document = {
        "status": status,
        "summary": "A result",
        "tasks": [task("drive-reader", [])] if has_tasks else [],
    }
    with pytest.raises(SchemaError):
        Draft202012Validator(planning_result_schema(roster)).validate(document)


def test_duplicate_tool_permissions_are_normalized_without_widening(roster):
    document = plan(
        task(
            "drive-reader",
            [
                "native.project_file_read",
                "native.project_files_list",
                "native.project_file_read",
                "native.project_file_read",
            ],
        )
    )
    Draft202012Validator(planning_result_schema(roster)).validate(document)
    decision = LeadDecision.model_validate(document)
    assert decision.tasks[0].tool_ids == ("native.project_file_read", "native.project_files_list")


def test_schema_allows_only_explicit_reusable_backlog_ids_or_new_tasks(roster):
    schema = planning_result_schema(roster, ready_todo_ids=("todo-review", "todo-draft"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for identifier in (None, "todo-review", "todo-draft"):
        candidate = task("file-writer", ["native.local_file_write"])
        candidate["todo_id"] = identifier
        validator.validate(plan(candidate))
    for identifier in ("finished-review", "cancelled-draft", "unknown-result", "foreign-todo"):
        candidate = task("file-writer", ["native.local_file_write"])
        candidate["todo_id"] = identifier
        with pytest.raises(SchemaError):
            validator.validate(plan(candidate))
    # Backlog reuse does not let another member borrow the writer's tool.
    candidate = task("drive-reader", ["native.local_file_write"])
    candidate["todo_id"] = "todo-draft"
    with pytest.raises(SchemaError):
        validator.validate(plan(candidate))
    for status in ("waiting", "complete"):
        validator.validate({"status": status, "summary": "No assignments.", "tasks": []})


def test_schema_without_reusable_backlog_requires_null_for_new_tasks(roster):
    validator = Draft202012Validator(planning_result_schema(roster))
    candidate = task("reviewer", [])
    validator.validate(plan(candidate))
    candidate["todo_id"] = "previously-completed"
    with pytest.raises(SchemaError):
        validator.validate(plan(candidate))


def test_reusable_backlog_enum_is_deduplicated_once_and_counts_toward_provider_bound(roster):
    identifier = "one-shared-backlog-id"
    schema = planning_result_schema(roster, ready_todo_ids=[identifier, identifier])
    assert json.dumps(schema).count(identifier) == 1
    assert schema["properties"]["tasks"]["items"]["properties"]["todo_id"]["enum"] == [
        identifier,
        None,
    ]
    with pytest.raises(ValidationError, match="smaller team or skill selection"):
        planning_result_schema(roster, ready_todo_ids=[f"todo-{index}" for index in range(750)])


def test_completed_todo_is_not_reusable_but_remains_a_saved_dependency(tmp_path):
    h = harness(tmp_path)
    backlog_dependencies(h, prerequisite_done=True)
    h.decision["tasks"] = h.decision["tasks"][1:]
    state = planning(h)
    assert state.active_cycle.phase == "ready"
    request = h.store.get_job(state.active_cycle.planning_plan_id).input["request"]["tasks"][0]
    validator = Draft202012Validator(request["final_output_schema"])
    validator.validate(h.decision)
    completed_reuse = deepcopy(h.decision)
    completed_reuse["tasks"][0]["todo_id"] = "saved-research"
    with pytest.raises(SchemaError):
        validator.validate(completed_reuse)
    assert state.todos[0].status == "done" and state.todos[0].cycle_id is None
    assert state.todos[1].depends_on == ("saved-research",)
    executed = h.platform.get(h.actor, state.active_cycle.execution_plan_id)
    assert executed.waves == (("brief",), ("lead-summary",))


def test_begin_captures_one_roster_for_prompt_and_permission_schema(tmp_path):
    h = harness(tmp_path)
    source_tools(h, tmp_path, privacy="local_only")
    state = h.work.request_cycle(h.actor, h.project.id, "Assess local sources", "schema-plan")
    roster_calls = []
    original = h.coordinator._planning_roster

    def roster(actor, current):
        result = original(actor, current)
        roster_calls.append(result)
        return result

    h.coordinator._planning_roster = roster
    h.coordinator.begin(h.actor, h.project.id, state.active_cycle)
    state = h.work.get(h.actor, h.project.id)
    request = h.store.get_job(state.active_cycle.planning_plan_id).input["request"]["tasks"][0]
    assert len(roster_calls) == 1
    assert request["final_output_schema"] == planning_result_schema(roster_calls[0])
    data = json.loads(request["additional_instructions"].split("from this user data:\n", 1)[1])
    serialized = request["additional_instructions"].split("from this user data:\n", 1)[1]
    assert serialized == json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    uncompressed = json.dumps(data, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) < len(uncompressed.encode("utf-8"))
    lead = next(member for member in data["team"] if member["agent_id"] == "lead")
    ready = {tool["id"] for tool in lead["ready_tools"]}
    assert "native.drive_search" not in ready  # local-only privacy
    assert not {"native.unavailable", "native.private", "native.other_agent"} & ready
    validator = Draft202012Validator(request["final_output_schema"])
    validator.validate(plan(task("lead", ["native.local_file_read"])))
    with pytest.raises(SchemaError):
        validator.validate(plan(task("lead", ["native.drive_search"])))


def test_oversized_or_empty_roster_fails_closed_with_actionable_reason():
    with pytest.raises(ValidationError, match="No project team member"):
        planning_result_schema([])
    large = [
        {
            "agent_id": f"agent-{index}",
            "ready_tools": [{"id": f"tool.{tool}"} for tool in range(30)],
        }
        for index in range(32)
    ]
    with pytest.raises(ValidationError, match="smaller team or skill selection"):
        planning_result_schema(large)
