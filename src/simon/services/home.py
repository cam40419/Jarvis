import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid5

from pydantic import TypeAdapter

from simon.adapters.google import ConnectedError
from simon.adapters.home import LIFXAPI, ShellyAPI, TuyaAPI
from simon.config import Settings
from simon.domain.connected_tools import HOME_CAPABILITIES, ActionProposal
from simon.domain.errors import AuthorizationError, DomainError, NotFoundError
from simon.domain.home import (
    DirectHomeControl,
    HomeChange,
    HomeCommand,
    HomeControl,
    HomeDevice,
    HomeStatus,
    OutletPower,
    OutletSetup,
    color_hs,
)
from simon.domain.models import ActorContext, utc_now
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.conversations import ConversationService
from simon.services.home_inventory import HomeInventory
from simon.services.identity import IDENTITY_LOCK
from simon.services.policy import PolicyEngine


class HomeService:
    """Household inventory and immediate soft writes with durable per-device receipts."""

    def __init__(self, store: Store, audit: AuditService, settings: Settings) -> None:
        self.store, self.audit, self.settings = store, audit, settings
        self.conversations = ConversationService(store, audit)
        self.policy = PolicyEngine()
        self.devices: tuple[HomeDevice, ...] = ()
        if settings.home_devices_file:
            try:
                data = settings.home_devices_file.read_bytes()
                if len(data) > 64000:
                    raise ValueError("inventory too large")
                self.devices = TypeAdapter(tuple[HomeDevice, ...]).validate_json(data)
                if len(self.devices) > 32 or len({d.id for d in self.devices}) != len(self.devices):
                    raise ValueError("duplicate IDs or too many devices")
            except (OSError, ValueError):
                raise ValueError(
                    "Invalid home device inventory. Check SIMON_HOME_DEVICES_FILE."
                ) from None
        self.lifx = LIFXAPI(settings)
        self.tuya = TuyaAPI(settings)
        self.shelly = ShellyAPI(settings)
        self.catalog = HomeInventory(store, audit, settings, self.lifx, self.tuya, self.shelly)

    def inventory(self, actor: ActorContext) -> list[dict[str, object]]:
        self.policy.authorize(actor, HOME_CAPABILITIES["list"], None)
        return [
            {
                "id": d.id,
                "name": d.name,
                "room": d.room,
                "groups": list(d.groups),
                "present": d.present,
                "provider": d.provider,
                "identifier_suffix": d.remote_id[-6:] if d.provider == "shelly" else None,
                "setup_available": d.provider == "shelly" and d.source == "discovered",
                "load_type": d.load_type,
                "control_enabled": d.control_enabled
                and d.present
                and d.load_type in {"lighting", "air_purifier"},
            }
            for d in self.catalog.devices(actor.household_id, self.devices)
        ]

    def device(self, actor: ActorContext, device_id: str, *, control: bool = False) -> HomeDevice:
        self.policy.authorize(actor, HOME_CAPABILITIES["control" if control else "read"], None)
        device = next(
            (
                d
                for d in self.catalog.devices(actor.household_id, self.devices)
                if d.id == device_id
            ),
            None,
        )
        if not device:
            raise NotFoundError("home device not found")
        if control and (
            not device.present
            or not device.control_enabled
            or device.load_type not in {"lighting", "air_purifier"}
        ):
            raise AuthorizationError(
                "This device is read-only. Only explicitly enabled lighting or air purifier "
                "loads can be controlled."
            )
        return device

    def adapter(self, device: HomeDevice) -> LIFXAPI | TuyaAPI | ShellyAPI:
        adapters: dict[str, LIFXAPI | TuyaAPI | ShellyAPI] = {
            "lifx": self.lifx,
            "tuya": self.tuya,
            "shelly": self.shelly,
        }
        return adapters[device.provider]

    def read(self, actor: ActorContext, device_id: str) -> HomeStatus:
        device = self.device(actor, device_id)
        try:
            return self.adapter(device).read(device)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ConnectedError(
                "Device status could not be interpreted. Check model compatibility."
            ) from None

    def binding(self, device: HomeDevice) -> UUID:
        credentials = {
            "lifx": [self.settings.lifx_token],
            "tuya": [self.settings.tuya_client_secret],
            "shelly": [self.settings.shelly_password],
        }[device.provider]
        material = (
            device.model_dump_json() + self.settings.tuya_region + self.settings.tuya_client_id
        )
        material += "".join(s.get_secret_value() for s in credentials if s)
        return UUID(hashlib.sha256(material.encode()).hexdigest()[:32])

    def commands(self, actor: ActorContext, run_id: UUID | None = None) -> tuple[HomeCommand, ...]:
        self.conversations.authorize(actor, "threads:read")
        self.policy.authorize(actor, HOME_CAPABILITIES["read"], None)
        return tuple(self.store.home_commands(actor.household_id, actor.actor_id, run_id))

    def control(
        self,
        actor: ActorContext,
        run_id: UUID,
        request: HomeControl,
        revalidate: Callable[[], ActorContext],
    ) -> tuple[HomeCommand, ...]:
        self.policy.authorize(actor, HOME_CAPABILITIES["control"], None)
        devices = self.catalog.devices(actor.household_id, self.devices)
        if request.device_ids:
            selected = [self.device(actor, key) for key in request.device_ids]
        else:
            selected = [
                d
                for d in devices
                if d.load_type == "lighting"
                and (
                    request.all_lights
                    or (request.room is not None and d.room.casefold() == request.room.casefold())
                    or (
                        request.group is not None
                        and request.group.casefold() in {g.casefold() for g in d.groups}
                    )
                )
            ]
        if not selected:
            raise NotFoundError("No matching lights. Refresh devices or check the room/group.")
        previous = self.store.home_commands(actor.household_id, actor.actor_id, run_id)
        if len({d.id for d in selected} | {c.change.device_id for c in previous}) > 32:
            raise ConnectedError("At most 32 devices can be controlled in one answer.")

        def apply(device: HomeDevice) -> HomeCommand:
            change = HomeChange(
                device_id=device.id,
                on=request.on,
                brightness=request.brightness,
                color=request.color,
            )
            return self._control_one(actor, run_id, device, change, revalidate)

        # Provider calls do not hold a database transaction. A small bounded pool keeps
        # multi-room commands responsive; each device is independently reauthorized.
        with ThreadPoolExecutor(max_workers=4) as pool:
            return tuple(pool.map(apply, selected))

    def _control_one(
        self,
        actor: ActorContext,
        run_id: UUID | None,
        device: HomeDevice,
        change: HomeChange,
        revalidate: Callable[[], ActorContext],
        command_id: UUID | None = None,
    ) -> HomeCommand:
        if command_id is None:
            assert run_id is not None
            command_id = uuid5(run_id, device.id)

        def check() -> ActorContext:
            current = revalidate()
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Home access changed")
            self.conversations.authorize(current, "threads:write")
            self.policy.authorize(current, HOME_CAPABILITIES["control"], None)
            if run_id is None:
                return current
            attempt = self.store.attempt(run_id)
            if (
                not attempt
                or attempt.household_id != actor.household_id
                or attempt.run.actor_id != actor.actor_id
                or attempt.run.parent_run_id is not None
                or attempt.status != "pending"
                or attempt.expires_at <= utc_now()
            ):
                raise AuthorizationError("The chat request is no longer active.")
            self.conversations.get(current, attempt.run.thread_id)
            return current

        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            check()
            existing = self.store.home_command(actor.household_id, actor.actor_id, command_id)
            if existing:
                if existing.change != change:
                    raise ConnectedError(
                        "This device already has a command in this answer. "
                        "Report its result; wait for a new user request."
                    )
                return existing
            attempt = self.store.attempt(run_id) if run_id is not None else None
            command = HomeCommand(
                id=command_id,
                household_id=actor.household_id,
                actor_id=actor.actor_id,
                thread_id=attempt.run.thread_id if attempt else None,
                run_id=run_id,
                device_name=device.name,
                change=change,
            )
            self.store.save_home_command(command)
            self.audit.record(
                event_type="home.command_started",
                actor=actor,
                resource_type="home_command",
                resource_id=str(command.id),
                payload={"device_id": device.id, "change": change.model_dump(mode="json")},
            )
        dispatched = False
        try:
            checked = check()
            current_device = self.device(checked, device.id, control=True)
            if self.binding(current_device) != self.binding(device):
                raise AuthorizationError("Device configuration changed. Request the change again.")
            state = self.read(checked, device.id)
            self.validate_change(change, state)
            checked = check()  # Cancellation/revocation during preflight must prevent dispatch.
            if self.binding(self.device(checked, device.id, control=True)) != self.binding(device):
                raise AuthorizationError("Device configuration changed.")
            dispatched = True
            self.adapter(device).set(device, change)
            try:
                observed = self.read(checked, device.id)
            except Exception:
                observed = None  # An accepted write stays accepted even when readback fails.
            command = command.model_copy(
                update={
                    "status": "succeeded",
                    "observed": observed,
                    "verified": self.matches(change, observed),
                }
            )
        except Exception as error:
            unknown = dispatched and (not isinstance(error, ConnectedError) or error.unknown)
            command = command.model_copy(
                update={
                    "status": "unknown" if unknown else "failed",
                    "error": str(error)
                    if isinstance(error, DomainError)
                    else "Device control could not be completed.",
                }
            )
        with self.store.transaction(actor.household_id):
            self.store.save_home_command(command)
            self.audit.record(
                event_type="home.command_" + command.status,
                actor=actor,
                resource_type="home_command",
                resource_id=str(command.id),
                payload={"device_id": device.id, "verified": command.verified},
            )
        return command

    def direct_control(
        self,
        actor: ActorContext,
        device_id: str,
        request: DirectHomeControl,
        revalidate: Callable[[], ActorContext],
    ) -> HomeCommand:
        device = self.device(actor, device_id, control=True)
        command_id = uuid5(
            actor.household_id, f"dashboard:{actor.actor_id}:{device.id}:{request.idempotency_key}"
        )
        change = HomeChange(device_id=device.id, **request.model_dump(exclude={"idempotency_key"}))
        return self._control_one(actor, None, device, change, revalidate, command_id)

    def outlet_power(
        self,
        actor: ActorContext,
        device_id: str,
        request: OutletPower,
        revalidate: Callable[[], ActorContext],
    ) -> HomeCommand:
        device = self.device(actor, device_id, control=True)
        if device.provider != "shelly":
            raise AuthorizationError("This endpoint controls Shelly outlets only.")
        command_id = uuid5(
            actor.household_id, f"outlet:{actor.actor_id}:{device.id}:{request.idempotency_key}"
        )
        return self._control_one(
            actor,
            None,
            device,
            HomeChange(device_id=device.id, on=request.on),
            revalidate,
            command_id,
        )

    def setup_outlet(self, actor: ActorContext, device_id: str, request: OutletSetup) -> HomeDevice:
        self.policy.authorize(actor, HOME_CAPABILITIES["setup_outlet"], None)
        device = self.device(actor, device_id)
        if device.provider != "shelly" or device.source != "discovered":
            raise AuthorizationError("Setup requires a discovered Shelly outlet.")
        updated = device.model_copy(
            update={**request.model_dump(), "name_override": True, "room_override": True}
        )
        with self.store.transaction(actor.household_id):
            self.store.save_home_device(updated)
            self.audit.record(
                event_type="home.outlet_configured",
                actor=actor,
                resource_type="home",
                resource_id=device_id,
                payload=request.model_dump(),
            )
        return updated

    @staticmethod
    def validate_change(change: HomeChange, state: HomeStatus) -> None:
        if state.online is False:
            raise ConnectedError("This device is offline. Restore its connection.")
        for field, capability in (
            ("on", "power"),
            ("brightness", "brightness"),
            ("color", "color"),
        ):
            if getattr(change, field) is not None and capability not in state.capabilities:
                # Setting a color and intensity together can leave a scene/white mode.
                if field == "brightness" and change.color and "color" in state.capabilities:
                    continue
                raise ConnectedError(
                    "The requested control is not supported by this device's mode."
                )

    @staticmethod
    def matches(change: HomeChange, observed: HomeStatus | None) -> bool:
        if not observed or observed.online is False:
            return False
        if change.on is not None and observed.on != change.on:
            return False
        if change.brightness is not None and (
            observed.brightness is None or abs(observed.brightness - change.brightness) > 2
        ):
            return False
        if change.color:
            if not observed.color:
                return False
            h, s = color_hs(change.color)
            oh, os = color_hs(observed.color)
            if abs(s - os) > 0.02 or (s > 0.02 and min(abs(h - oh), 360 - abs(h - oh)) > 2):
                return False
        return True

    def propose(self, actor: ActorContext, run_id: UUID, change: HomeChange) -> ActionProposal:
        device = self.device(actor, change.device_id, control=True)
        state = self.read(actor, device.id)
        if state.online is False:
            raise ConnectedError(
                "This device is offline. Restore its connection before controlling it."
            )
        if (change.on is not None and "power" not in state.capabilities) or (
            change.brightness is not None and "brightness" not in state.capabilities
        ):
            raise ConnectedError(
                "The requested control is not supported by this device's current mode."
            )
        return ActionProposal(
            household_id=actor.household_id,
            actor_id=actor.actor_id,
            run_id=run_id,
            connection_id=self.binding(device),
            kind="home.set",
            home=change,
            device_name=device.name,
            device_room=device.room,
            device_provider=device.provider,
        )

    def decide(
        self,
        actor: ActorContext,
        action_id: UUID,
        *,
        confirm: bool,
        revalidate: Callable[[], ActorContext],
    ) -> ActionProposal:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            checked = revalidate()
            if (checked.actor_id, checked.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Home access changed")
            self.conversations.authorize(checked, "home:control")
            action = self.store.action(action_id)
            if (
                not action
                or action.kind != "home.set"
                or (action.actor_id, action.household_id) != (actor.actor_id, actor.household_id)
            ):
                raise NotFoundError("action not found")
            self.conversations.run(checked, action.run_id)
            if action.status != "pending":
                return action
            if not confirm or action.expires_at <= utc_now():
                action = action.model_copy(update={"status": "cancelled"})
                self.store.save_action(action)
                self.audit.record(
                    event_type="action.cancelled",
                    actor=actor,
                    resource_type="action",
                    resource_id=str(action.id),
                    payload={"kind": action.kind},
                )
                return action
            assert action.home
            device = self.device(checked, action.home.device_id, control=True)
            if action.connection_id != self.binding(device):
                raise AuthorizationError("Device configuration changed. Request a new preview.")
            action = action.model_copy(update={"status": "executing"})
            self.store.save_action(action)
            self.audit.record(
                event_type="action.confirmed",
                actor=actor,
                resource_type="action",
                resource_id=str(action.id),
                payload={"kind": action.kind},
            )
        dispatched = False
        try:
            checked = revalidate()
            if (checked.actor_id, checked.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Home access changed")
            self.device(checked, device.id, control=True)
            dispatched = True
            assert action.home
            self.adapter(device).set(device, action.home)
            try:
                observed = self.read(checked, device.id)
            except DomainError:
                observed = None
            matched = bool(
                observed
                and observed.online is not False
                and (action.home.on is None or observed.on == action.home.on)
                and (
                    action.home.brightness is None
                    or (
                        observed.brightness is not None
                        and abs(observed.brightness - action.home.brightness) <= 2
                    )
                )
            )
            action = action.model_copy(
                update={"status": "succeeded", "home_observed": observed, "home_verified": matched}
            )
        except Exception as error:
            unknown = dispatched and (not isinstance(error, ConnectedError) or error.unknown)
            action = action.model_copy(
                update={
                    "status": "unknown" if unknown else "failed",
                    "error": str(error)
                    if isinstance(error, DomainError)
                    else "Device control could not be completed.",
                }
            )
        with self.store.transaction(actor.household_id):
            self.store.save_action(action)
            self.audit.record(
                event_type="action." + action.status,
                actor=actor,
                resource_type="action",
                resource_id=str(action.id),
                payload={"kind": action.kind, "version": 1, "verified": action.home_verified},
            )
        return action
