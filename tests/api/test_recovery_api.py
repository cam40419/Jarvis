"""Recovery retains authenticated reads while rejecting core model dispatch."""

from uuid import uuid4

from simon.services.recovery import RecoveryGuard
from tests.api.test_native_models_api import configure_model_http, enroll_model
from tests.api.test_native_project_api import create_project


def test_quarantine_status_and_probe_do_not_expose_operator_state(
    client, container, auth_headers, tmp_path
):
    transport = configure_model_http(container)
    project = create_project(client, auth_headers)
    path = f"/v2/projects/{project['id']}/models"
    model, _ = enroll_model(client, path, auth_headers)
    guard = RecoveryGuard(tmp_path / "missing-private-witness.json")
    container.recovery = container.model_usage.recovery = guard
    status = client.get("/v2/recovery")
    assert status.status_code == 200
    assert status.json() == {"managed": True, "dispatch_allowed": False}
    assert client.get(path).status_code == 200
    response = client.post(
        path + f"/connections/{model['id']}/probe",
        headers=auth_headers,
        json={"expected_version": model["version"], "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "recovery_quarantined"
    assert transport.calls == []
    assert "missing-private-witness" not in response.text


def test_recovery_status_requires_current_session(client):
    assert client.get("/v2/recovery").status_code == 401
