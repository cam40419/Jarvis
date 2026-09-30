import json

from simon.adapters.model_tools import definitions


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
