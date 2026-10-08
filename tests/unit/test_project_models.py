"""Tenant-owned keys, real synthetic transport checks and current route authority."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from simon.adapters.memory import InMemoryStore
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.identity import Membership
from simon.domain.model_routing import ModelEndpoint
from simon.domain.models import ActorContext, Channel
from simon.domain.native_models import (
    EnrollProjectModel,
    ModelResourcePolicy,
    ModelTemplate,
    ProbeProjectModel,
    ProjectModel,
    ProjectModelCredential,
    UpdateProjectModel,
)
from simon.domain.native_projects import NativeProjectMember
from simon.services.identity import ROLE_SCOPES
from simon.services.model_usage import ModelUsageService
from simon.services.project_models import (
    ProjectModelService,
    ScopedModelSecrets,
    configured_templates,
)
from tests.unit.test_native_teams import create_role, issue, setup_team


def template(workspace, *, identifier="local", key=False, cloud=False, paid=False, **changes):
    return ModelTemplate(
        id=identifier,
        name=identifier.title(),
        workspace_ids=(workspace,),
        credential_required=key,
        endpoint=ModelEndpoint(
            id=identifier,
            provider="openai_compatible",
            model="synthetic-model",
            base_url="https://models.example/v1" if cloud else "http://127.0.0.1:8123/v1",
            local=not cloud,
            api_key_env="MODEL_PROJECT_KEY" if key else None,
            input_cost_per_million_usd=1 if paid else 0,
            output_cost_per_million_usd=2 if paid else 0,
            **changes,
        ),
    )


def setup_models(*, key=False, cloud=False, paid=False):
    h = setup_team(InMemoryStore())
    h.cipher = Fernet(Fernet.generate_key())
    h.templates = [template(h.workspace, key=key, cloud=cloud, paid=paid)]
    h.calls, h.hook = [], None
    h.response = '{"simon_model_probe":true,"version":1}'
    h.reported_model = None
    h.tokens = {"prompt_tokens": 100, "completion_tokens": 10}

    def response(request):
        h.calls.append(request)
        if h.hook:
            h.hook(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": h.response}}],
                "usage": h.tokens,
                "model": h.reported_model,
            },
        )

    h.usage = ModelUsageService(h.store, h.projects)
    h.models = ProjectModelService(
        h.store,
        h.projects,
        lambda _: tuple(h.templates),
        lambda: h.cipher,
        h.usage,
        transport=httpx.MockTransport(response),
    )
    if paid or cloud:
        for project_id in (None, h.first.id):
            h.store.save_model_resource_policy(
                ModelResourcePolicy(
                    workspace_id=h.workspace,
                    project_id=project_id,
                    version=1,
                    lifetime_limit_microusd=1_000_000,
                    daily_limit_microusd=1_000_000,
                    monthly_limit_microusd=1_000_000,
                    per_operation_limit_microusd=1_000_000,
                    allow_paid=paid if project_id else False,
                    allow_cloud=cloud if project_id else False,
                ),
                expected_version=0,
            )
    return h


def enroll(h, **changes):
    command = EnrollProjectModel(
        **{
            "template_id": h.templates[0].id,
            "label": "Project model",
            "idempotency_key": f"enroll-{uuid4()}",
            **changes,
        }
    )
    return h.models.enroll(h.owner_actor, h.first.id, command)


def check(h, row, **changes):
    return h.models.probe(
        h.owner_actor,
        h.first.id,
        UUID(row["id"]),
        ProbeProjectModel(
            **{
                "expected_version": row["version"],
                "idempotency_key": f"probe-{uuid4()}",
                **changes,
            }
        ),
    )


def update(h, row, **changes):
    return h.models.update(
        h.owner_actor,
        h.first.id,
        UUID(row["id"]),
        UpdateProjectModel(
            **{
                "expected_version": row["version"],
                "idempotency_key": f"update-{uuid4()}",
                "label": row["label"],
                "enabled": row["enabled"],
                **changes,
            }
        ),
    )


@pytest.fixture
def models():
    return setup_models()


def test_local_qualification_and_selected_runtime_are_real_bounded_calls(models):
    h = models
    enrolled = enroll(h)
    assert not h.calls and not enrolled["ready"]
    assert enrolled["qualification_reservation_microusd"] == 0
    with pytest.raises(ValidationError, match="qualified"):
        h.models.runtime(h.owner_actor, h.first.id)
    ready = check(h, enrolled)
    assert ready["ready"] and ready["qualification_status"] == "ready"
    assert len(h.calls) == 1
    request = json.loads(h.calls[0].content)
    assert request["max_tokens"] == 512 and request["stream"] is False
    assert "Authorization" not in h.calls[0].headers
    runtime = h.models.runtime(h.owner_actor, h.first.id)
    assert runtime.planning_endpoint_id == runtime.review_endpoint_id
    assert runtime.environ == {}
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    assert usage.status == "settled" and usage.input_tokens == 100
    assert usage.output_tokens == 10 and usage.charged_microusd == 0
    view = h.models.view(h.owner_actor, h.first.id)
    assert view["models"][0]["ready"] and view["can_manage"]


def test_project_keys_are_encrypted_scoped_and_never_use_process_fallback(monkeypatch):
    monkeypatch.setenv("MODEL_PROJECT_KEY", "wrong-global-key")
    monkeypatch.setenv("SIMON_OPENAI_API_KEY", "wrong-openai-key")
    h = setup_models(key=True, cloud=True, paid=True)
    row = enroll(h, credential="sk-private-enrolled-key")
    stored = h.store.project_model_credential(h.workspace, h.first.id, UUID(row["id"]), 1)
    assert stored is not None
    assert "sk-private-enrolled-key" not in stored.model_dump_json()
    assert "encrypted_secret" not in row and "api_key_env" not in json.dumps(row)
    ready = check(h, row)
    assert ready["ready"]
    assert h.calls[0].headers["Authorization"] == "Bearer sk-private-enrolled-key"
    runtime = h.models.runtime(h.owner_actor, h.first.id)
    assert "sk-private-enrolled-key" not in repr(runtime)
    assert "sk-private-enrolled-key" not in json.dumps(h.models.view(h.owner_actor, h.first.id))
    assert all(
        "sk-private-enrolled-key" not in event.model_dump_json()
        for event in h.store.audit_events(h.workspace)
    )
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    assert usage.charged_microusd == 120 and usage.held_microusd == 0
    assert row["qualification_reservation_microusd"] >= usage.charged_microusd


@pytest.mark.parametrize("field", ["workspace_id", "project_id", "model_id", "revision"])
def test_ciphertext_cannot_be_swapped_between_scopes_or_revisions(field):
    secrets = ScopedModelSecrets(lambda: Fernet(b"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="))
    workspace, project, model = uuid4(), uuid4(), uuid4()
    encrypted = secrets.encrypt(workspace, project, model, 1, SecretStr("private-key"))
    row = ProjectModelCredential(
        workspace_id=workspace,
        project_id=project,
        model_id=model,
        revision=1,
        encrypted_secret=encrypted,
        created_by=uuid4(),
    )
    assert secrets.decrypt(row) == "private-key"
    with pytest.raises(ValidationError, match="unavailable"):
        secrets.decrypt(row.model_copy(update={field: 2 if field == "revision" else uuid4()}))


@pytest.mark.parametrize("secret", ["", "a b", "secret\n", "\x00secret", "a" * 4097, "\ud800"])
def test_invalid_credentials_fail_safely_without_echo(secret):
    with pytest.raises(ValidationError) as exc:
        ScopedModelSecrets.validate(SecretStr(secret))
    assert "provider credential" in str(exc.value)


@pytest.mark.parametrize("body", ["not-token", "gAAAAA", ""])
def test_corrupt_or_wrong_master_key_is_not_exposed(body):
    h = setup_models(key=True)
    row = enroll(h, credential="private-key")
    stored = h.store.project_model_credential(h.workspace, h.first.id, UUID(row["id"]), 1)
    with pytest.raises(ValidationError, match="unavailable"):
        h.models.secrets.decrypt(stored.model_copy(update={"encrypted_secret": body}))
    h.cipher = Fernet(Fernet.generate_key())
    with pytest.raises(ValidationError, match="unavailable"):
        h.models.secrets.decrypt(stored)


def test_secret_receipt_distinguishes_changed_key_and_checks_authority_before_replay():
    h = setup_models(key=True)
    command = EnrollProjectModel(
        template_id="local", label="Enrolled", credential="first-key", idempotency_key="known-write"
    )
    first = h.models.enroll(h.owner_actor, h.first.id, command)
    assert h.models.enroll(h.owner_actor, h.first.id, command) == first
    assert h.models.operation_receipt(h.owner_actor, h.first.id, "known-write") == first
    with pytest.raises(IdempotencyConflictError):
        h.models.enroll(
            h.owner_actor,
            h.first.id,
            command.model_copy(update={"credential": SecretStr("different-key")}),
        )
    with pytest.raises(NotFoundError):
        h.models.operation_receipt(h.owner_actor, h.second.id, "known-write")
    h.store.put_membership(Membership(actor_id=h.owner, workspace_id=h.workspace, role="guest"))
    with pytest.raises(AuthorizationError):
        h.models.operation_receipt(h.owner_actor, h.first.id, "known-write")
    with pytest.raises(AuthorizationError):
        h.models.enroll(h.owner_actor, h.first.id, command)


def test_rotate_disable_and_restore_require_new_qualification():
    h = setup_models(key=True)
    row = check(h, enroll(h, credential="first-key"))
    binding = h.models.resolve(h.owner_actor, h.first.id, UUID(row["id"]))
    changed = update(h, row, credential="second-key")
    assert not changed["ready"] and changed["credential_revision"] == 2
    with pytest.raises(InvalidTransitionError):
        h.models.assert_current(h.owner_actor, h.first.id, binding)
    assert check(h, changed)["ready"]
    assert h.calls[-1].headers["Authorization"] == "Bearer second-key"
    current = h.models.view(h.owner_actor, h.first.id)["models"][0]
    disabled = update(h, current, enabled=False)
    assert not disabled["ready"]
    restored = update(h, disabled, enabled=True)
    assert restored["qualification_status"] == "unverified" and not restored["ready"]


def test_label_change_keeps_evidence_but_fences_inflight_binding(models):
    h = models
    row = check(h, enroll(h))
    binding = h.models.resolve(h.owner_actor, h.first.id, UUID(row["id"]))
    changed = update(h, row, label="New name")
    assert changed["ready"]
    with pytest.raises(InvalidTransitionError):
        h.models.assert_current(h.owner_actor, h.first.id, binding)


@pytest.mark.parametrize("change", ["model", "price", "grant", "removed"])
def test_catalog_changes_invalidate_qualified_binding(models, change):
    h = models
    check(h, enroll(h))
    original = h.templates[0]
    if change == "removed":
        h.templates = []
    elif change == "grant":
        h.templates = [original.model_copy(update={"workspace_ids": (uuid4(),)})]
    else:
        field, value = (
            ("model", "different-model") if change == "model" else ("input_cost_per_million_usd", 1)
        )
        h.templates = [
            original.model_copy(
                update={"endpoint": original.endpoint.model_copy(update={field: value})}
            )
        ]
    assert not h.models.view(h.owner_actor, h.first.id)["models"][0]["ready"]
    with pytest.raises(ValidationError):
        h.models.runtime(h.owner_actor, h.first.id)
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        "```json\n{}\n```",
        '{"simon_model_probe":1,"version":1}',
        '{"simon_model_probe":true,"version":true}',
        '{"simon_model_probe":true,"version":1,"extra":0}',
        '{"simon_model_probe":false,"simon_model_probe":true,"version":1}',
        "x" * 5000,
        "[" * 1800 + "]" * 1800,
    ],
)
def test_invalid_qualification_results_do_not_qualify_but_known_usage_settles(text):
    h = setup_models(key=True, cloud=True, paid=True)
    h.response = text
    checked = check(h, enroll(h, credential="private-key"))
    assert checked["qualification_status"] == "failed" and not checked["ready"]
    assert checked["qualification_error"] == "qualification_output_invalid"
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    assert usage.status == "settled" and usage.charged_microusd == 120


@pytest.mark.parametrize("failure", ["timeout", "missing_usage", "overuse"])
def test_uncertain_or_overreported_usage_blocks_qualification_and_retains_cost(failure):
    h = setup_models(key=True, cloud=True, paid=True)
    if failure == "timeout":

        def timeout(request):
            raise httpx.ReadTimeout("private-provider-error", request=request)

        h.hook = timeout
    elif failure == "missing_usage":
        h.tokens = {}
    else:
        h.tokens = {"prompt_tokens": 90000, "completion_tokens": 5000}
    checked = check(h, enroll(h, credential="private-key"))
    assert checked["qualification_status"] == "failed"
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    if failure == "overuse":
        assert usage.charged_microusd == 100000 and usage.held_microusd == 0
    else:
        assert usage.status == "unknown" and usage.held_microusd == usage.reserved_microusd
    assert "private-provider-error" not in json.dumps(checked)


def test_probe_replay_never_resends_and_different_request_conflicts(models):
    h = models
    row = enroll(h)
    first = check(h, row, idempotency_key="single-probe")
    assert check(h, row, idempotency_key="single-probe") == first
    assert len(h.calls) == 1
    with pytest.raises(IdempotencyConflictError):
        check(h, first, idempotency_key="single-probe")


@pytest.mark.parametrize("change", ["key", "template", "owner"])
def test_inflight_authority_changes_preserve_cost_without_qualifying_stale_key(change):
    h = setup_models(key=True, cloud=True, paid=True)
    row = enroll(h, credential="first-key")

    def mutate(_request):
        if change == "key":
            update(h, row, credential="replacement-key")
        elif change == "template":
            h.templates = []
        else:
            h.store.put_membership(
                Membership(actor_id=h.owner, workspace_id=h.workspace, role="guest")
            )

    h.hook = mutate
    if change == "owner":
        with pytest.raises(AuthorizationError):
            check(h, row)
    else:
        assert not check(h, row)["ready"]
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    assert usage.status == "settled" and usage.charged_microusd == 120
    assert (
        h.store.project_model(h.workspace, h.first.id, UUID(row["id"])).qualification_status
        == "unverified"
    )


def test_no_paid_or_cloud_grant_no_probe_dispatch():
    h = setup_models(key=True, cloud=True, paid=True)
    row = enroll(h, credential="private-key")
    existing = h.store.model_resource_policy(h.workspace, h.first.id)
    h.store.save_model_resource_policy(
        existing.model_copy(update={"allow_cloud": False, "version": 2}), expected_version=1
    )
    with pytest.raises(ValidationError, match="cloud"):
        check(h, row)
    h.store.save_model_resource_policy(
        existing.model_copy(update={"allow_paid": False, "version": 3}), expected_version=2
    )
    with pytest.raises(ValidationError, match="paid"):
        check(h, row)
    assert not h.calls and not h.store.model_usage_entries(h.workspace, h.first.id)


def test_pinned_routes_cannot_silently_fallback_and_review_can_be_separate(models):
    h = models
    h.templates.append(template(h.workspace, identifier="reviewer"))
    first = check(h, enroll(h))
    reviewer = check(h, enroll(h, template_id="reviewer", label="Reviewer"))
    policy = ModelResourcePolicy(
        workspace_id=h.workspace,
        project_id=h.first.id,
        version=1,
        planning_model_id=UUID(first["id"]),
        review_model_id=UUID(reviewer["id"]),
    )
    h.store.save_model_resource_policy(policy, expected_version=0)
    runtime = h.models.runtime(h.owner_actor, h.first.id)
    assert runtime.planning_endpoint_id != runtime.review_endpoint_id
    update(h, reviewer, enabled=False)
    with pytest.raises(ValidationError, match="selected"):
        h.models.runtime(h.owner_actor, h.first.id)


def test_agents_and_nonowners_cannot_enroll_probe_or_retrieve_receipts(models):
    h = models
    row = enroll(h)
    role = create_role(h)
    agent, _, _ = issue(h, role)
    with pytest.raises(AuthorizationError):
        h.models.view(agent, h.first.id)
    h.store.put_native_project_member(
        NativeProjectMember(workspace_id=h.workspace, project_id=h.first.id, actor_id=h.member)
    )
    member = ActorContext(
        actor_id=h.member,
        workspace_id=h.workspace,
        scopes=ROLE_SCOPES["member"],
        channel=Channel.API,
    )
    assert h.models.view(member, h.first.id)["can_manage"] is False
    with pytest.raises(AuthorizationError):
        h.models.operation_receipt(member, h.first.id, "anything")
    with pytest.raises(AuthorizationError):
        h.models.probe(
            member,
            h.first.id,
            UUID(row["id"]),
            ProbeProjectModel(expected_version=1, idempotency_key="probe-member"),
        )
    with pytest.raises(AuthorizationError):
        h.models.view(h.owner_actor.model_copy(update={"channel": Channel.CHAT}), h.first.id)


def test_missing_and_unnecessary_keys_fail_without_persistence(models):
    with pytest.raises(ValidationError, match="credential"):
        enroll(models, credential="unnecessary-key")
    h = setup_models(key=True)
    with pytest.raises(ValidationError, match="credential"):
        enroll(h)
    assert not h.store.project_models(h.workspace, h.first.id)
    with pytest.raises(NotFoundError):
        h.models.operation_receipt(h.owner_actor, h.first.id, "unknown")


def test_catalog_is_strict_bounded_and_scoped(tmp_path):
    workspace, other = uuid4(), uuid4()
    path = tmp_path / "catalog.json"
    local = template(workspace)
    path.write_text(json.dumps([local.model_dump(mode="json")]), encoding="utf-8")
    assert configured_templates(path, workspace) == (local,)
    assert configured_templates(path, other) == ()
    assert configured_templates(None, workspace) == ()
    for invalid in (
        "{}",
        "[{}]",
        "[] " + " " * 1024 * 1024,
        json.dumps([local.model_dump(mode="json")] * 2),
        '[{"id":"one","id":"two"}]',
        "[" * 1800 + "]" * 1800,
    ):
        path.write_text(invalid, encoding="utf-8")
        with pytest.raises(ValidationError, match="catalog"):
            configured_templates(path, workspace)
    with pytest.raises(ValidationError):
        configured_templates(tmp_path / "missing.json", workspace)


def test_catalog_failure_is_visible_without_disclosing_configuration(models):
    h = models
    enroll(h)

    def unavailable(_workspace):
        raise ValidationError("The administrator's model catalog is unavailable or invalid.")

    h.models.templates_factory = unavailable
    view = h.models.view(h.owner_actor, h.first.id)
    assert view["configuration_error"] and view["templates"] == []
    assert not view["models"][0]["ready"]


def test_probe_replay_while_provider_waits_cannot_issue_second_request(models):
    h = models
    row = enroll(h)
    entered, resume = Event(), Event()

    def hold(_request):
        entered.set()
        assert resume.wait(10)

    h.hook = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        call = pool.submit(check, h, row, idempotency_key="concurrent-check")
        assert entered.wait(10)
        replay = check(h, row, idempotency_key="concurrent-check")
        assert replay["qualification_status"] == "unverified"
        assert len(h.calls) == 1
        resume.set()
        assert call.result(timeout=10)["ready"]
    assert len(h.store.model_usage_entries(h.workspace, h.first.id)) == 1


def test_failure_before_dispatch_releases_hold_without_qualifying(monkeypatch):
    h = setup_models(key=True, cloud=True, paid=True)
    row = enroll(h, credential="private-key")

    def unavailable(*_args):
        raise InvalidTransitionError("Configuration changed")

    monkeypatch.setattr(h.models, "assert_current", unavailable)
    result = check(h, row)
    assert not result["ready"] and not h.calls
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    assert usage.status == "released" and usage.held_microusd == 0
    assert usage.charged_microusd == 0


def test_probe_budget_failure_leaves_no_receipt_or_usage():
    h = setup_models(key=True, cloud=True, paid=True)
    row = enroll(h, credential="private-key")
    old = h.store.model_resource_policy(h.workspace, None)
    h.store.save_model_resource_policy(
        old.model_copy(update={"version": 2, "per_operation_limit_microusd": 1}),
        expected_version=1,
    )
    with pytest.raises(InvalidTransitionError):
        check(h, row, idempotency_key="budget-check")
    assert not h.calls and not h.store.model_usage_entries(h.workspace, h.first.id)
    namespace = h.models._namespace(h.owner_actor, h.first.id) + ":probe"
    assert h.store.command_receipt(namespace, "budget-check") is None


@pytest.mark.parametrize("bad", ["prices", "text", "output", "disabled"])
def test_unusable_template_blocks_probe_before_any_request(bad):
    h = setup_models(key=True, cloud=True)
    row = enroll(h, credential="private-key")
    changes = {
        "prices": {"input_cost_per_million_usd": None},
        "text": {"capabilities": frozenset({"images"})},
        "output": {"max_output_tokens": 16},
        "disabled": {"enabled": False},
    }[bad]
    h.templates[0] = h.templates[0].model_copy(
        update={
            "endpoint": h.templates[0].endpoint.model_copy(update=changes),
        }
    )
    with pytest.raises(ValidationError):
        check(h, row)
    assert not h.calls and not h.store.model_usage_entries(h.workspace, h.first.id)


def test_catalog_key_policy_change_requires_actual_enrollment(models):
    h = models
    row = check(h, enroll(h))
    h.templates[0] = h.templates[0].model_copy(
        update={
            "credential_required": True,
            "endpoint": h.templates[0].endpoint.model_copy(
                update={"api_key_env": "MODEL_PROJECT_KEY"}
            ),
        }
    )
    with pytest.raises(ValidationError, match="Enroll"):
        check(h, row)
    assert not h.models.view(h.owner_actor, h.first.id)["models"][0]["ready"]


def test_connection_limit_and_update_guards(models):
    h = models
    row = enroll(h)
    with pytest.raises(ValidationError, match="does not accept"):
        update(h, row, credential="extra-key")
    with pytest.raises(InvalidTransitionError):
        update(h, row, expected_version=2)
    for index in range(99):
        h.store.insert_project_model(
            ProjectModel(
                workspace_id=h.workspace,
                project_id=h.first.id,
                template_id="local",
                label=f"Model {index}",
                created_by=h.owner,
            )
        )
    with pytest.raises(ValidationError, match="maximum"):
        enroll(h)
    assert len(h.store.project_models(h.workspace, h.first.id)) == 100


def test_credential_rotation_receipt_is_safe_and_changed_key_conflicts():
    h = setup_models(key=True)
    row = enroll(h, credential="first-key")
    rotated = update(h, row, credential="second-key", idempotency_key="rotate-key")
    assert update(h, row, credential="second-key", idempotency_key="rotate-key") == rotated
    with pytest.raises(IdempotencyConflictError):
        update(h, row, credential="third-key", idempotency_key="rotate-key")
    receipt = h.models.operation_receipt(h.owner_actor, h.first.id, "rotate-key")
    assert receipt == rotated and "second-key" not in json.dumps(receipt)
    assert len(h.store.project_models(h.workspace, h.first.id)) == 1


@pytest.mark.parametrize("change", ["wrong_kind", "extra", "invalid_secret", "not_object"])
def test_authenticated_but_malformed_envelopes_fail_closed(change):
    cipher = Fernet(Fernet.generate_key())
    secrets = ScopedModelSecrets(lambda: cipher)
    workspace, project, model = uuid4(), uuid4(), uuid4()
    good = secrets.encrypt(workspace, project, model, 1, SecretStr("private-key"))
    document = json.loads(cipher.decrypt(good.encode()))
    if change == "wrong_kind":
        document["kind"] = "unscoped"
    elif change == "extra":
        document["admin"] = True
    elif change == "invalid_secret":
        document["secret"] = 5
    else:
        document = []
    record = ProjectModelCredential(
        workspace_id=workspace,
        project_id=project,
        model_id=model,
        revision=1,
        encrypted_secret=cipher.encrypt(json.dumps(document).encode()).decode(),
        created_by=uuid4(),
    )
    with pytest.raises(ValidationError, match="unavailable"):
        secrets.decrypt(record)
    with pytest.raises(ValidationError, match="revision"):
        secrets.encrypt(workspace, project, model, 0, SecretStr("private-key"))


def test_readonly_members_get_safe_route_summary_and_pause_visibility(models):
    h = models
    row = check(h, enroll(h))
    h.store.put_native_project_member(
        NativeProjectMember(workspace_id=h.workspace, project_id=h.first.id, actor_id=h.member)
    )
    member = ActorContext(
        actor_id=h.member,
        workspace_id=h.workspace,
        scopes=ROLE_SCOPES["member"],
        channel=Channel.API,
    )
    view = h.models.view(member, h.first.id)
    assert view["routing"]["ready"] and view["routing"]["planning_model_id"] == row["id"]
    assert view["routing"]["planning_label"] == row["label"]
    assert view["templates"][0]["qualification_reservation_microusd"] == 0
    h.store.save_model_resource_policy(
        ModelResourcePolicy(
            workspace_id=h.workspace,
            project_id=None,
            paused=True,
            version=1,
        ),
        expected_version=0,
    )
    assert not h.models.view(member, h.first.id)["routing"]["ready"]
    with pytest.raises(ValidationError, match="paused"):
        h.models.runtime(h.owner_actor, h.first.id)


def test_symlink_catalog_is_rejected(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("[]", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("The host does not permit symlink creation")
    with pytest.raises(ValidationError):
        configured_templates(link, uuid4())


def test_provider_version_alias_is_recorded_without_discarding_known_usage():
    h = setup_models(key=True, cloud=True, paid=True)
    h.reported_model = "synthetic-model-2026-10-08"
    result = check(h, enroll(h, credential="private-key"))
    assert result["ready"]
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    assert usage.status == "settled" and usage.charged_microusd == 120
    assert usage.model == "synthetic-model"
    assert usage.reported_model == "synthetic-model-2026-10-08"


def test_qualification_respects_small_endpoint_output_limit_and_exact_estimate():
    h = setup_models(key=True, cloud=True, paid=True)
    h.templates[0] = h.templates[0].model_copy(
        update={
            "endpoint": h.templates[0].endpoint.model_copy(update={"max_output_tokens": 128}),
        }
    )
    row = enroll(h, credential="private-key")
    result = check(h, row)
    assert result["ready"]
    assert json.loads(h.calls[0].content)["max_tokens"] == 128
    usage = h.store.model_usage_entries(h.workspace, h.first.id)[0]
    assert usage.reserved_microusd == row["qualification_reservation_microusd"]
    assert (
        h.models.view(h.owner_actor, h.first.id)["templates"][0][
            "qualification_reservation_microusd"
        ]
        == usage.reserved_microusd
    )
