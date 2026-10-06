from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from simon.domain.project_continuity import ProjectScheduleSpec
from simon.services.project_calendar import next_occurrence


def schedule(**changes):
    return ProjectScheduleSpec(
        name="Morning review",
        instruction="Review the retained project work",
        kind="daily",
        timezone="America/New_York",
        local_time="09:00",
        model_budget_usd=1,
        **changes,
    )


def test_daily_calendar_keeps_wall_time_across_dst():
    spec = schedule()
    assert next_occurrence(spec, datetime(2026, 3, 7, 15, tzinfo=UTC)) == datetime(
        2026, 3, 8, 13, tzinfo=UTC
    )
    assert next_occurrence(spec, datetime(2026, 10, 31, 14, tzinfo=UTC)) == datetime(
        2026, 11, 1, 14, tzinfo=UTC
    )


def test_nonexistent_time_skips_day_and_repeated_time_runs_once():
    spring = schedule().model_copy(update={"local_time": "02:30"})
    assert next_occurrence(spring, datetime(2026, 3, 7, 8, tzinfo=UTC)) == datetime(
        2026, 3, 9, 6, 30, tzinfo=UTC
    )
    autumn = schedule().model_copy(update={"local_time": "01:30"})
    first = next_occurrence(autumn, datetime(2026, 10, 31, 8, tzinfo=UTC))
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_occurrence(autumn, first) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_weekly_uses_local_weekday_and_coalesces_downtime():
    spec = schedule().model_copy(update={"kind": "weekly", "weekdays": (0, 4)})
    assert next_occurrence(spec, datetime(2026, 10, 2, 14, tzinfo=UTC)) == datetime(
        2026, 10, 5, 13, tzinfo=UTC
    )


def test_once_is_absolute_and_exhausted_after_occurrence():
    when = datetime(2026, 10, 5, 15, tzinfo=UTC)
    spec = ProjectScheduleSpec(
        name="One review", instruction="Review", kind="once", run_at=when, model_budget_usd=1
    )
    assert next_occurrence(spec, datetime(2026, 10, 1, tzinfo=UTC)) == when
    assert next_occurrence(spec, when) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "not/a/zone"},
        {"kind": "once"},
        {"kind": "weekly"},
        {"weekdays": (0,)},
        {"local_time": "24:00"},
        {"model_budget_usd": 0},
        {"kind": "weekly", "weekdays": (0, 0)},
        {"kind": "weekly", "weekdays": (7,)},
    ],
)
def test_calendar_rejects_ambiguous_or_unbounded_configuration(changes):
    data = schedule().model_dump()
    data.update(changes)
    with pytest.raises(ValidationError):
        ProjectScheduleSpec.model_validate(data)
