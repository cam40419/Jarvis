"""Durable discovery and Simon-owned organization; never sends device commands."""

import hashlib
from datetime import timedelta
from typing import Literal
from uuid import UUID

from simon.adapters.google import ConnectedError
from simon.adapters.home import LIFXAPI, ShellyAPI, TuyaAPI
from simon.config import Settings
from simon.domain.connected_tools import HOME_CAPABILITIES
from simon.domain.errors import NotFoundError
from simon.domain.home import HomeDevice, HomeOrganization, HomeRename, HomeSync
from simon.domain.identity import DEV_HOUSEHOLD_ID
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.conversations import ConversationService
from simon.services.policy import PolicyEngine


class HomeInventory:
    def __init__(
        self,
        store: Store,
        audit: AuditService,
        settings: Settings,
        lifx: LIFXAPI,
        tuya: TuyaAPI,
        shelly: ShellyAPI | None = None,
    ) -> None:
        self.store, self.audit, self.settings = store, audit, settings
        self.lifx, self.tuya = lifx, tuya
        self.shelly = shelly or ShellyAPI(settings)
        self.target = settings.home_household_id or (
            DEV_HOUSEHOLD_ID if settings.environment == "development" else None
        )

    def configured(self, household_id: UUID) -> dict[str, bool]:
        if household_id != self.target:
            return {"lifx": False, "tuya": False, "shelly": False}
        return {
            "lifx": bool(self.settings.lifx_token),
            "tuya": bool(self.settings.tuya_client_id and self.settings.tuya_client_secret),
            "shelly": self.settings.shelly_lan_discovery,
        }

    def account_key(self, provider: str) -> str:
        if provider == "shelly":
            # Password rotation does not change the LAN or the commissioned load.
            return hashlib.sha256(f"shelly-lan:{self.target}".encode()).hexdigest()
        # LIFX has no account ID endpoint here. Rotating its token causes a fresh account binding.
        secret = (
            self.settings.lifx_token if provider == "lifx" else self.settings.tuya_client_secret
        )
        material = provider + (secret.get_secret_value() if secret else "")
        if provider == "tuya":
            material += self.settings.tuya_client_id + self.settings.tuya_region
        return hashlib.sha256(material.encode()).hexdigest()

    def devices(self, household_id: UUID, legacy: tuple[HomeDevice, ...]) -> tuple[HomeDevice, ...]:
        configured = self.configured(household_id)
        saved = {d.id: d for d in self.store.home_devices(household_id)}
        result = {
            key: d
            for key, d in saved.items()
            if d.source == "discovered"
            and (
                configured.get(d.provider)
                or (d.provider == "shelly" and household_id == self.target)
            )
            and d.account_key == self.account_key(d.provider)
        }
        for device in legacy:
            if device.household_id != household_id:
                continue
            previous = saved.get(device.id)
            if previous and previous.source == "configured":
                device = device.model_copy(
                    update={
                        "name": previous.name if previous.name_override else device.name,
                        "name_override": previous.name_override,
                        "room": previous.room if previous.room_override else device.room,
                        "groups": previous.groups if previous.groups_override else device.groups,
                        "room_override": previous.room_override,
                        "groups_override": previous.groups_override,
                    }
                )
            if device.provider == "shelly":
                duplicate = next(
                    (
                        d
                        for d in result.values()
                        if d.provider == "shelly" and d.remote_id == device.remote_id
                    ),
                    None,
                )
                if duplicate:
                    device = device.model_copy(update={"address": duplicate.address})
                    result.pop(duplicate.id)
            result[device.id] = device
        return tuple(
            sorted(result.values(), key=lambda d: (d.room.casefold(), d.name.casefold(), d.id))
        )

    def status(self, actor: ActorContext) -> list[dict[str, object]]:
        ConversationService.authorize(actor, "home:read")
        result = []
        for provider, configured in self.configured(actor.household_id).items():
            record = self.store.home_sync(actor.household_id, provider)
            if record and record.account_key != self.account_key(provider):
                record = None
            result.append(
                {
                    "provider": provider,
                    "configured": configured,
                    "status": record.status
                    if configured and record
                    else "pending"
                    if configured
                    else "unconfigured",
                    "count": record.count if configured and record else 0,
                    "error": record.error if configured and record else None,
                    "completed_at": record.completed_at.isoformat()
                    if configured and record and record.completed_at
                    else None,
                }
            )
        return result

    def sync(self, actor: ActorContext, *, force: bool = False) -> list[dict[str, object]]:
        ConversationService.authorize(actor, "home:read")
        for provider in ("lifx", "tuya", "shelly"):
            if self.configured(actor.household_id)[provider]:
                self._sync_provider(actor, provider, force=force)
        return self.status(actor)

    def sync_target(self) -> None:
        if self.target and self.settings.home_auto_discovery:
            self.sync(
                ActorContext(
                    actor_id=UUID("00000000-0000-4000-8000-000000000002"),
                    household_id=self.target,
                    channel=Channel.WORKER,
                    scopes=frozenset({"home:read"}),
                )
            )

    def _sync_provider(
        self, actor: ActorContext, provider: Literal["lifx", "tuya", "shelly"], *, force: bool
    ) -> None:
        now, key = utc_now(), self.account_key(provider)
        with self.store.transaction(actor.household_id):
            previous = self.store.home_sync(actor.household_id, provider)
            if previous and previous.account_key == key:
                if previous.status == "syncing" and previous.lease_until > now:
                    return
                cooldown = timedelta(seconds=15 if force else 300)
                if previous.status != "syncing" and previous.attempted_at + cooldown > now:
                    return
            claim = HomeSync(
                household_id=actor.household_id,
                provider=provider,
                account_key=key,
                attempted_at=now,
                lease_until=now + timedelta(minutes=5),
                count=previous.count if previous and previous.account_key == key else 0,
            )
            self.store.save_home_sync(claim)
        try:
            adapters: dict[str, LIFXAPI | TuyaAPI | ShellyAPI] = {
                "lifx": self.lifx,
                "tuya": self.tuya,
                "shelly": self.shelly,
            }
            items = adapters[provider].discover()
            # Validate every destination before changing the saved inventory.
            found = tuple(
                HomeDevice(
                    id=provider + "-" + hashlib.sha256(item.remote_id.encode()).hexdigest()[:20],
                    household_id=actor.household_id,
                    provider=provider,
                    remote_id=item.remote_id,
                    address=item.address,
                    name=item.name,
                    room=item.room,
                    groups=item.groups,
                    load_type="lighting" if item.lighting else "unclassified",
                    control_enabled=item.lighting,
                    source="discovered",
                    account_key=key,
                )
                for item in items
            )
            if len(found) > 1000 or len({d.id for d in found}) != len(found):
                raise ConnectedError("Discovery returned duplicate or too many devices.")
            with self.store.transaction(actor.household_id):
                current = self.store.home_sync(actor.household_id, provider)
                if not current or current.claim_id != claim.claim_id:
                    return
                existing = {d.id: d for d in self.store.home_devices(actor.household_id)}
                for device in found:
                    old = existing.get(device.id)
                    if old and old.account_key == key:
                        device = device.model_copy(
                            update={
                                "name": old.name if old.name_override else device.name,
                                "name_override": old.name_override,
                                "room": old.room if old.room_override else device.room,
                                "groups": old.groups if old.groups_override else device.groups,
                                "room_override": old.room_override,
                                "groups_override": old.groups_override,
                            }
                        )
                        if provider == "shelly":
                            device = device.model_copy(
                                update={
                                    "name": old.name,
                                    "load_type": old.load_type,
                                    "control_enabled": old.control_enabled,
                                }
                            )
                    self.store.save_home_device(device)
                ids = {d.id for d in found}
                for old in existing.values():
                    if (
                        old.source == "discovered"
                        and old.provider == provider
                        and old.id not in ids
                        and provider != "shelly"  # Missing multicast announcements are not removal.
                    ):
                        self.store.save_home_device(old.model_copy(update={"present": False}))
                self.store.save_home_sync(
                    claim.model_copy(
                        update={
                            "status": "ready",
                            "completed_at": utc_now(),
                            "count": len(found),
                        }
                    )
                )
                self.audit.record(
                    event_type="home.discovered",
                    actor=actor,
                    resource_type="home",
                    resource_id=provider,
                    payload={"count": len(found)},
                )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, ConnectedError)
                else "Device discovery could not be completed. Check provider setup and refresh."
            )
            with self.store.transaction(actor.household_id):
                current = self.store.home_sync(actor.household_id, provider)
                if current and current.claim_id == claim.claim_id:
                    self.store.save_home_sync(
                        claim.model_copy(update={"status": "error", "error": message})
                    )

    def rename(
        self, actor: ActorContext, change: HomeRename, legacy: tuple[HomeDevice, ...]
    ) -> dict[str, object]:
        PolicyEngine().authorize(actor, HOME_CAPABILITIES["rename"], None)
        with self.store.transaction(actor.household_id):
            device = next(
                (d for d in self.devices(actor.household_id, legacy) if d.id == change.device_id),
                None,
            )
            if device is None:
                raise NotFoundError("home device not found")
            self.store.save_home_device(
                device.model_copy(update={"name": change.name, "name_override": True})
            )
            self.audit.record(
                event_type="home.renamed",
                actor=actor,
                resource_type="home",
                resource_id=device.id,
                payload={"previous_name": device.name, "name": change.name},
            )
            return {
                "device_id": device.id,
                "name": change.name,
                "scope": "Simon inventory",
                "device_commands_sent": False,
            }

    def organize(
        self,
        actor: ActorContext,
        change: HomeOrganization,
        legacy: tuple[HomeDevice, ...],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        PolicyEngine().authorize(actor, HOME_CAPABILITIES["organize"], None)
        from simon.services.canonical import digest

        def apply() -> dict[str, object]:
            by_id = {d.id: d for d in self.devices(actor.household_id, legacy)}
            if any(key not in by_id for key in change.device_ids):
                raise NotFoundError("One or more home devices were not found in this household.")
            for key in change.device_ids:
                update: dict[str, object] = {}
                if change.room is not None:
                    update.update(room=change.room, room_override=True)
                if change.groups is not None:
                    update.update(groups=change.groups, groups_override=True)
                self.store.save_home_device(by_id[key].model_copy(update=update))
            self.audit.record(
                event_type="home.organized",
                actor=actor,
                resource_type="home",
                resource_id="inventory",
                payload={
                    "device_ids": list(change.device_ids),
                    "room": change.room,
                    "groups": change.groups,
                },
            )
            return {
                "updated": list(change.device_ids),
                "room": change.room,
                "groups": list(change.groups) if change.groups is not None else None,
                "scope": "Simon inventory",
                "device_commands_sent": False,
            }

        with self.store.transaction(actor.household_id):
            result, _ = self.store.execute_once(
                f"{actor.household_id}:home.organize:{actor.actor_id}",
                operation_key,
                digest(change.model_dump(mode="json")),
                apply,
            )
            return result
