from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticError

from simon.adapters.memory import InMemoryStore
from simon.agent_setup import starter_manifest
from simon.config import Settings
from simon.domain.agent_platform import AgentProfile, PlatformManifest, TeamTemplate
from simon.domain.agent_profiles import AgentRoleDefinition, CreateAgentProfile, UpdateAgentProfile
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.execution import EnvironmentDefinition
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolDefinition
from simon.services.agent_profiles import AgentProfileService


@pytest.fixture
def actor():
    return ActorContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write", "memories:read", "threads:read"}),
    )


@pytest.fixture
def manifest():
    return starter_manifest(Settings(model_provider="local"))


def request(**changes):
    return CreateAgentProfile(
        **{
            "name": "Collection researcher",
            "description": "Research collection briefs and save a clear launch proposal.",
            "skill_ids": ("local-research", "local-documents"),
            "idempotency_key": "create-role-001",
            **changes,
        }
    )


def change_profile(manifest, identifier, **changes):
    return manifest.model_copy(
        update={
            "agents": tuple(
                profile.model_copy(update=changes) if profile.id == identifier else profile
                for profile in manifest.agents
            )
        }
    )


def test_role_combines_only_selected_skills_and_persists_without_result_on_create(actor, manifest):
    store = InMemoryStore()
    service = AgentProfileService(store, manifest)
    record = service.create(actor, request())
    assert record.name in record.profile.instructions
    assert record.description in record.profile.instructions
    assert record.profile.id == record.id
    assert record.profile.version == record.version == 1
    assert record.profile.max_action == "write"
    assert record.profile.max_steps == 16
    assert set(record.profile.tool_ids) == {
        "native.local_files_roots",
        "native.local_files_list",
        "native.local_files_search",
        "native.local_file_read",
        "native.local_file_write",
        "native.local_file_edit",
        "native.local_folder_create",
        "native.project_list",
    }
    assert record.profile.tool_scopes == {"jobs:read", "jobs:write", "memories:read"}
    assert record.profile.environment_ids == ()
    saved = store.get_job(service._job_id(record.id))
    assert saved.result is None  # PostgreSQL create_job deliberately does not persist result.
    assert saved.input["initial_state"]["record"]["name"] == record.name
    restarted = AgentProfileService(store, manifest)
    assert restarted.get(actor, record.id) == record
    assert restarted.profiles(actor) == (record.profile,)


def test_research_and_writing_skills_work_separately_and_do_not_duplicate_stock_roles(
    actor, manifest
):
    service = AgentProfileService(InMemoryStore(), manifest)
    skills = {item["id"]: item for item in service.skills(actor)}
    assert {
        "local-research",
        "local-documents",
        "drive-research",
        "drive-documents",
    } <= skills.keys()
    assert (
        not {"profile.file-reader", "profile.google-reader", "profile.file-writer"} & skills.keys()
    )
    research = service.create(actor, request(skill_ids=("local-research",)))
    assert research.profile.max_action == "read"
    assert "native.local_file_write" not in research.profile.tool_ids
    writing = service.create(
        actor,
        request(
            skill_ids=("local-documents",),
            idempotency_key="separate-writing",
        ),
    )
    assert "native.local_file_read" in writing.profile.tool_ids
    assert "native.local_file_write" in writing.profile.tool_ids
    assert "native.local_files_search" not in writing.profile.tool_ids


