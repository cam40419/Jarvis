import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from simon.adapters.google import ConnectedError
from simon.adapters.home import LIFXAPI, ShellyAPI, TuyaAPI, request_json
from simon.adapters.memory import InMemoryStore
from simon.config import Settings
from simon.domain.home import HomeChange, HomeDevice
from simon.services.audit import AuditService
from simon.services.home import HomeService


def device(provider="lifx", **changes):
    return HomeDevice.model_validate(
        {
            "id": "office-light",
            "household_id": str(uuid4()),
            "name": "Office Beam",
            "provider": provider,
            "remote_id": "d073d5000001"
            if provider == "lifx"
            else "tuya-fixture-1"
            if provider == "tuya"
            else "shellyplugusg4-000000000001",
            **({"address": "192.168.1.50"} if provider == "shelly" else {}),
            **changes,
        }
    )


def transport(monkeypatch, handler):
    original = httpx.Client

    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 8
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("simon.adapters.home.httpx.Client", factory)


def test_lifx_exact_beam_target_and_readback(monkeypatch):
    light = device()
    calls = []

    def respond(request):
        calls.append(request.method)
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.url.path.startswith("/v1/lights/id:d073d5000001")
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": light.remote_id, "connected": True, "power": "on", "brightness": 0.5}],
            )
        body = json.loads(request.content)
        assert body == {"duration": 0, "fast": False, "power": "on", "brightness": 0.5}
        return httpx.Response(207, json={"results": [{"id": light.remote_id, "status": "ok"}]})

    transport(monkeypatch, respond)
    api = LIFXAPI(Settings(_env_file=None, lifx_token=SecretStr("test-token")))
    assert api.read(light).brightness == 50
    api.set(light, HomeChange(device_id=light.id, on=True, brightness=50))
    assert calls == ["GET", "PUT"]


def test_lifx_empty_or_unconfirmed_response_is_not_success(monkeypatch):
    transport(
        monkeypatch, lambda req: httpx.Response(200, json={"results": [{"status": "timed_out"}]})
    )
    api = LIFXAPI(Settings(_env_file=None, lifx_token=SecretStr("test")))
    with pytest.raises(ConnectedError):
        api.read(device())
    with pytest.raises(ConnectedError) as error:
        api.set(device(), HomeChange(device_id="office-light", on=False))
    assert error.value.unknown
    with pytest.raises(ConnectedError):
        LIFXAPI(Settings(_env_file=None)).read(device())


def test_tuya_signed_requests_capability_bounds_and_token_reuse(monkeypatch):
    requests = []

    def respond(request):
        path = request.url.raw_path.decode()
        requests.append(path)
        token = request.headers.get("access_token", "")
        canonical = (
            request.method + "\n" + hashlib.sha256(request.content).hexdigest() + "\n\n" + path
        )
        expected = (
            hmac.new(
                b"secret",
                ("client" + token + request.headers["t"] + canonical).encode(),
                hashlib.sha256,
            )
            .hexdigest()
            .upper()
        )
        assert request.headers["sign"] == expected
        assert request.url.host == "openapi-ueaz.tuyaus.com"
        if path.startswith("/v1.0/token"):
            assert not token
            result = {"access_token": "access", "expire_time": 7200}
        else:
            assert token == "access"
            if path.endswith("specification"):
                result = {
                    "functions": [
                        {"code": "switch_led", "type": "Boolean"},
                        {
                            "code": "bright_value_v2",
                            "type": "Integer",
                            "values": '{"min":10,"max":1000,"step":1}',
                        },
                    ]
                }
            elif path.endswith("status"):
                result = [
                    {"code": "switch_led", "value": True},
                    {"code": "bright_value_v2", "value": 500},
                    {"code": "work_mode", "value": "white"},
                ]
            else:
                assert json.loads(request.content) == {
                    "commands": [
                        {"code": "switch_led", "value": False},
                        {"code": "bright_value_v2", "value": 500},
                    ]
                }
                result = True
        return httpx.Response(200, json={"success": True, "result": result})

    transport(monkeypatch, respond)
    api = TuyaAPI(
        Settings(
            _env_file=None,
            tuya_client_id="client",
            tuya_client_secret=SecretStr("secret"),
            tuya_region="us-east",
        )
    )
    light = device("tuya")
    state = api.read(light)
    assert state.on and state.brightness == 50 and state.online is None
    api.set(light, HomeChange(device_id=light.id, on=False, brightness=50))
    assert requests.count("/v1.0/token?grant_type=1") == 1


def test_tuya_unknown_dp_and_color_modes_do_not_send(monkeypatch):
    api = TuyaAPI(Settings(_env_file=None))
    api.properties = lambda device: (
        {"bright_value": {"type": "Integer", "values": {"min": 25, "max": 255}}},
        {"work_mode": "colour"},
    )
    assert api.read(device("tuya")).capabilities == ()
    with pytest.raises(ConnectedError):
        api.set(device("tuya"), HomeChange(device_id="office-light", brightness=50))
    with pytest.raises(ConnectedError):
        api.set(device("tuya"), HomeChange(device_id="office-light", on=True))
    with pytest.raises(ConnectedError):
        api.token()


