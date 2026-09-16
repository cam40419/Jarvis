"""Read-only metering, durable poll claims, and bounded dashboard-ready history."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from itertools import pairwise

from simon.adapters.google import ConnectedError
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.home import HomeDevice, PowerSample
from simon.domain.models import ActorContext, utc_now
from simon.services.conversations import ConversationService
from simon.services.home import HomeService


class PowerMonitor:
    def __init__(self, home: HomeService) -> None:
        self.home, self.store, self.settings = home, home.store, home.settings

    def devices(self, actor: ActorContext) -> tuple[HomeDevice, ...]:
        ConversationService.authorize(actor, "home:read")
        return tuple(
            d
            for d in self.home.catalog.devices(actor.household_id, self.home.devices)
            if d.provider == "shelly"
        )

    def poll(self, actor: ActorContext) -> tuple[PowerSample, ...]:
        devices = self.devices(actor)

        def sample(device: HomeDevice) -> PowerSample:
            now = utc_now()
            with self.store.transaction(actor.household_id):
                previous = self.store.power_samples(
                    actor.household_id,
                    device.id,
                    now - timedelta(days=366),
                    now + timedelta(seconds=1),
                    limit=1,
                )
                if (
                    previous
                    and previous[0].captured_at
                    + timedelta(seconds=self.settings.power_poll_seconds)
                    > now
                ):
                    return previous[0]
                record = PowerSample(
                    household_id=actor.household_id, device_id=device.id, captured_at=now
                )
                self.store.save_power_sample(record)  # Commit the claim before LAN I/O.
            try:
                reading = self.home.shelly.meter(device)
                record = record.model_copy(update={"state": "ok", "reading": reading})
            except Exception as error:
                record = record.model_copy(
                    update={
                        "state": "unavailable",
                        "error": str(error)
                        if isinstance(error, ConnectedError)
                        else "Outlet metering is unavailable.",
                    }
                )
            self.store.save_power_sample(record)
            return record

        with ThreadPoolExecutor(max_workers=4) as pool:
            result = tuple(pool.map(sample, devices))
        self.store.prune_power_samples(
            actor.household_id, utc_now() - timedelta(days=self.settings.power_retention_days)
        )
        return result

    def overview(self, actor: ActorContext) -> dict[str, object]:
        now, result = utc_now(), []
        for device in self.devices(actor):
            rows = self.store.power_samples(
                actor.household_id,
                device.id,
                now - timedelta(days=366),
                now + timedelta(seconds=1),
                1,
            )
            latest = rows[0] if rows else None
            stale = (
                not latest
                or latest.state != "ok"
                or (now - latest.captured_at).total_seconds()
                > self.settings.power_poll_seconds * 2.5
            )
            result.append(
                {
                    "device_id": device.id,
                    "name": device.name,
                    "room": device.room,
                    "stale": bool(stale),
                    "latest": latest.model_dump(mode="json") if latest else None,
                }
            )
        return {
            "poll_seconds": self.settings.power_poll_seconds,
            "retention_days": self.settings.power_retention_days,
            "monitoring_enabled": self.settings.power_monitoring_enabled,
            "devices": result,
        }

    def history(
        self, actor: ActorContext, device_id: str, start: datetime, end: datetime
    ) -> dict[str, object]:
        device = self.home.device(actor, device_id)
        if device.provider != "shelly":
            raise AuthorizationError("Power history is available for Shelly outlets only.")
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or end <= start
            or end - start > timedelta(days=1)
            or end > utc_now() + timedelta(seconds=5)
        ):
            raise ValidationError("Use an explicit timezone and a past window of at most 24 hours.")
        samples = tuple(
            reversed(self.store.power_samples(actor.household_id, device_id, start, end))
        )
        energy, intervals, covered, resets, gaps = 0.0, 0, 0.0, 0, 0
        for previous, current in pairwise(samples):
            a, b = previous.reading, current.reading
            elapsed = (current.captured_at - previous.captured_at).total_seconds()
            if previous.state != "ok" or current.state != "ok" or not a or not b:
                gaps += 1
                continue
            reset = (
                a.energy_wh is not None and b.energy_wh is not None and b.energy_wh < a.energy_wh
            )
            if a.uptime_seconds is not None and b.uptime_seconds is not None:
                reset = reset or b.uptime_seconds + 2 < a.uptime_seconds + elapsed
            if reset:
                resets += 1
                continue
            if (
                elapsed > self.settings.power_poll_seconds * 2.5
                or a.energy_wh is None
                or b.energy_wh is None
            ):
                gaps += 1
                continue
            energy += b.energy_wh - a.energy_wh
            intervals += 1
            covered += elapsed
        return {
            "device_id": device_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "samples": [s.model_dump(mode="json") for s in samples],
            "summary": {
                "observed_energy_wh": energy if intervals else None,
                "observed_energy_kwh": energy / 1000 if intervals else None,
                "covered_seconds": covered,
                "requested_seconds": (end - start).total_seconds(),
                "counter_resets": resets,
                "gaps": gaps,
                "sample_limit_reached": len(samples) == 3000,
                "note": "Consumption covers valid sampled intervals only; "
                "resets, gaps, and window edges are excluded.",
            },
        }