def test_owner_and_workspace_isolation_include_idempotency_namespace(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    record = service.create(actor, request())
    for other in (
        actor.model_copy(update={"actor_id": uuid4()}),
        actor.model_copy(update={"household_id": uuid4()}),
    ):
        assert service.list(other) == ()
        assert service.profiles(other) == ()
        with pytest.raises(NotFoundError):
            service.get(other, record.id)
        with pytest.raises(NotFoundError):
            service.update(
                other,
                record.id,
                UpdateAgentProfile(
                    **request().model_dump(),
                    expected_version=1,
                ),
            )
        assert service.create(other, request()).id != record.id
    assert service.create(actor, request()) == record
    with pytest.raises(IdempotencyConflictError):
        service.create(actor, request(description="Different role"))


def test_versioned_update_replay_does_not_overwrite_a_later_edit(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    created = service.create(actor, request())
    body = UpdateAgentProfile(
        **request(
            name="Launch editor",
            idempotency_key="edit-role-001",
        ).model_dump(),
        expected_version=1,
    )
    changed = service.update(actor, created.id, body)
    assert changed.version == changed.profile.version == 2
    assert service.update(actor, created.id, body) == changed
    with pytest.raises(IdempotencyConflictError):
        service.update(actor, created.id, body.model_copy(update={"name": "Conflicting title"}))
    with pytest.raises(InvalidTransitionError):
        service.update(
            actor, created.id, body.model_copy(update={"idempotency_key": "stale-edit-001"})
        )
    latest = service.update(
        actor,
        created.id,
        body.model_copy(
            update={
                "name": "Collection director",
                "expected_version": 2,
                "idempotency_key": "edit-role-002",
            }
        ),
    )
    assert latest.version == 3
    assert service.update(actor, created.id, body) == changed
    assert service.get(actor, created.id) == latest


@pytest.mark.parametrize("revocation", ["team", "tool", "scope", "write"])
def test_operator_revocations_block_execution_but_keep_saved_role_editable(
    actor,
    manifest,
    revocation,
):
    store = InMemoryStore()
    service = AgentProfileService(store, manifest)
    record = service.create(actor, request())
    source = next(item for item in manifest.agents if item.id == "file-writer")
    if revocation == "team":
        changed = manifest.model_copy(
            update={
                "teams": tuple(
                    team.model_copy(update={"allowed_workspace_ids": frozenset({uuid4()})})
                    for team in manifest.teams
                )
            }
        )
    elif revocation == "tool":
        changed = change_profile(
            manifest,
            source.id,
            tool_ids=tuple(key for key in source.tool_ids if key != "native.local_file_write"),
        )
    elif revocation == "scope":
        changed = change_profile(
            manifest, source.id, tool_scopes=source.tool_scopes - {"jobs:write"}
        )
    else:
        changed = change_profile(manifest, source.id, max_action="read")
    current = AgentProfileService(store, changed)
    blocked = current.get(actor, record.id)
    assert blocked.state == "blocked" and blocked.blocked_reasons
    assert blocked.skill_ids == record.skill_ids
    assert blocked.skills == record.skills
    assert blocked.editable
    assert current.profiles(actor) == ()
    assert current.create(actor, request()).state == "blocked"


def test_actor_scope_revocation_cannot_reuse_saved_privileges(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    record = service.create(actor, request())
    limited = actor.model_copy(update={"scopes": frozenset({"jobs:read"})})
    assert service.get(limited, record.id).state == "blocked"
    assert service.profiles(limited) == ()
    with pytest.raises(AuthorizationError):
        service.create(limited, request(idempotency_key="new-without-write"))
    with pytest.raises(AuthorizationError):
        service.skills(actor.model_copy(update={"scopes": frozenset()}))


def test_later_operator_identifier_collision_blocks_custom_role_even_for_hidden_source(
    actor, manifest
):
    store = InMemoryStore()
    saved = AgentProfileService(store, manifest).create(actor, request())
    private = AgentProfile(
        id=saved.id,
        instructions="Operator-only profile.",
        tool_ids=("native.local_file_write",),
        max_action="write",
        tool_scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    changed = manifest.model_copy(
        update={
            "agents": (*manifest.agents, private),
            "teams": (
                *manifest.teams,
                TeamTemplate(
                    id="private",
                    name="Private",
                    agent_ids=(private.id,),
                    allowed_workspace_ids=frozenset({uuid4()}),
                ),
            ),
        }
    )
    current = AgentProfileService(store, changed)
    assert current.get(actor, saved.id).state == "blocked"
    assert "conflicts" in current.get(actor, saved.id).blocked_reasons[0]
    assert current.profiles(actor) == ()


def test_hidden_team_cannot_supply_custom_skills(actor, manifest):
    hidden = manifest.model_copy(
        update={
            "teams": tuple(
                team.model_copy(update={"allowed_workspace_ids": frozenset({uuid4()})})
                for team in manifest.teams
            )
        }
    )
    service = AgentProfileService(InMemoryStore(), hidden)
    assert service.skills(actor) == []
    with pytest.raises(ValidationError, match="currently granted"):
        service.create(actor, request())


def test_unavailable_provider_skills_can_be_saved_with_honest_readiness(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    skills = {
        skill["id"]: skill
        for skill in service.skills(
            actor,
            [
                {
                    "id": "native.drive_read_file",
                    "state": "unavailable",
                    "blocked_reasons": ["Google account is not connected"],
                }
            ],
        )
    }
    assert skills["drive-research"]["state"] == "unavailable"
    assert "Google account is not connected" in skills["drive-research"]["blocked_reasons"]
    assert skills["coding"]["state"] == "unavailable"
    record = service.create(actor, request(skill_ids=("coding",)))
    assert record.skills[0].state == "unavailable"
    assert record.skills[0].blocked_reasons


def test_analysis_skill_supports_minimal_tool_free_manifests_and_preserves_custom_guidance(actor):
    source = AgentProfile(
        id="analyst",
        instructions="Use only the evidence supplied with the task.",
        privacy="local_only",
        max_steps=4,
    )
    manifest = PlatformManifest(
        agents=(source,),
        teams=(TeamTemplate(id="analysis", name="Analysis", agent_ids=(source.id,)),),
    )
    service = AgentProfileService(InMemoryStore(), manifest)
    record = service.create(actor, request(skill_ids=("analysis",)))
    assert record.profile.tool_ids == ()
    assert record.profile.tool_scopes == frozenset()
    assert record.profile.privacy == "local_only"
    assert record.profile.max_steps == 4
    assert source.instructions in record.profile.instructions


def test_builtin_analysis_does_not_turn_custom_writer_into_dependency_reviewer(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    record = service.create(actor, request(skill_ids=("analysis", "local-documents")))
    assert "Review dependency outputs" not in record.profile.instructions
    assert "Role title: Collection researcher" in record.profile.instructions
    source = next(item for item in manifest.agents if item.id == "reviewer")
    customized = change_profile(
        manifest, source.id, instructions="Never include confidential names."
    )
    custom_service = AgentProfileService(InMemoryStore(), customized)
    custom = custom_service.create(actor, request(skill_ids=("analysis", "local-documents")))
    assert "Never include confidential names." in custom.profile.instructions


def synthetic_manifest(*, conflicting_models=False):
    tools = (
        ToolDefinition(
            id="test.read",
            transport="environment",
            description="Read input",
            required_scopes=frozenset({"jobs:read"}),
        ),
        ToolDefinition(
            id="test.write",
            transport="environment",
            description="Write output",
            side_effect=True,
            action_policy="write",
            required_scopes=frozenset({"jobs:write"}),
        ),
    )
    agents = (
        AgentProfile(
            id="reader",
            instructions="Read source evidence.",
            tool_ids=("test.read",),
            tool_scopes=frozenset({"jobs:read", "jobs:write"}),
            privacy="local_only",
            environment_ids=("read-env",),
            max_steps=7,
            max_tool_calls=12,
            model_override="local",
        ),
        AgentProfile(
            id="writer",
            instructions="Write requested deliverables.",
            tool_ids=("test.write",),
            tool_scopes=frozenset({"jobs:write"}),
            max_action="write",
            max_steps=16,
            environment_ids=("write-env",),
            model_override="remote" if conflicting_models else None,
        ),
    )
    return PlatformManifest(
        agents=agents,
        tools=tools,
        teams=(TeamTemplate(id="team", name="Team", agent_ids=("reader", "writer")),),
        models=(
            ModelEndpoint(
                id="local",
                provider="openai_compatible",
                model="test",
                base_url="http://127.0.0.1:11434/v1",
                local=True,
            ),
            ModelEndpoint(
                id="remote",
                provider="openai_responses",
                model="test",
                base_url="https://api.openai.com/v1",
                api_key_env="TEST_MODEL_KEY",
            ),
        ),
        environments=tuple(
            EnvironmentDefinition(
                id=identifier,
                kind="docker",
                container_image="test:local",
            )
            for identifier in ("read-env", "write-env")
        ),
    )


def test_fallback_skills_combine_environments_but_keep_strongest_privacy_and_limits(actor):
    service = AgentProfileService(InMemoryStore(), synthetic_manifest())
    record = service.create(actor, request(skill_ids=("profile.reader", "profile.writer")))
    assert record.profile.environment_ids == ("read-env", "write-env")
    assert record.profile.privacy == "local_only"
    assert record.profile.model_override == "local"
    assert record.profile.max_steps == 7
    assert record.profile.max_tool_calls == 12
    assert record.profile.tool_scopes == {"jobs:read", "jobs:write"}


def test_conflicting_explicit_models_reject_before_persisting(actor):
    service = AgentProfileService(InMemoryStore(), synthetic_manifest(conflicting_models=True))
    with pytest.raises(ValidationError, match="different models"):
        service.create(actor, request(skill_ids=("profile.reader", "profile.writer")))
    assert service.list(actor) == ()


def test_operator_grant_expansion_never_silently_expands_saved_custom_role(actor):
    manifest = synthetic_manifest()
    store = InMemoryStore()
    service = AgentProfileService(store, manifest)
    saved = service.create(actor, request(skill_ids=("profile.reader",)))
    changed = change_profile(
        manifest,
        "reader",
        tool_ids=("test.read", "test.write"),
        environment_ids=("read-env", "write-env"),
        max_action="write",
        privacy="allow_cloud",
        max_steps=30,
        max_tool_calls=100,
    )
    current = AgentProfileService(store, changed).get(actor, saved.id)
    assert current.profile.tool_ids == ("test.read",)
    assert current.profile.environment_ids == ("read-env",)
    assert current.profile.max_action == "read"
    assert current.profile.privacy == "local_only"
    assert current.profile.max_steps == 7
    assert current.profile.max_tool_calls == 12


@pytest.mark.parametrize("mutation", ["scopes", "side_effect"])
def test_tool_definition_cannot_silently_upgrade_selected_read_skill(actor, mutation):
    manifest = synthetic_manifest()
    manifest = change_profile(manifest, "reader", max_action="write")
    store = InMemoryStore()
    saved = AgentProfileService(store, manifest).create(
        actor, request(skill_ids=("profile.reader",))
    )
    changes = (
        {"required_scopes": frozenset({"jobs:write"})}
        if mutation == "scopes"
        else {
            "side_effect": True,
            "action_policy": "write",
        }
    )
    modified = manifest.model_copy(
        update={
            "tools": tuple(
                item.model_copy(update=changes) if item.id == "test.read" else item
                for item in manifest.tools
            )
        }
    )
    current = AgentProfileService(store, modified)
    assert current.get(actor, saved.id).state == "blocked"
    assert current.profiles(actor) == ()


def test_confirmation_policy_cannot_be_removed_from_saved_custom_skill(actor):
    manifest = synthetic_manifest()
    manifest = manifest.model_copy(
        update={
            "tools": tuple(
                tool.model_copy(update={"action_policy": "external_commitment"})
                if tool.id == "test.write"
                else tool
                for tool in manifest.tools
            )
        }
    )
    store = InMemoryStore()
    saved = AgentProfileService(store, manifest).create(
        actor, request(skill_ids=("profile.writer",))
    )
    changed = manifest.model_copy(
        update={
            "tools": tuple(
                tool.model_copy(update={"action_policy": "write"})
                if tool.id == "test.write"
                else tool
                for tool in manifest.tools
            )
        }
    )
    current = AgentProfileService(store, changed)
    assert current.get(actor, saved.id).state == "blocked"
    assert current.profiles(actor) == ()


def test_selected_tool_cannot_borrow_another_tools_scope_consent(actor):
    manifest = synthetic_manifest()
    manifest = change_profile(
        manifest,
        "reader",
        tool_ids=("test.read", "test.write"),
        max_action="write",
    )
    store = InMemoryStore()
    saved = AgentProfileService(store, manifest).create(
        actor, request(skill_ids=("profile.reader",))
    )
    changed = manifest.model_copy(
        update={
            "tools": tuple(
                tool.model_copy(update={"required_scopes": frozenset({"jobs:read", "jobs:write"})})
                if tool.id == "test.read"
                else tool
                for tool in manifest.tools
            )
        }
    )
    current = AgentProfileService(store, changed)
    assert current.get(actor, saved.id).state == "blocked"
    assert current.profiles(actor) == ()


def test_custom_profile_count_is_bounded_and_existing_profiles_remain_editable(
    actor, manifest, monkeypatch
):
    monkeypatch.setattr("simon.services.agent_profiles.MAX_CUSTOM_AGENTS", 2)
    service = AgentProfileService(InMemoryStore(), manifest)
    first = service.create(actor, request())
    service.create(actor, request(idempotency_key="second-agent-001"))
    with pytest.raises(ValidationError, match="edit an existing"):
        service.create(actor, request(idempotency_key="third-agent-001"))
    updated = service.update(
        actor,
        first.id,
        UpdateAgentProfile(
            **request(idempotency_key="edit-at-capacity").model_dump(),
            expected_version=1,
        ),
    )
    assert updated.version == 2
    assert len(service.list(actor)) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"name": " "},
        {"description": " "},
        {"description": "x" * 4001},
        {"skill_ids": ()},
        {"skill_ids": ("analysis", "analysis")},
        {"skill_ids": tuple(f"skill-{number}" for number in range(129))},
        {"tool_ids": ("test.write",)},
        {"tool_scopes": ("admin",)},
        {"environment_ids": ("host",)},
        {"max_steps": 100},
        {"idempotency_key": "       x"},
    ],
)
def test_request_shape_accepts_role_and_skill_choices_only(changes):
    with pytest.raises(PydanticError):
        request(**changes)


def test_individual_catalog_has_exactly_one_tool_per_skill_and_no_unassigned_tools(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    items = service.individual_skills(actor)
    tools = {tool.id: tool for tool in manifest.tools}
    expected = {
        key
        for source in manifest.agents
        for key in source.tool_ids
        if tools[key].required_scopes <= source.tool_scopes
        and (not tools[key].side_effect or source.max_action == "write")
    }
    atomic = [item for item in items if item["id"] != "analysis"]
    assert {item["id"] for item in atomic} == {"tool." + key for key in expected}
    assert all(item["tool_ids"] == [item["id"][5:]] for item in atomic)
    assert all(item["name"] and item["category"] for item in atomic)
    assert next(item for item in items if item["id"] == "analysis")["tool_ids"] == []
    search = next(item for item in items if item["id"] == "tool.native.local_files_search")
    assert search["name"] == "Search local files"
    assert search["category"] == "Local files"
    assert search["source_agent_ids"] == ["file-reader"]


def test_individual_read_write_selection_does_not_pull_in_neighboring_tools(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    record = service.create(
        actor,
        request(
            skill_ids=(
                "tool.native.local_files_search",
                "tool.native.local_file_read",
                "tool.native.local_file_write",
            )
        ),
    )
    assert set(record.profile.tool_ids) == {
        "native.local_files_search",
        "native.local_file_read",
        "native.local_file_write",
    }
    assert record.profile.tool_scopes == {"jobs:read", "jobs:write"}
    assert "native.local_file_edit" not in record.profile.tool_ids
    assert record.name in record.profile.instructions
    assert record.description in record.profile.instructions
    for source in manifest.agents:
        if source.id in {"file-reader", "file-writer"}:
            assert source.instructions not in record.profile.instructions
    assert "required user confirmation" in record.profile.instructions


def test_native_individual_skill_descriptions_explain_user_actions_without_api_parameters(
    actor,
    manifest,
):
    original_tools = {tool.id: tool.model_dump(mode="json") for tool in manifest.tools}
    service = AgentProfileService(InMemoryStore(), manifest)
    items = {item["id"]: item for item in service.individual_skills(actor)}
    browse = items["tool.native.project_files_list"]
    assert browse["description"] == (
        "Browse files in this project's connected Google Drive folder."
    )
    assert "Google Docs" in items["tool.native.project_file_read"]["description"]
    assert "cells" in items["tool.native.project_sheet_write"]["description"]
    for item in items.values():
        if item["id"].startswith("tool.native."):
            assert not any(
                parameter in item["description"]
                for parameter in (
                    "folder_id",
                    "next_page_token",
                    "tab IDs",
                    "sheet_id",
                    "null",
                )
            )
    assert {tool.id: tool.model_dump(mode="json") for tool in manifest.tools} == original_tools
    assert browse["tool_ids"] == ["native.project_files_list"]


def test_project_capture_reuses_stock_member_id_without_creating_global_agent(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    snapshot = service.capture_role(
        actor,
        "file-reader",
        "Collection author",
        "Research and save collection documents.",
        ("tool.native.local_file_read", "tool.native.local_file_write"),
        7,
    )
    role = AgentRoleDefinition.model_validate(snapshot["definition"])
    assert role.name == "Collection author"
    assert snapshot["profile"]["id"] == "file-reader"
    assert service.list(actor) == ()
    resolved = AgentProfileService(service.store, manifest).resolve_role(actor, snapshot)
    assert resolved.name == "Collection author"
    assert resolved.id == "file-reader"
    assert resolved.version == 7
    assert set(resolved.tool_ids) == {"native.local_file_read", "native.local_file_write"}
    assert resolved.max_action == "write"
    original = next(source for source in manifest.agents if source.id == "file-reader")
    assert original.name == "File researcher"
    assert original.max_action == "read"
    assert "native.local_file_write" not in original.tool_ids


@pytest.mark.parametrize("owner_field", ["actor_id", "household_id"])
def test_project_role_snapshot_is_account_and_workspace_bound(actor, manifest, owner_field):
    service = AgentProfileService(InMemoryStore(), manifest)
    snapshot = service.capture_role(
        actor,
        "file-writer",
        "Editor",
        "Edit requested text.",
        ("tool.native.local_file_edit",),
        1,
    )
    other = actor.model_copy(update={owner_field: uuid4()})
    with pytest.raises(AuthorizationError, match="another account"):
        service.resolve_role(other, snapshot)


def test_atomic_sources_are_deterministic_and_never_switch_after_revocation(actor):
    manifest = synthetic_manifest()
    narrow = manifest.agents[0].model_copy(
        update={
            "id": "narrow",
            "tool_scopes": frozenset({"jobs:read"}),
        }
    )
    manifest = manifest.model_copy(
        update={
            "agents": (*manifest.agents, narrow),
            "teams": (
                TeamTemplate(id="all", name="All", agent_ids=("reader", "writer", "narrow")),
            ),
        }
    )
    service = AgentProfileService(InMemoryStore(), manifest)
    chosen = next(
        item for item in service.individual_skills(actor) if item["id"] == "tool.test.read"
    )
    assert chosen["source_agent_ids"] == ["narrow"]
    reordered = manifest.model_copy(update={"agents": tuple(reversed(manifest.agents))})
    other = AgentProfileService(service.store, reordered)
    chosen_reordered = next(
        item for item in other.individual_skills(actor) if item["id"] == "tool.test.read"
    )
    assert chosen_reordered == chosen
    snapshot = service.capture_role(
        actor, "reader", "Reader", "Read evidence.", ("tool.test.read",), 1
    )
    revoked = change_profile(manifest, "narrow", tool_ids=())
    current = AgentProfileService(service.store, revoked)
    assert any(item["id"] == "tool.test.read" for item in current.individual_skills(actor))
    with pytest.raises(ValidationError, match="revoked"):
        current.resolve_role(actor, snapshot)


def test_atomic_source_requires_real_operator_scope_and_write_grants(actor):
    manifest = synthetic_manifest()
    denied = change_profile(manifest, "writer", max_action="read")
    service = AgentProfileService(InMemoryStore(), denied)
    assert "tool.test.write" not in {item["id"] for item in service.individual_skills(actor)}
    with pytest.raises(ValidationError, match="currently granted"):
        service.capture_role(actor, "writer", "Writer", "Write output.", ("tool.test.write",), 1)
    denied = change_profile(manifest, "reader", tool_scopes=frozenset())
    assert "tool.test.read" not in {
        item["id"] for item in AgentProfileService(InMemoryStore(), denied).individual_skills(actor)
    }


def test_atomic_skill_readiness_uses_only_selected_tools(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    items = {
        item["id"]: item
        for item in service.individual_skills(
            actor,
            [
                {
                    "id": "native.drive_read_file",
                    "state": "unavailable",
                    "blocked_reasons": ["Connect the Google account"],
                }
            ],
        )
    }
    assert items["tool.native.drive_read_file"]["state"] == "unavailable"
    assert items["tool.native.drive_search_files"]["state"] == "configured"
    assert items["tool.native.drive_read_file"]["blocked_reasons"] == ["Connect the Google account"]


def test_project_atomic_role_keeps_customized_operator_instructions(actor, manifest):
    manifest = change_profile(
        manifest,
        "file-reader",
        instructions="Never include customer phone numbers in the output.",
    )
    service = AgentProfileService(InMemoryStore(), manifest)
    snapshot = service.capture_role(
        actor,
        "file-reader",
        "Research writer",
        "Research and write the requested document.",
        ("tool.native.local_file_read", "tool.native.local_file_write"),
        1,
    )
    resolved = service.resolve_role(actor, snapshot)
    assert "Never include customer phone numbers" in resolved.instructions
    assert resolved.tool_ids == ("native.local_file_read", "native.local_file_write")


@pytest.mark.parametrize("change", ["scope", "action"])
def test_project_role_revalidates_exact_tool_policy_on_every_resolution(actor, change):
    manifest = synthetic_manifest()
    service = AgentProfileService(InMemoryStore(), manifest)
    snapshot = service.capture_role(
        actor, "reader", "Reader", "Read evidence.", ("tool.test.read",), 1
    )
    mutation = (
        {"required_scopes": frozenset({"jobs:read", "jobs:write"})}
        if change == "scope"
        else {
            "side_effect": True,
            "action_policy": "write",
        }
    )
    changed = manifest.model_copy(
        update={
            "tools": tuple(
                tool.model_copy(update=mutation) if tool.id == "test.read" else tool
                for tool in manifest.tools
            )
        }
    )
    with pytest.raises((AuthorizationError, ValidationError)):
        AgentProfileService(service.store, changed).resolve_role(actor, snapshot)


def test_project_role_preserves_privacy_and_limits_without_gain_from_source_expansion(actor):
    manifest = synthetic_manifest()
    service = AgentProfileService(InMemoryStore(), manifest)
    snapshot = service.capture_role(
        actor,
        "reader",
        "Research writer",
        "Read and write evidence.",
        ("tool.test.read", "tool.test.write"),
        1,
    )
    changed = change_profile(
        manifest,
        "reader",
        privacy="allow_cloud",
        max_steps=30,
        max_tool_calls=100,
        environment_ids=("read-env", "write-env"),
    )
    current = AgentProfileService(service.store, changed).resolve_role(actor, snapshot)
    assert current.privacy == "local_only"
    assert current.max_steps == 7
    assert current.max_tool_calls == 12
    assert current.environment_ids == ("read-env", "write-env")


def test_atomic_role_supports_large_tool_identifiers_and_128_independent_choices(actor):
    long_id = "a" * 200
    tool_ids = (long_id, *(f"custom.read_{number}" for number in range(127)))
    profile = AgentProfile(
        id="source",
        instructions="Attribute evidence to sources.",
        tool_ids=tool_ids,
        tool_scopes=frozenset({"jobs:read"}),
    )
    manifest = PlatformManifest(
        agents=(profile,),
        teams=(TeamTemplate(id="team", name="Team", agent_ids=("source",)),),
        tools=tuple(
            ToolDefinition(
                id=identifier,
                transport="native",
                description="Read evidence.",
                configured=True,
                required_scopes=frozenset({"jobs:read"}),
            )
            for identifier in tool_ids
        ),
    )
    service = AgentProfileService(InMemoryStore(), manifest)
    skill_ids = tuple("tool." + identifier for identifier in tool_ids)
    definition = AgentRoleDefinition(
        name="Analyst", description="Analyze evidence.", skill_ids=skill_ids
    )
    assert len(definition.skill_ids) == 128
    snapshot = service.capture_role(
        actor,
        "source",
        definition.name,
        definition.description,
        definition.skill_ids,
        1,
    )
    assert set(service.resolve_role(actor, snapshot).tool_ids) == set(tool_ids)


def test_saved_legacy_bundle_ids_still_resolve_for_reusable_and_project_agents(actor, manifest):
    service = AgentProfileService(InMemoryStore(), manifest)
    legacy = service.create(actor, request())
    snapshot = service.capture_role(
        actor,
        "file-writer",
        legacy.name,
        legacy.description,
        legacy.skill_ids,
        2,
    )
    resolved = service.resolve_role(actor, snapshot)
    assert resolved.tool_ids == legacy.profile.tool_ids
    assert service.get(actor, legacy.id).profile == legacy.profile


def test_project_role_rejects_malformed_snapshot_and_does_not_execute_saved_profile_blob(
    actor, manifest
):
    service = AgentProfileService(InMemoryStore(), manifest)
    snapshot = service.capture_role(
        actor,
        "file-reader",
        "Reader",
        "Read evidence.",
        ("tool.native.local_file_read",),
        1,
    )
    mismatched = {**snapshot, "identifier": "file-writer"}
    with pytest.raises(ValidationError, match="does not match"):
        service.resolve_role(actor, mismatched)
    with pytest.raises(ValidationError, match="invalid"):
        service.resolve_role(actor, {"profile": snapshot["profile"]})
    snapshot["profile"]["tool_ids"].append("native.local_file_write")
    assert service.resolve_role(actor, snapshot).tool_ids == ("native.local_file_read",)
