"""Intake/model/source transactions remain authoritative after an app restart."""

import pytest

from tests.api.test_native_intake_api import (
    analyze_body,
    install_planner,
    save_settings,
    upload_source,
)
from tests.api.test_native_project_api import create_project
from tests.api.test_native_project_postgres_api import (
    login_owner,
)
from tests.api.test_native_project_postgres_api import (
    postgres_native_api as postgres_native_api,
)

pytestmark = pytest.mark.postgres


def test_intake_source_plan_staffing_and_receipts_survive_postgres_app_restart(postgres_native_api):
    with postgres_native_api() as (client, container):
        headers = login_owner(client)
        state = install_planner(container)
        project = create_project(client, headers)
        path = f"/v2/projects/{project['id']}/intake"
        save_settings(client, path, headers)
        source, upload = upload_source(client, path, headers, content=b"Original project evidence.")
        body = analyze_body(client, path, source_ids=[source["id"]])
        response = client.post(path + "/analyze", headers=headers, json=body)
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["status"] == "applied" and run["charged_microusd"] == 360
        assert len(state.calls) == 2
        cookies = dict(client.cookies)
        tasks = client.get(f"/v2/projects/{project['id']}/tasks").json()

    with postgres_native_api(cookies=cookies) as (restarted, container):
        state = install_planner(container)
        restored = restarted.get(path)
        assert restored.status_code == 200
        assert restored.json()["spent_microusd"] == 360
        assert restored.json()["reserved_microusd"] == 0
        assert restarted.get(path + f"/runs/{run['id']}").json() == run
        assert restarted.post(path + "/analyze", headers=headers, json=body).json() == run
        assert restarted.post(path + "/sources", headers=headers, json=upload).json() == source
        assert (
            restarted.get(path + f"/sources/{source['id']}/content").content
            == b"Original project evidence."
        )
        assert restarted.get(f"/v2/projects/{project['id']}/tasks").json() == tasks
        assert state.calls == []