def test_shelly_gen4_identity_digest_switch_and_metering(monkeypatch):
    plug = device("shelly")
    calls = []

    def respond(request):
        assert request.url.host == "192.168.1.50" and request.method == "POST"
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Digest qop="auth", realm="shelly-test", nonce="abcdef", algorithm=SHA-256'
                    )
                },
            )
        assert request.headers["authorization"].startswith("Digest ")
        assert 'username="admin"' in request.headers["authorization"]
        method = request.url.path.split("/")[-1]
        calls.append(method)
        if method == "Shelly.GetDeviceInfo":
            result = {"id": plug.remote_id, "model": "S4PL-00116US", "gen": 4}
        elif method == "Switch.GetStatus":
            assert json.loads(request.content) == {"id": 0}
            result = {"output": True, "apower": 24.2, "aenergy": {"total": 130}}
        else:
            assert method == "Switch.Set" and json.loads(request.content) == {"id": 0, "on": False}
            result = {"was_on": True}
        return httpx.Response(200, json=result)

    transport(monkeypatch, respond)
    api = ShellyAPI(Settings(_env_file=None, shelly_password=SecretStr("password")))
    state = api.read(plug)
    assert state.watts == 24.2 and state.energy_wh == 130
    api.set(plug, HomeChange(device_id=plug.id, on=False))
    assert calls.count("Switch.Set") == 1
    with pytest.raises(ConnectedError):
        api.set(plug, HomeChange(device_id=plug.id, brightness=30))


def test_shelly_wrong_device_never_switches(monkeypatch):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"id": "other-device", "model": "other", "gen": 4})

    transport(monkeypatch, respond)
    with pytest.raises(ConnectedError):
        ShellyAPI(Settings(_env_file=None)).set(
            device("shelly"), HomeChange(device_id="office-light", on=True)
        )
    assert len(calls) == 1 and "Switch.Set" not in calls[0]


@pytest.mark.parametrize("status,unknown", [(401, False), (302, False), (408, True), (500, True)])
def test_provider_errors_no_retry_no_private_body(monkeypatch, status, unknown):
    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(status, text="secret provider response")

    transport(monkeypatch, respond)
    with pytest.raises(ConnectedError) as error:
        request_json("POST", "https://api.lifx.com/test", write=True)
    assert error.value.unknown == unknown and "secret" not in str(error.value) and calls == [1]


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.169.254", "8.8.8.8", "224.0.0.1", "localhost", "192.168.1.2/path"],
)
def test_arbitrary_network_destinations_rejected(address):
    with pytest.raises(ValidationError):
        device("shelly", address=address)


@pytest.mark.parametrize("failure", ["timeout", "json", "size"])
def test_uncertain_http_failures_are_not_retried(monkeypatch, failure):
    calls = []

    def respond(request):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("private provider error", request=request)
        return httpx.Response(200, content=b"x" * (200001 if failure == "size" else 10))

    transport(monkeypatch, respond)
    with pytest.raises(ConnectedError) as error:
        request_json("POST", "https://api.lifx.com/test", write=True)
    assert error.value.unknown and "private" not in str(error.value) and calls == [1]


@pytest.mark.parametrize("body", [[], {"success": False}, {"success": True, "result": {}}])
def test_tuya_bad_token_responses(monkeypatch, body):
    transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    api = TuyaAPI(
        Settings(_env_file=None, tuya_client_id="test", tuya_client_secret=SecretStr("secret"))
    )
    with pytest.raises(ConnectedError):
        api.token()


@pytest.mark.parametrize("contents", ["{}", "invalid", "oversized", "duplicate", "too-many"])
def test_invalid_inventory_fails_closed(tmp_path, contents):
    if contents == "oversized":
        contents = "x" * 64001
    if contents == "duplicate":
        contents = json.dumps([device().model_dump(mode="json")] * 2)
    elif contents == "too-many":
        contents = json.dumps([device(id=f"light-{n}").model_dump(mode="json") for n in range(33)])
    inventory = tmp_path / "home.json"
    inventory.write_text(contents)
    store = InMemoryStore()
    with pytest.raises(ValueError, match="Invalid home device inventory"):
        HomeService(
            store, AuditService(store), Settings(_env_file=None, home_devices_file=inventory)
        )


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"on": "true"},
        {"brightness": 0},
        {"brightness": 101},
        {"brightness": True},
        {"on": True, "url": "https://example.com"},
    ],
)
def test_invalid_device_changes_rejected(change):
    with pytest.raises(ValidationError):
        HomeChange(device_id="office-light", **change)
