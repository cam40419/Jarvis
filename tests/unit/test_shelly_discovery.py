from ipaddress import IPv4Address
from types import SimpleNamespace

import httpx

from simon.adapters import lan_discovery
from simon.adapters.home import ShellyAPI
from simon.config import Settings
from tests.unit.test_home_adapters import device, transport


def test_mdns_accepts_only_expected_model_names_private_ipv4_and_port(monkeypatch):
    good = "shellyplugusg4-0123456789ab"
    closed = []

    class FakeZeroconf:
        def __init__(self, **kwargs):
            pass

        def get_service_info(self, type_, name, timeout):
            assert name.startswith(good) and timeout == 700
            return SimpleNamespace(
                port=80,
                parsed_addresses=lambda version: [
                    "192.168.1.45",
                    "127.0.0.1",
                    "169.254.169.254",
                    "8.8.8.8",
                    "::1",
                ],
            )

        def close(self):
            closed.append("zeroconf")

    class FakeBrowser:
        def __init__(self, zc, types, listener):
            assert types == ["_shelly._tcp.local.", "_http._tcp.local."]
            for name in ("printer", "shellyplugusg4-bad", good, good):
                listener.add_service(zc, types[0], name + "." + types[0])

        def cancel(self):
            closed.append("browser")

    monkeypatch.setattr(lan_discovery, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(lan_discovery, "ServiceBrowser", FakeBrowser)
    assert lan_discovery.shelly_candidates(0) == ((good, IPv4Address("192.168.1.45")),)
    assert closed == ["browser", "zeroconf"]


def test_discovery_verifies_identity_without_switching(monkeypatch):
    remote = "shellyplugusg4-0123456789ab"
    monkeypatch.setattr(
        lan_discovery,
        "shelly_candidates",
        lambda: (
            (remote, IPv4Address("192.168.1.45")),
            ("shellyplugusg4-ffffffffffff", IPv4Address("192.168.1.46")),
        ),
    )
    calls = []

    def respond(request):
        calls.append(request.url.path)
        assert request.url.path == "/rpc/Shelly.GetDeviceInfo"
        return httpx.Response(
            200, json={"id": remote, "model": "S4PL-00116US", "gen": 4, "name": "Bedroom lamp"}
        )

    transport(monkeypatch, respond)
    rows = ShellyAPI(Settings(_env_file=None)).discover()
    assert len(rows) == 1 and rows[0].name == "Bedroom lamp" and not rows[0].lighting
    assert rows[0].address == IPv4Address("192.168.1.45")
    assert len(calls) == 2


def test_meter_reads_watts_energy_electrical_values_and_uptime(monkeypatch):
    plug = device("shelly")

    def respond(request):
        if request.url.path == "/rpc/Shelly.GetDeviceInfo":
            result = {"id": plug.remote_id, "model": "S4PL-00116US", "gen": 4}
        else:
            assert request.url.path == "/rpc/Shelly.GetStatus"
            result = {
                "sys": {"uptime": 4500},
                "switch:0": {
                    "output": True,
                    "apower": 24.5,
                    "voltage": 120.2,
                    "current": 0.204,
                    "freq": 60,
                    "temperature": {"tC": 32},
                    "aenergy": {"total": 230.5},
                },
            }
        return httpx.Response(200, json=result)

    transport(monkeypatch, respond)
    state = ShellyAPI(Settings(_env_file=None)).meter(plug)
    assert (state.watts, state.energy_wh, state.voltage, state.uptime_seconds) == (
        24.5,
        230.5,
        120.2,
        4500,
    )
    assert (state.current, state.frequency, state.temperature_c) == (0.204, 60, 32)
