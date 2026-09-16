import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from simon.adapters.google import ConnectedError
from simon.adapters.home import LIFXAPI, ShellyAPI, TuyaAPI
from simon.config import Settings
from simon.domain.home import HomeChange, HomeControl, HomeStatus
from simon.services.home import HomeService
from tests.unit.test_home_adapters import device, transport


def test_lifx_color_preserves_brightness_and_reads_hue(monkeypatch):
    light = device()
    bodies = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": light.remote_id,
                        "power": "on",
                        "connected": True,
                        "brightness": 0.42,
                        "color": {"hue": 240, "saturation": 1},
                        "product": {"capabilities": {"has_color": True}},
                    }
                ],
            )
        bodies.append(json.loads(request.content))
        return httpx.Response(207, json={"results": [{"id": light.remote_id, "status": "ok"}]})

    transport(monkeypatch, respond)
    api = LIFXAPI(Settings(_env_file=None, lifx_token=SecretStr("test")))
    state = api.read(light)
    assert state.color == "#0000ff" and state.brightness == 42 and "color" in state.capabilities
    api.set(light, HomeChange(device_id=light.id, color="#0000ff"))
    assert bodies == [{"duration": 0, "fast": False, "color": "hue:240.0000 saturation:1.000000"}]


@pytest.mark.parametrize(
    "code,category,maximum",
    [
        ("colour_data", "dj", 255),
        ("colour_data", "dd", 1000),
        ("colour_data_v2", "dj", 1000),
        ("colour_data", "fwd", 1000),
    ],
)
def test_tuya_cloud_color_scales_modes_and_brightness(code, category, maximum):
    api = TuyaAPI(Settings(_env_file=None))
    functions = {
        code: {"type": "Json", "values": "{}", "category": category},
        "work_mode": {"type": "Enum", "values": '{"range":["white","colour"]}'},
        "bright_value": {"type": "Integer", "values": {"min": 10, "max": 1000}},
    }
    status = {"work_mode": "colour", code: json.dumps({"h": 120, "s": maximum, "v": maximum * 0.4})}
    api.properties = lambda d: (functions, status)
    api.token = lambda: "token"
    commands = []

    def call(method, path, token, body, **kwargs):
        assert method == "POST" and kwargs == {"write": True}
        commands.extend(body["commands"])
        return True

    api._call = call
    light = device("tuya")
    assert api.read(light).color == "#00ff00" and api.read(light).brightness == 40
    api.set(light, HomeChange(device_id=light.id, color="#0000ff"))
    assert commands == [
        {"code": code, "value": {"h": 240, "s": maximum, "v": round(maximum * 0.4)}}
    ]
    commands.clear()
    api.set(light, HomeChange(device_id=light.id, brightness=70))
    assert commands[0]["value"] == {"h": 120, "s": maximum, "v": round(maximum * 0.7)}
    commands.clear()
    status.update(work_mode="white", bright_value=500)
    api.set(light, HomeChange(device_id=light.id, color="#ff0000"))
    assert commands == [
        {"code": "work_mode", "value": "colour"},
        {"code": code, "value": {"h": 0, "s": maximum, "v": round(maximum * 0.5)}},
    ]


def test_unknown_color_layout_never_sends_and_explicit_bounds_win():
    api = TuyaAPI(Settings(_env_file=None))
    functions = {"colour_data": {"type": "Json", "values": {}}}
    api.properties = lambda d: (functions, {"work_mode": "colour"})
    api._call = lambda *a, **k: pytest.fail("must not send")
    assert "color" not in api.read(device("tuya")).capabilities
    with pytest.raises(ConnectedError):
        api.set(device("tuya"), HomeChange(device_id="office-light", color="#00ff00"))
    functions["colour_data"]["values"] = {"s": {"max": 1000}, "v": {"max": 1000}}
    assert api.color_field(functions) == ("colour_data", 1000)
    functions["colour_data"]["values"]["s"]["max"] = 42
    assert api.color_field(functions) is None
    assert api.color_value("not-json", 1000) is None
    assert api.color_value({"h": 400, "s": 1, "v": 2}, 1000) is None
    with pytest.raises(ConnectedError):
        ShellyAPI(Settings(_env_file=None)).set(
            device("shelly"), HomeChange(device_id="office-light", on=True, color="#0000ff")
        )


def test_office_hexagon_advertised_schema_restores_saved_color_intensity():
    api = TuyaAPI(Settings(_env_file=None))
    functions = {
        "colour_data": {
            "type": "Json",
            "category": "dd",
            "values": json.dumps(
                {
                    "h": {"min": 0, "max": 360, "scale": 0, "step": 1},
                    "s": {"min": 0, "max": 1000, "scale": 0, "step": 1},
                    "v": {"min": 0, "max": 1000, "scale": 0, "step": 1},
                }
            ),
        },
        "work_mode": {"type": "Enum", "values": '{"range":["white","colour","scene","music"]}'},
    }
    status = {"work_mode": "white", "colour_data": '{"h":120,"s":1000,"v":637}'}
    api.properties = lambda d: (functions, status)
    api.token = lambda: "token"
    calls = []

    def send(method, path, token, body, **kwargs):
        calls.append(body)
        return True

    api._call = send
    api.set(device("tuya"), HomeChange(device_id="office-light", color="#0000ff"))
    assert calls == [
        {
            "commands": [
                {"code": "work_mode", "value": "colour"},
                {"code": "colour_data", "value": {"h": 240, "s": 1000, "v": 637}},
            ]
        }
    ]
    status["work_mode"] = "colour"
    calls.clear()
    api.set(device("tuya"), HomeChange(device_id="office-light", color="#ff0000"))
    assert calls[0]["commands"][0]["value"]["v"] == 637


@pytest.mark.parametrize(
    "data",
    [
        {"all_lights": True, "room": "Office", "on": False},
        {"on": False},
        {"all_lights": True, "color": "red"},
        {"all_lights": True, "color": "#000000"},
        {"all_lights": True, "brightness": 0},
        {"device_ids": ["a", "a"], "on": True},
    ],
)
def test_bad_targets_and_colors_are_rejected(data):
    with pytest.raises(ValidationError):
        HomeControl.model_validate(data)


def test_color_verification_accounts_for_wrap_and_white():
    change = HomeChange(device_id="light", color="#ff0000")
    assert HomeService.matches(change, HomeStatus(device_id="light", color="#ff0001"))
    assert not HomeService.matches(change, HomeStatus(device_id="light", color="#0000ff"))
    assert not HomeService.matches(change, HomeStatus(device_id="light"))
    assert HomeChange(device_id="light", color="#880000").color == "#ff0000"
