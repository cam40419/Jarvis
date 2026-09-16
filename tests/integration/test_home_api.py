from simon.domain.home import HomeControl, HomeStatus
from tests.contract.test_home_control import pending
from tests.contract.test_home_tools import prepare
from tests.unit.test_home_adapters import device


def test_home_inventory_status_and_action_api(client, container, auth_headers):
    service = container.connected
    _, actor = container.identity.resolve(client.cookies.get("simon_session"))
    light = device().model_copy(
        update={
            "id": "office-beam",
            "household_id": actor.household_id,
            "room": "Office",
            "load_type": "lighting",
            "control_enabled": True,
        }
    )
    service.home.devices = (light,)
    service.home.lifx.read = lambda device: HomeStatus(
        device_id=device.id,
        online=True,
        on=True,
        brightness=50,
        capabilities=("power", "brightness"),
    )
    calls = []
    service.home.lifx.set = lambda *args: calls.append(1)
    response = client.get("/v1/home/devices")
    assert response.status_code == 200 and response.json()[0]["id"] == "office-beam"
    assert light.remote_id not in response.text and "address" not in response.text
    assert client.get("/v1/home/devices/office-beam/status").json()["brightness"] == 50
    assert client.get("/v1/home/devices/unknown/status").status_code == 404
    action = prepare(service, actor)
    assert not calls
    path = f"/v1/actions/{action.id}/confirm"
    assert client.post(path, json={}).status_code == 403 and not calls
    response = client.post(path, headers=auth_headers, json={})
    assert response.status_code == 200 and response.json()["home_verified"]
    assert client.post(path, headers=auth_headers, json={}).json() == response.json()
    assert calls == [1]
    organize = {"device_ids": [light.id], "room": "Office", "groups": ["Desk lights"]}
    assert client.post("/v1/home/organize", json=organize).status_code == 403
    assert client.post("/v1/home/organize", headers=auth_headers, json=organize).status_code == 200
    assert client.get("/v1/home/devices").json()[0]["groups"] == ["Desk lights"]
    assert client.post("/v1/home/discovery/refresh", json={}).status_code == 403
    assert (
        client.post("/v1/home/discovery/refresh", headers=auth_headers, json={}).status_code == 200
    )
    assert client.get("/v1/home/discovery").status_code == 200
    attempt = pending(service, actor)
    service.home.control(
        actor, attempt.run.id, HomeControl(all_lights=True, on=False), lambda: actor
    )
    receipts = client.get("/v1/home/commands")
    assert receipts.status_code == 200
    assert receipts.json()[0]["status"] == "succeeded"
    assert receipts.json()[0]["change"]["on"] is False
    client.cookies.clear()
    assert client.get("/v1/home/commands").status_code == 401
    assert client.get("/v1/home/devices").status_code == 401
    assert client.get("/v1/home/devices/office-beam/status").status_code == 401
    assert client.get("/v1/home/discovery").status_code == 401
