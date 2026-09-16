"""Explicit-device adapters; provider results and labels are never executable instructions."""

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic, time
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx

from simon.adapters.google import ConnectedError
from simon.config import Settings
from simon.domain.home import (
    DiscoveredDevice,
    HomeChange,
    HomeDevice,
    HomeStatus,
    color_hex,
    color_hs,
)

TUYA_ENDPOINTS = {
    "us": "https://openapi.tuyaus.com",
    "us-east": "https://openapi-ueaz.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "eu-west": "https://openapi-weaz.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    auth: httpx.Auth | None = None,
    write: bool = False,
) -> Any:
    try:
        started = monotonic()
        # Bypass proxy environment variables for explicit LAN destinations as well as cloud calls.
        with (
            httpx.Client(timeout=8, follow_redirects=False, trust_env=False, auth=auth) as client,
            client.stream(method, url, headers=headers, content=body) as response,
        ):
            if not 200 <= response.status_code < 300:
                raise ConnectedError(
                    "Device API rejected the request. Check connection settings.",
                    unknown=write and (response.status_code >= 500 or response.status_code == 408),
                )
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 200000 or monotonic() - started > 15:
                    raise ConnectedError("Device response exceeded its limit.", unknown=write)
            return json.loads(content)
    except httpx.HTTPError:
        raise ConnectedError(
            "Device API is unavailable. Check the connection.", unknown=write
        ) from None
    except (ValueError, TypeError):
        raise ConnectedError("Device API returned an invalid response.", unknown=write) from None


class LIFXAPI:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def headers(self) -> dict[str, str]:
        if not self.settings.lifx_token:
            raise ConnectedError("Set SIMON_LIFX_TOKEN locally to connect LIFX.")
        return {
            "Authorization": "Bearer " + self.settings.lifx_token.get_secret_value(),
            "Content-Type": "application/json",
        }

    def read(self, device: HomeDevice) -> HomeStatus:
        rows = request_json(
            "GET", f"https://api.lifx.com/v1/lights/id:{device.remote_id}", headers=self.headers
        )
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("id") != device.remote_id:
            raise ConnectedError("LIFX did not return the configured light.")
        row = rows[0]
        color = row.get("color") or {}
        supports_color = row.get("product", {}).get("capabilities", {}).get("has_color") is True
        return HomeStatus(
            device_id=device.id,
            online=row.get("connected"),
            on=row.get("power") == "on",
            brightness=float(row["brightness"]) * 100,
            color=color_hex(float(color["hue"]), float(color["saturation"]))
            if supports_color and "hue" in color and "saturation" in color
            else None,
            capabilities=("power", "brightness", "color")
            if supports_color
            else ("power", "brightness"),
        )

    def discover(self) -> tuple[DiscoveredDevice, ...]:
        rows = request_json("GET", "https://api.lifx.com/v1/lights/all", headers=self.headers)
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ConnectedError("LIFX discovery returned an invalid or oversized inventory.")
        devices = []
        for row in rows:
            group = str((row.get("group") or {}).get("name", ""))[:100].strip()
            devices.append(
                DiscoveredDevice(
                    remote_id=row["id"],
                    name=str(row.get("label") or "LIFX device")[:100],
                    room=group,
                    groups=(group,) if group else (),
                    lighting=isinstance(row.get("brightness"), (int, float)) and "power" in row,
                )
            )
        return tuple(devices)

    def set(self, device: HomeDevice, change: HomeChange) -> None:
        body: dict[str, Any] = {"duration": 0, "fast": False}
        if change.on is not None:
            body["power"] = "on" if change.on else "off"
        if change.brightness is not None:
            body["brightness"] = change.brightness / 100
        if change.color is not None:
            hue, saturation = color_hs(change.color)
            body["color"] = f"hue:{hue:.4f} saturation:{saturation:.6f}"
        result = request_json(
            "PUT",
            f"https://api.lifx.com/v1/lights/id:{device.remote_id}/state",
            headers=self.headers,
            body=json.dumps(body).encode(),
            write=True,
        )
        results = result.get("results", []) if isinstance(result, dict) else []
        if (
            len(results) != 1
            or results[0].get("id") != device.remote_id
            or results[0].get("status") != "ok"
        ):
            raise ConnectedError("LIFX did not confirm the requested change.", unknown=True)


