import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from simon.adapters.google import ConnectedError
from simon.adapters.home import LIFXAPI, TuyaAPI
from simon.adapters.model_tools import definitions
from simon.config import Settings
from simon.domain.home import HomeOrganization
from tests.unit.test_home_adapters import transport


def test_lifx_discovery_maps_names_rooms_groups_and_does_not_write(monkeypatch):
    def respond(request):
        assert request.method == "GET" and request.url.path == "/v1/lights/all"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "d073d5000001",
                    "label": "Beam",
                    "group": {"name": "Office"},
                    "location": {"name": "Home"},
                    "brightness": 1,
                    "power": "on",
                }
            ],
        )

    transport(monkeypatch, respond)
    result = LIFXAPI(Settings(_env_file=None, lifx_token=SecretStr("synthetic"))).discover()
    assert result[0].name == "Beam" and result[0].room == "Office"
    assert result[0].groups == ("Office",) and result[0].lighting


def test_tuya_discovery_pages_linked_accounts_and_discards_secrets(monkeypatch):
    calls = []

    def respond(request):
        assert request.method == "GET"
        calls.append(request.url.raw_path.decode())
        if request.url.path.endswith("/token"):
            result = {"access_token": "synthetic", "expire_time": 7200}
        elif "last_row_key" not in request.url.params:
            assert request.url.path == "/v1.0/iot-01/associated-users/devices"
            result = {
                "devices": [
                    {
                        "id": "hex-1",
                        "name": "Up Arrow",
                        "category": "dd",
                        "local_key": "private-key",
                        "ip": "private-address",
                    }
                ],
                "has_more": True,
                "last_row_key": "cursor +/?",
            }
        else:
            assert request.url.params["last_row_key"] == "cursor +/?"
            assert request.url.raw_path.decode().split("?")[1].startswith("last_row_key=")
            result = {
                "devices": [{"id": "hex-2", "name": "Down Arrow", "category": "dd"}],
                "has_more": False,
            }
        return httpx.Response(200, json={"success": True, "result": result})

    transport(monkeypatch, respond)
    api = TuyaAPI(
        Settings(_env_file=None, tuya_client_id="client", tuya_client_secret=SecretStr("secret"))
    )
    result = api.discover()
    assert len(calls) == 3 and len(result) == 2 and all(d.lighting for d in result)
    assert "private" not in "".join(d.model_dump_json() for d in result)


@pytest.mark.parametrize("case", ["bad-shape", "missing-cursor", "repeated-cursor", "limit"])
def test_tuya_incomplete_discovery_fails_instead_of_returning_partial_inventory(case):
    api = TuyaAPI(Settings(_env_file=None))
    api.token = lambda: "synthetic"
    calls = []

    def result(*args):
        calls.append(1)
        if case == "bad-shape":
            return []
        return {
            "devices": [{"id": "hex-1", "name": "Up Arrow"}],
            "has_more": True,
            "last_row_key": ""
            if case == "missing-cursor"
            else str(len(calls))
            if case == "limit"
            else "same",
        }

    api._call = result
    with pytest.raises(ConnectedError):
        api.discover()
    assert len(calls) <= 10


def test_tuya_suspended_error_and_region_alias(monkeypatch):
    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"success": False, "code": 28841107, "msg": "private response"}
        ),
    )
    settings = Settings(
        _env_file=None,
        tuya_region="us-west",
        tuya_client_id="client",
        tuya_client_secret=SecretStr("secret"),
    )
    assert settings.tuya_region == "us"
    with pytest.raises(ConnectedError, match="data center is suspended"):
        TuyaAPI(settings).discover()


@pytest.mark.parametrize(
    "change",
    [
        {"device_ids": []},
        {"device_ids": ["a", "a"], "room": "Office"},
        {"device_ids": ["a"], "room": " Office"},
        {"device_ids": ["a"], "groups": ["Evening", "evening"]},
        {"device_ids": ["a"], "groups": [""]},
        {"device_ids": ["a"], "groups": ["x" * 101]},
        {"device_ids": ["a"], "room": "Office", "control_enabled": True},
    ],
)
def test_invalid_organization_cannot_change_device_authority(change):
    with pytest.raises(ValidationError):
        HomeOrganization.model_validate(change)


def test_model_discovery_and_organization_schemas():
    tools = definitions(
        (
            "home_refresh_devices",
            "home_organize_devices",
            "home_list_devices",
            "home_get_status",
            "home_control",
        )
    )
    assert all(t["strict"] and not t["parameters"]["additionalProperties"] for t in tools)
    organize = next(t for t in tools if t["name"] == "home_organize_devices")
    assert set(organize["parameters"]["required"]) == {"device_ids", "room", "groups"}
    assert "control_enabled" not in json.dumps(organize)
