"""Model declarations, project grants, secrets and accounting have strict boundaries."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from simon.domain.model_routing import ModelEndpoint
from simon.domain.native_models import (
    EnrollProjectModel,
    ModelResourcePolicy,
    ModelTemplate,
    ModelUsage,
    ProbeProjectModel,
    ProjectModel,
    ProjectModelCredential,
    ReconcileModelUsage,
    UpdateModelResourcePolicy,
    UpdateProjectModel,
)


def usage_values():
    now = datetime(2026, 10, 8, tzinfo=UTC)
    return {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "operation_id": uuid4(),
        "phase": "generation",
        "requested_by": uuid4(),
        "model_id": uuid4(),
        "model_version": 1,
        "template_id": "local-model",
        "model": "weights",
        "endpoint_fingerprint": "a" * 64,
        "reserved_microusd": 100,
        "held_microusd": 100,
        "started_at": now,
        "deadline_at": now + timedelta(minutes=3),
    }


def test_template_requires_explicit_workspace_and_project_credential_placeholder():
    endpoint = ModelEndpoint(
        id="local-model",
        model="weights",
        provider="openai_compatible",
        local=True,
        base_url="http://localhost:1234/v1",
    )
    values = {
        "id": endpoint.id,
        "name": "Local model",
        "workspace_ids": (uuid4(),),
        "endpoint": endpoint,
        "credential_required": False,
    }
    template = ModelTemplate(**values)
    assert template.endpoint == endpoint
    for changes in (
        {"id": "different"},
        {"credential_required": True},
        {"workspace_ids": ()},
        {"workspace_ids": values["workspace_ids"] * 2},
        {"endpoint": endpoint.model_copy(update={"api_key_env": "OPENAI_API_KEY"})},
    ):
        with pytest.raises(ValidationError):
            ModelTemplate(**(values | changes))
    keyed = endpoint.model_copy(update={"api_key_env": "MODEL_PROJECT_KEY"})
    assert ModelTemplate(**(values | {"endpoint": keyed, "credential_required": True}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("lifetime_limit_microusd", True),
        ("daily_limit_microusd", "1"),
        ("monthly_limit_microusd", -1),
        ("per_operation_limit_microusd", 1_000_000_000_001),
        ("max_concurrent_calls", 0),
        ("max_concurrent_calls", 33),
    ],
)
def test_policy_limits_reject_coercions_and_out_of_range_values(field, value):
    with pytest.raises(ValidationError):
        ModelResourcePolicy(workspace_id=uuid4(), **{field: value})


def test_workspace_cannot_grant_project_privileges_or_pin_models():
    for change in (
        {"allow_paid": True},
        {"allow_cloud": True},
        {"planning_model_id": uuid4()},
        {"review_model_id": uuid4()},
    ):
        with pytest.raises(ValidationError):
            ModelResourcePolicy(workspace_id=uuid4(), **change)
        assert ModelResourcePolicy(workspace_id=uuid4(), project_id=uuid4(), **change)
    assert (
        UpdateModelResourcePolicy(
            expected_version=0, idempotency_key="policy-save"
        ).max_concurrent_calls
        == 2
    )


def test_credentials_never_appear_in_command_repr_serialization_or_validation_text():
    secret = "test-sensitive-provider-key"
    command = EnrollProjectModel(
        template_id="local-model",
        label="Personal",
        credential=secret,
        idempotency_key="enroll-model",
    )
    assert isinstance(command.credential, SecretStr)
    assert secret not in repr(command)
    assert secret not in command.model_dump_json()
    assert secret not in str(command.model_dump())
    with pytest.raises(ValidationError) as caught:
        EnrollProjectModel(
            template_id="!", label="", credential=secret, idempotency_key="enroll-model"
        )
    assert secret not in str(caught.value)
    update = UpdateProjectModel(
        expected_version=1, label="Updated", enabled=False, idempotency_key="update-model"
    )
    assert update.credential is None
    assert ProbeProjectModel(expected_version=1, idempotency_key="probe-model")
    credential = ProjectModelCredential(
        workspace_id=uuid4(),
        project_id=uuid4(),
        model_id=uuid4(),
        revision=1,
        encrypted_secret=secret,
        created_by=uuid4(),
    )
    assert secret not in repr(credential)


@pytest.mark.parametrize(
    "changes",
    [
        {"held_microusd": 101},
        {"held_microusd": -1},
        {"reserved_microusd": True},
        {"charged_microusd": 9_223_372_036_854_775_808},
        {"input_rate": "NaN"},
        {"output_rate": "Infinity"},
        {"input_rate": -1},
        {"status": "settled"},
        {"status": "released", "held_microusd": 0, "charged_microusd": 1},
        {"status": "reconciled", "held_microusd": 0},
        {"reconciliation_by": uuid4()},
        {"model_version": 0},
        {"model_id": None},
        {"deadline_at": datetime(2026, 10, 8, tzinfo=UTC)},
        {"finished_at": datetime(2026, 10, 7, tzinfo=UTC)},
        {"started_at": datetime(2026, 10, 8)},
        {"endpoint_snapshot": {"api_key": "must-not-persist"}},
    ],
)
def test_invalid_accounting_and_unsafe_snapshots_are_rejected(changes):
    with pytest.raises(ValidationError):
        ModelUsage(**(usage_values() | changes))


def test_usage_preserves_decimal_rates_utc_periods_and_documented_overruns():
    values = usage_values()
    record = ModelUsage(
        **(
            values
            | {
                "status": "settled",
                "held_microusd": 0,
                "charged_microusd": 125,
                "input_rate": "0.00000125",
                "started_at": values["started_at"].astimezone(timezone(timedelta(hours=-4))),
            }
        )
    )
    assert record.input_rate == Decimal("0.00000125")
    assert record.started_at.tzinfo == UTC
    assert ModelUsage.model_validate_json(record.model_dump_json()) == record
    reconciled = ModelUsage(
        **(
            values
            | {
                "status": "reconciled",
                "held_microusd": 0,
                "charged_microusd": 25,
                "reconciliation_by": uuid4(),
                "reconciliation_at": values["deadline_at"],
                "reconciliation_reason": "Provider reports final usage",
                "reconciliation_evidence": "Invoice record item 1234",
            }
        )
    )
    assert reconciled.reconciliation_by
    with pytest.raises(ValidationError):
        ModelUsage.model_validate(
            reconciled.model_dump()
            | {
                "reconciliation_at": values["started_at"],
            }
        )
    assert ReconcileModelUsage(
        expected_version=1,
        idempotency_key="reconcile-call",
        charged_microusd=25,
        reason="Provider final usage",
        evidence="Invoice record item 1234",
    )


def test_qualification_is_explicit_and_dated():
    values = {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "template_id": "local-model",
        "label": "Local",
        "created_by": uuid4(),
    }
    assert ProjectModel(**values).qualification_status == "unverified"
    for changes in (
        {"qualification_status": "ready"},
        {
            "qualification_status": "ready",
            "qualification_fingerprint": "a" * 64,
            "qualified_at": datetime.now(UTC),
            "qualification_error": "bad_output",
        },
    ):
        with pytest.raises(ValidationError):
            ProjectModel(**(values | changes))
