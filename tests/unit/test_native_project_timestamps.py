"""Persisted records and idempotency receipts have one canonical timestamp encoding."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.domain.native_projects import NativeProject, NativeProjectMember, NativeTask
from simon.services.canonical import digest


@pytest.fixture(params=[NativeProject, NativeProjectMember, NativeTask])
def native_record(request):
    instant = datetime(2026, 10, 7, 16, 34, 56, 123456, tzinfo=UTC)
    shared = {"workspace_id": uuid4(), "created_at": instant}
    if request.param is NativeProject:
        return NativeProject(
            **shared,
            name="Project",
            objective="Review evidence",
            created_by=uuid4(),
            updated_at=instant,
        )
    if request.param is NativeProjectMember:
        return NativeProjectMember(**shared, project_id=uuid4(), actor_id=uuid4())
    return NativeTask(
        **shared,
        project_id=uuid4(),
        title="Review evidence",
        created_by=uuid4(),
        updated_at=instant,
    )


@pytest.mark.parametrize("offset_minutes", [-240, 330])
def test_database_offsets_and_receipt_json_have_identical_utc_encoding(
    native_record, offset_minutes
):
    values = native_record.model_dump()
    for field in ("created_at", "updated_at"):
        if field in values:
            values[field] = values[field].astimezone(timezone(timedelta(minutes=offset_minutes)))
    decoded = type(native_record).model_validate(values)
    assert decoded.created_at.tzinfo is UTC
    assert decoded.model_dump(mode="json") == native_record.model_dump(mode="json")
    assert decoded.model_dump(mode="json")["created_at"] == "2026-10-07T16:34:56.123456Z"
    assert digest(decoded.model_dump(mode="json")) == digest(native_record.model_dump(mode="json"))

    receipt = native_record.model_dump(mode="json")
    for field in ("created_at", "updated_at"):
        if field in values:
            receipt[field] = values[field].isoformat()
    assert (
        type(native_record).model_validate(receipt).model_dump_json()
        == native_record.model_dump_json()
    )


def test_native_records_still_reject_timezone_naive_timestamps(native_record):
    values = native_record.model_dump()
    for field in ("created_at", "updated_at"):
        if field in values:
            invalid = {**values, field: values[field].replace(tzinfo=None)}
            with pytest.raises(ValidationError, match="timezone"):
                type(native_record).model_validate(invalid)
