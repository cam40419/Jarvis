"""Calendar occurrences: skip nonexistent wall times, choose the first repeated time."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from simon.domain.project_continuity import ProjectScheduleSpec


def next_occurrence(spec: ProjectScheduleSpec, after: datetime) -> datetime | None:
    """Strictly after the cursor; missed occurrences coalesce instead of flooding the queue."""
    if spec.kind == "once":
        return spec.run_at if spec.run_at is not None and spec.run_at > after else None
    zone = ZoneInfo(spec.timezone)
    hour, minute = map(int, spec.local_time.split(":"))
    first = after.astimezone(zone).date()
    for days in range(15):
        date = first + timedelta(days=days)
        if spec.kind == "weekly" and date.weekday() not in spec.weekdays:
            continue
        wall = datetime.combine(date, time(hour, minute))
        candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
        # A spring-forward gap does not denote an actual instant. Do not silently
        # change the requested hour. A fall-back time runs once, at its first fold.
        if candidate.astimezone(zone).replace(tzinfo=None) != wall:
            continue
        if candidate > after:
            return candidate
    raise ValueError("No calendar occurrence found within the bounded lookahead")
