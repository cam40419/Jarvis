"""Storage history retains exact limits, expiry semantics and immutable object binding."""

from datetime import timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.models import utc_now
from simon.domain.native_storage import MAX_STORAGE_LIMIT_BYTES, StorageCharge, StorageEntitlement


def entitlement(**changes):
    return StorageEntitlement(
        **{
            "workspace_id": uuid4(),
            "version": 1,
            "plan_key": "starter",
            "limit_bytes": 100,
            "reason": "Approved workspace allocation",
            "issued_by": uuid4(),
            **changes,
        }
    )


def test_expired_override_history_is_readable_and_never_extends_its_limit():
    now = utc_now()
    value = entitlement(override_limit_bytes=200, override_expires_at=now)
    assert value.effective_limit(now - timedelta(microseconds=1)) == 200
    assert value.effective_limit(now) == 100
    assert value.effective_limit(now + timedelta(days=1)) == 100
    assert StorageEntitlement.model_validate_json(value.model_dump_json()) == value
    offset = now.astimezone(timezone(timedelta(hours=-4)))
    assert (
        entitlement(override_limit_bytes=0, override_expires_at=offset).override_expires_at == now
    )
    assert entitlement(limit_bytes=0).effective_limit(now) == 0
    assert (
        entitlement(limit_bytes=MAX_STORAGE_LIMIT_BYTES).effective_limit(now)
        == MAX_STORAGE_LIMIT_BYTES
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 0},
        {"version": True},
        {"plan_key": "UNBOUNDED"},
        {"plan_key": "x" * 65},
        {"limit_bytes": -1},
        {"limit_bytes": True},
        {"limit_bytes": MAX_STORAGE_LIMIT_BYTES + 1},
        {"override_limit_bytes": 200},
        {"override_expires_at": utc_now()},
        {"override_limit_bytes": 10, "override_expires_at": utc_now().replace(tzinfo=None)},
        {"reason": ""},
        {"reason": "x" * 1001},
        {"reason": "bad\x00reason"},
    ],
)
def test_entitlement_rejects_invalid_authority_or_unpaired_override(changes):
    with pytest.raises(ValidationError):
        entitlement(**changes)


def test_charge_strict_size_state_and_hash_and_frozen_bindings():
    fields = {
        "workspace_id": uuid4(),
        "project_id": uuid4(),
        "object_kind": "source",
        "object_id": uuid4(),
        "bytes": 0,
        "sha256": "a" * 64,
    }
    value = StorageCharge(**fields)
    assert value.state == "reserved"
    for changes in (
        {"bytes": -1},
        {"bytes": True},
        {"object_kind": "unknown"},
        {"state": "unknown"},
        {"sha256": "A" * 64},
    ):
        with pytest.raises(ValidationError):
            StorageCharge(**{**fields, **changes})
    with pytest.raises(ValidationError):
        value.bytes = 1
