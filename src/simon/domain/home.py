from colorsys import hsv_to_rgb, rgb_to_hsv
from ipaddress import IPv4Address, IPv4Network
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.models import StrictModel, utc_now


def color_hs(color: str) -> tuple[float, float]:
    h, s, _ = rgb_to_hsv(*(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)))
    return h * 360, s


def color_hex(hue: float, saturation: float) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in hsv_to_rgb(hue / 360, saturation, 1))


class HomeDevice(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    household_id: UUID
    name: str = Field(min_length=1, max_length=100)
    room: str = Field(default="", max_length=100)
    provider: Literal["lifx", "tuya", "shelly"]
    remote_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    address: IPv4Address | None = None
    load_type: Literal["lighting", "air_purifier", "unclassified", "other"] = "unclassified"
    control_enabled: bool = False
    groups: tuple[str, ...] = ()
    name_override: bool = False
    room_override: bool = False
    groups_override: bool = False
    source: Literal["configured", "discovered"] = "configured"
    account_key: str = ""
    present: bool = True

    @model_validator(mode="after")
    def destination(self) -> "HomeDevice":
        if self.provider == "shelly":
            if not self.address or not any(
                self.address in IPv4Network(cidr)
                for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            ):
                raise ValueError(
                    "Shelly requires an explicitly configured private LAN IPv4 address"
                )
            if not self.remote_id.lower().startswith("shellyplugusg4-"):
                raise ValueError("expected a Shelly Plug US Gen4 device ID")
        elif self.address is not None:
            raise ValueError("cloud devices do not accept custom addresses")
        if self.provider == "lifx" and (
            len(self.remote_id) != 12 or any(c not in "0123456789abcdef" for c in self.remote_id)
        ):
            raise ValueError("LIFX requires the exact 12-character light ID")
        return self


class HomeQuery(StrictModel):
    device_id: str = Field(min_length=1, max_length=64)


class HomeRename(HomeQuery):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("provide a name without surrounding whitespace")
        return value


class HomeSettings(StrictModel):
    on: bool | None = Field(default=None, strict=True)
    brightness: int | None = Field(default=None, ge=1, le=100, strict=True)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("color")
    @classmethod
    def normalized_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.lower() == "#000000":
            raise ValueError("use on=false to turn lights off")
        return color_hex(*color_hs(value))

    @model_validator(mode="after")
    def nonempty(self) -> "HomeSettings":
        if self.on is None and self.brightness is None and self.color is None:
            raise ValueError("choose power, brightness, or color")
        return self


class HomeChange(HomeSettings, HomeQuery):
    pass


class DirectHomeControl(HomeSettings):
    idempotency_key: str = Field(min_length=8, max_length=200)


class HomeControl(HomeSettings):
    device_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=32)
    room: str | None = Field(default=None, min_length=1, max_length=100)
    group: str | None = Field(default=None, min_length=1, max_length=100)
    all_lights: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def target(self) -> "HomeControl":
        if (
            sum(
                (
                    self.device_ids is not None,
                    self.room is not None,
                    self.group is not None,
                    self.all_lights,
                )
            )
            != 1
        ):
            raise ValueError("choose exactly one of device_ids, room, group, or all_lights")
        if self.device_ids and len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("duplicate devices")
        if any(v is not None and v != v.strip() for v in (self.room, self.group)):
            raise ValueError("trim target whitespace")
        return self


class HomeStatus(StrictModel):
    device_id: str
    online: bool | None = None
    on: bool | None = None
    brightness: float | None = None
    color: str | None = None
    watts: float | None = Field(default=None, allow_inf_nan=False)
    energy_wh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    voltage: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    current: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    frequency: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    temperature_c: float | None = Field(default=None, allow_inf_nan=False)
    uptime_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    capabilities: tuple[str, ...] = ()
    note: str = ""


class HomeCommand(StrictModel):
    id: UUID
    household_id: UUID
    actor_id: UUID
    thread_id: UUID | None = None
    run_id: UUID | None = None
    device_name: str
    change: HomeChange
    status: Literal["executing", "succeeded", "failed", "unknown"] = "executing"
    observed: HomeStatus | None = None
    verified: bool = False
    error: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class DiscoveredDevice(StrictModel):
    remote_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    room: str = Field(default="", max_length=100)
    groups: tuple[str, ...] = ()
    lighting: bool = False
    address: IPv4Address | None = None


class HomeOrganization(StrictModel):
    device_ids: tuple[str, ...] = Field(min_length=1, max_length=200)
    room: str | None = Field(default=None, max_length=100)
    groups: tuple[str, ...] | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def valid(self) -> "HomeOrganization":
        if self.room is None and self.groups is None:
            raise ValueError("specify a room or groups")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("duplicate devices")
        if self.room is not None and self.room != self.room.strip():
            raise ValueError("trim room whitespace")
        if self.groups is not None and (
            len(set(g.casefold() for g in self.groups)) != len(self.groups)
            or any(not g or g != g.strip() or len(g) > 100 for g in self.groups)
        ):
            raise ValueError("invalid or duplicate groups")
        return self


class HomeSync(StrictModel):
    household_id: UUID
    provider: Literal["lifx", "tuya", "shelly"]
    account_key: str
    claim_id: UUID = Field(default_factory=uuid4)
    attempted_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    lease_until: AwareDatetime
    status: Literal["syncing", "ready", "error"] = "syncing"
    count: int = 0
    error: str | None = None


class OutletSetup(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    room: str = Field(default="", max_length=100)
    load_type: Literal["lighting", "air_purifier", "unclassified"]
    control_enabled: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def known_load(self) -> "OutletSetup":
        if self.control_enabled and self.load_type == "unclassified":
            raise ValueError("identify the connected load before enabling control")
        if (
            not self.name.strip()
            or self.name != self.name.strip()
            or self.room != self.room.strip()
        ):
            raise ValueError("trim name and room whitespace")
        return self


class OutletPower(StrictModel):
    on: bool = Field(strict=True)
    idempotency_key: str = Field(min_length=8, max_length=200)


class HomeOutletSetup(OutletSetup, HomeQuery):
    pass


class PowerSample(StrictModel):
    household_id: UUID
    device_id: str
    captured_at: AwareDatetime
    state: Literal["pending", "ok", "unavailable"] = "pending"
    reading: HomeStatus | None = None
    error: str | None = None