class TuyaAPI:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token = ""
        self._expires = 0.0
        self._lock = Lock()

    def _call(
        self,
        method: str,
        path: str,
        token: str = "",
        body: dict[str, Any] | None = None,
        *,
        write: bool = False,
    ) -> Any:
        if not self.settings.tuya_client_id or not self.settings.tuya_client_secret:
            raise ConnectedError("Configure the Tuya cloud project credentials locally.")
        encoded = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode() if body else b""
        )
        stamp = str(int(time() * 1000))
        canonical = method + "\n" + hashlib.sha256(encoded).hexdigest() + "\n\n" + path
        signature = (
            hmac.new(
                self.settings.tuya_client_secret.get_secret_value().encode(),
                (self.settings.tuya_client_id + token + stamp + canonical).encode(),
                hashlib.sha256,
            )
            .hexdigest()
            .upper()
        )
        headers = {
            "client_id": self.settings.tuya_client_id,
            "t": stamp,
            "sign_method": "HMAC-SHA256",
            "sign": signature,
            "Content-Type": "application/json",
        }
        if token:
            headers["access_token"] = token
        result = request_json(
            method,
            TUYA_ENDPOINTS[self.settings.tuya_region] + path,
            headers=headers,
            body=encoded,
            write=write,
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            if isinstance(result, dict) and str(result.get("code")) == "28841107":
                raise ConnectedError(
                    "Tuya's configured data center is suspended. Enable it in the Tuya cloud "
                    "project, then refresh devices.",
                    unknown=write,
                )
            raise ConnectedError(
                "Tuya rejected the request. Check region, API entitlement, and device linking.",
                unknown=write,
            )
        return result.get("result")

    def token(self) -> str:
        with self._lock:
            if time() >= self._expires:
                result = self._call("GET", "/v1.0/token?grant_type=1")
                if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
                    raise ConnectedError("Tuya did not return an access token.")
                self._token = result["access_token"]
                self._expires = time() + max(0, int(result.get("expire_time", 0)) - 60)
            return self._token

    def properties(self, device: HomeDevice) -> tuple[dict[str, Any], dict[str, Any]]:
        token = self.token()
        base = "/v1.0/iot-03/devices/" + device.remote_id
        spec = self._call("GET", base + "/specification", token)
        values = self._call("GET", base + "/status", token)
        functions = {item["code"]: item for item in spec.get("functions", [])}
        for code in ("colour_data", "colour_data_v2"):
            if code in functions:
                functions[code] = {**functions[code], "category": spec.get("category")}
        status = {item["code"]: item["value"] for item in values}
        return functions, status

    def discover(self) -> tuple[DiscoveredDevice, ...]:
        devices: dict[str, DiscoveredDevice] = {}
        cursor = ""
        seen = set()
        for _ in range(10):
            query = {"size": "100"}
            if cursor:
                query["last_row_key"] = cursor
            # Query keys must be sorted before signing the exact request path.
            path = "/v1.0/iot-01/associated-users/devices?" + urlencode(sorted(query.items()))
            result = self._call("GET", path, self.token())
            if not isinstance(result, dict) or not isinstance(result.get("devices"), list):
                raise ConnectedError("Tuya discovery returned an invalid inventory.")
            for row in result["devices"]:
                item = DiscoveredDevice(
                    remote_id=row["id"],
                    name=str(row.get("name") or "Tuya device")[:100],
                    lighting=row.get("category") in {"dj", "dd", "xdd"},
                )
                devices[item.remote_id] = item
            if result.get("has_more") is False:
                return tuple(devices.values())
            cursor = result.get("last_row_key", "")
            if (
                result.get("has_more") is not True
                or not isinstance(cursor, str)
                or not cursor
                or cursor in seen
            ):
                raise ConnectedError("Tuya discovery could not finish paging. Refresh devices.")
            seen.add(cursor)
        raise ConnectedError("Tuya discovery exceeded 1,000 devices. Narrow the linked project.")

    @staticmethod
    def brightness_field(functions: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        for code in ("bright_value_v2", "bright_value"):
            field = functions.get(code)
            if field and field.get("type", "").lower() == "integer":
                bounds = (
                    json.loads(field["values"])
                    if isinstance(field["values"], str)
                    else field["values"]
                )
                if (
                    isinstance(bounds.get("min"), int)
                    and isinstance(bounds.get("max"), int)
                    and 0 <= bounds["min"] < bounds["max"] <= 10000
                ):
                    return code, bounds
        return None

    def read(self, device: HomeDevice) -> HomeStatus:
        functions, status = self.properties(device)
        caps = []
        if functions.get("switch_led", {}).get("type", "").lower() == "boolean":
            caps.append("power")
        brightness = None
        field = self.brightness_field(functions)
        if field and status.get("work_mode", "white") == "white":
            caps.append("brightness")
            code, bounds = field
            if isinstance(status.get(code), (int, float)):
                brightness = max(
                    1,
                    min(
                        100,
                        1 + 99 * (status[code] - bounds["min"]) / (bounds["max"] - bounds["min"]),
                    ),
                )
        color = None
        color_field = self.color_field(functions)
        if color_field and self.color_mode_supported(functions, status):
            caps.append("color")
            if status.get("work_mode") == "colour":
                hsv = self.color_value(status.get(color_field[0]), color_field[1])
                if hsv:
                    color = color_hex(hsv["h"], hsv["s"] / color_field[1])
                    brightness = hsv["v"] / color_field[1] * 100
                    caps.append("brightness")
        return HomeStatus(
            device_id=device.id,
            on=status.get("switch_led"),
            brightness=brightness,
            color=color,
            capabilities=tuple(caps),
            note="Cloud-reported state; online status unverified."
            + (
                " Color changes reuse saved color intensity when current brightness is unavailable."
                if color_field and brightness is None
                else ""
            ),
        )

    @staticmethod
    def color_field(functions: dict[str, Any]) -> tuple[str, int] | None:
        for code in ("colour_data_v2", "colour_data"):
            field = functions.get(code, {})
            if field.get("type", "").lower() != "json":
                continue
            bounds = field.get("values") or {}
            if isinstance(bounds, str):
                bounds = json.loads(bounds)
            # Prefer advertised HSV bounds. Standard cloud JSON is distinct from raw LAN DPs.
            maximum = bounds.get("v", {}).get("max")
            saturation = bounds.get("s", {}).get("max")
            if maximum is not None or saturation is not None:
                standard = (
                    all(
                        bounds.get(k, {}).get("min", 0) == 0
                        and bounds.get(k, {}).get("scale", 0) == 0
                        and bounds.get(k, {}).get("step", 1) == 1
                        for k in ("h", "s", "v")
                    )
                    and bounds.get("h", {}).get("max", 360) == 360
                )
                if standard and maximum in (255, 1000) and saturation == maximum:
                    return code, int(maximum)
                continue
            if code == "colour_data_v2" or field.get("category") in {"dd", "fwd", "xdd"}:
                return code, 1000
            if field.get("category") == "dj":
                return code, 255
        return None

    @staticmethod
    def color_value(value: Any, maximum: int) -> dict[str, float] | None:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        if not isinstance(value, dict) or any(
            type(value.get(k)) not in (int, float) or not 0 <= value[k] <= limit
            for k, limit in (("h", 360), ("s", maximum), ("v", maximum))
        ):
            return None
        return {k: float(value[k]) for k in ("h", "s", "v")}

    @staticmethod
    def color_mode_supported(functions: dict[str, Any], status: dict[str, Any]) -> bool:
        if status.get("work_mode") == "colour":
            return True
        field = functions.get("work_mode", {})
        bounds = field.get("values") or {}
        if isinstance(bounds, str):
            bounds = json.loads(bounds)
        return field.get("type", "").lower() == "enum" and "colour" in bounds.get("range", [])

    def set(self, device: HomeDevice, change: HomeChange) -> None:
        functions, status = self.properties(device)
        commands = []
        if change.on is not None:
            if functions.get("switch_led", {}).get("type", "").lower() != "boolean":
                raise ConnectedError(
                    "This Tuya device does not advertise the supported lighting power control."
                )
            commands.append({"code": "switch_led", "value": change.on})
        use_color = change.color is not None or (
            change.brightness is not None and status.get("work_mode") == "colour"
        )
        if use_color:
            color_field = self.color_field(functions)
            if not color_field or not self.color_mode_supported(functions, status):
                raise ConnectedError("This Tuya device does not advertise supported color control.")
            code, maximum = color_field
            hsv = self.color_value(status.get(code), maximum)
            if change.color is None and hsv is None:
                raise ConnectedError(
                    "Current color is unavailable; specify a color and brightness."
                )
            hue, saturation = (
                color_hs(change.color)
                if change.color
                else (hsv["h"], hsv["s"] / maximum)
                if hsv
                else (0, 0)
            )
            level = change.brightness
            saved_value = None
            if level is None:
                if status.get("work_mode") == "colour" and hsv:
                    saved_value = round(hsv["v"])
                else:
                    white = self.brightness_field(functions)
                    if (
                        status.get("work_mode") == "white"
                        and white
                        and type(status.get(white[0])) in (int, float)
                    ):
                        level = max(
                            1,
                            min(
                                100,
                                round(
                                    1
                                    + 99
                                    * (status[white[0]] - white[1]["min"])
                                    / (white[1]["max"] - white[1]["min"])
                                ),
                            ),
                        )
                    elif hsv:
                        # RGB-only controllers can report white mode with no white dimmer.
                        # Restore their saved color intensity instead of inventing a level.
                        saved_value = round(hsv["v"])
                if level is None and saved_value is None:
                    raise ConnectedError("Current brightness is unavailable; specify brightness.")
            if status.get("work_mode") != "colour":
                commands.append({"code": "work_mode", "value": "colour"})
            commands.append(
                {
                    "code": code,
                    "value": {
                        "h": round(hue),
                        "s": round(saturation * maximum),
                        "v": saved_value
                        if saved_value is not None
                        else round((level or 0) / 100 * maximum),
                    },
                }
            )
        elif change.brightness is not None:
            field = self.brightness_field(functions)
            if not field or status.get("work_mode", "white") != "white":
                raise ConnectedError(
                    "Brightness is unavailable for this Tuya device or color mode."
                )
            code, bounds = field
            step = max(1, int(bounds.get("step", 1)))
            value = (
                bounds["min"]
                + round((change.brightness - 1) / 99 * (bounds["max"] - bounds["min"]) / step)
                * step
            )
            commands.append({"code": code, "value": min(bounds["max"], value)})
        result = self._call(
            "POST",
            "/v1.0/iot-03/devices/" + device.remote_id + "/commands",
            self.token(),
            {"commands": commands},
            write=True,
        )
        if result is not True:
            raise ConnectedError("Tuya did not accept the lighting command.", unknown=True)


class ShellyAPI:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def discover(self) -> tuple[DiscoveredDevice, ...]:
        from simon.adapters.lan_discovery import shelly_candidates

        def probe(candidate: tuple[str, Any]) -> DiscoveredDevice | None:
            remote_id, address = candidate
            device = HomeDevice(
                id="discovery-probe",
                household_id=UUID(int=0),
                name="Shelly",
                provider="shelly",
                remote_id=remote_id,
                address=address,
            )
            try:
                info = self.identity(device)
                return DiscoveredDevice(
                    remote_id=remote_id,
                    address=address,
                    name=str(info.get("name") or remote_id)[:100],
                )
            except (ConnectedError, ValueError, TypeError, AttributeError):
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            devices = [d for d in pool.map(probe, shelly_candidates()) if d is not None]
        # Conflicting advertisements for the same hardware ID are not trusted.
        return tuple(d for d in devices if sum(x.remote_id == d.remote_id for x in devices) == 1)

    def _call(
        self,
        device: HomeDevice,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        write: bool = False,
    ) -> Any:
        # Only validated RFC1918 literal addresses; no DNS, redirects, or proxy routing.
        assert device.address
        password = self.settings.shelly_password
        auth = httpx.DigestAuth("admin", password.get_secret_value()) if password else None
        return request_json(
            "POST",
            f"http://{device.address}/rpc/{method}",
            headers={"Content-Type": "application/json"},
            body=json.dumps(params or {}).encode(),
            auth=auth,
            write=write,
        )

    def identity(self, device: HomeDevice) -> dict[str, Any]:
        info = self._call(device, "Shelly.GetDeviceInfo")
        if (
            str(info.get("id", "")).lower() != device.remote_id.lower()
            or info.get("model") != "S4PL-00116US"
            or info.get("gen") != 4
        ):
            raise ConnectedError("The configured address is not the expected Shelly Plug US Gen4.")
        return dict(info)

    def read(self, device: HomeDevice) -> HomeStatus:
        self.identity(device)
        status = self._call(device, "Switch.GetStatus", {"id": 0})
        return self.status(device, status)

    def meter(self, device: HomeDevice) -> HomeStatus:
        self.identity(device)
        result = self._call(device, "Shelly.GetStatus")
        return self.status(device, result["switch:0"], result.get("sys", {}).get("uptime"))

    @staticmethod
    def status(
        device: HomeDevice, status: dict[str, Any], uptime: float | None = None
    ) -> HomeStatus:
        return HomeStatus(
            device_id=device.id,
            online=True,
            on=status["output"],
            watts=status.get("apower"),
            energy_wh=status.get("aenergy", {}).get("total"),
            voltage=status.get("voltage"),
            current=status.get("current"),
            frequency=status.get("freq"),
            temperature_c=(status.get("temperature") or {}).get("tC"),
            uptime_seconds=uptime,
            capabilities=("power",),
            note="; ".join(status.get("errors", [])),
        )

    def set(self, device: HomeDevice, change: HomeChange) -> None:
        if change.brightness is not None or change.color is not None or change.on is None:
            raise ConnectedError("A Shelly outlet supports on/off only.")
        self.identity(device)
        result = self._call(device, "Switch.Set", {"id": 0, "on": change.on}, write=True)
        if not isinstance(result, dict) or not isinstance(result.get("was_on"), bool):
            raise ConnectedError("Shelly did not acknowledge the command.", unknown=True)
