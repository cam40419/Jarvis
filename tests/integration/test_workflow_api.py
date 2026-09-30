import json
import shutil
import sqlite3
import zipfile
from contextlib import closing
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership


def test_workflow_delete_keeps_history_and_requires_active_runs_to_end(
    client, auth_headers
):
    created = client.post(
        "/v1/workflows",
        headers=auth_headers,
        json={
            "spec": {
                "name": "Disposable light test",
                "steps": [{"id": "echo", "action": "system.echo"}],
            },
            "expected_version": 0,
            "idempotency_key": str(uuid4()),
        },
    ).json()
    run = client.post(
        f"/v1/workflows/{created['id']}/runs",
        headers=auth_headers,
        json={"definition_version": 1, "idempotency_key": str(uuid4())},
    ).json()
    path = f"/v1/workflows/{created['id']}?expected_version=1"
    blocked = client.delete(path, headers=auth_headers)
    assert blocked.status_code == 409
    assert "cancel" in blocked.json()["error"]["message"]

    cancelled = client.post(
        f"/v1/workflow-runs/{run['id']}/control",
        headers=auth_headers,
        json={"action": "cancel", "expected_version": run["version"]},
    )
    assert cancelled.status_code == 200
    deleted = client.delete(path, headers=auth_headers)
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert client.get(f"/v1/workflows/{created['id']}").status_code == 404
    assert all(flow["id"] != created["id"] for flow in client.get("/v1/workflows").json())
    assert client.get(f"/v1/workflow-runs/{run['id']}").status_code == 200
    assert client.delete(path, headers=auth_headers).status_code == 404


def test_workflow_api_auth_csrf_versions_runs_and_health(client, auth_headers, container):
    body = {
        "spec": {"name": "API demo", "steps": [{"id": "echo", "action": "system.echo"}]},
        "expected_version": 0,
        "idempotency_key": str(uuid4()),
    }
    assert client.post("/v1/workflows", json=body).status_code == 403
    created = client.post("/v1/workflows", headers=auth_headers, json=body)
    assert created.status_code == 201
    definition = created.json()
    identifier = definition["id"]
    assert client.post("/v1/workflows", headers=auth_headers, json=body).json() == definition
    assert client.get("/v1/workflows").json() == [definition]
    assert client.get(f"/v1/workflows/{identifier}?version=1").json() == definition
    assert not client.get("/v1/workflows/health").json()["worker_online"]
    revised = client.post(
        f"/v1/workflows/{identifier}/versions",
        headers=auth_headers,
        json={**body, "expected_version": 1, "idempotency_key": str(uuid4())},
    )
    assert revised.json()["version"] == 2
    response = client.post(
        f"/v1/workflows/{identifier}/runs",
        headers=auth_headers,
        json={"definition_version": 1, "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 201
    run_id = response.json()["id"]
    assert client.get("/v1/workflow-runs").json()[0]["id"] == run_id
    assert (
        client.post(
            f"/v1/workflow-runs/{run_id}/control", json={"action": "pause", "expected_version": 1}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/workflow-runs/{run_id}/control",
            headers=auth_headers,
            json={"action": "resume", "expected_version": 1},
        ).status_code
        == 409
    )
    container.workflows.tick("api-test")
    container.workflows.tick("api-test")
    assert client.get(f"/v1/workflow-runs/{run_id}").json()["status"] == "succeeded"
    assert client.get(f"/v1/workflow-runs/{run_id}/events").json()[-1]["type"] == "succeeded"
    assert client.get("/v1/workflows/health").json()["worker_online"]
    assert client.get("/v1/workflow-runs?limit=1000").status_code == 422
    container.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, household_id=DEV_HOUSEHOLD_ID, role="guest")
    )
    assert client.get("/v1/workflows").status_code == 403
    client.cookies.clear()
    assert client.get("/v1/workflow-runs").status_code == 401


def test_automations_page_and_printer_status_are_account_scoped(
    client, auth_headers, container, monkeypatch
):
    page = client.get("/automations")
    assert page.status_code == 200 and "Build a workflow" in page.text
    assert 'id="trigger-drafts"' in page.text
    assert 'id="schedule-drafts"' in page.text
    assert 'id="save-workflow"' in page.text
    assert 'href="/assets/workflow-studio.css"' in page.text
    script = client.get("/assets/automations.js")
    assert script.status_code == 200
    assert "Use print air-cleaning example" in script.text
    assert "condition: 'printer.not_printing'" in script.text
    assert "delay_seconds: 600" in script.text
    assert "Start workflow at" in script.text
    assert "start_step_id" in script.text
    assert "+ Add action at this step" in script.text
    assert "Timeline step" in script.text
    assert "stage.actions.children" in script.text
    client.cookies.clear()
    assert client.get("/v1/printers/a1/status").status_code == 401
    client.post(
        "/auth/dev-login", headers={"Origin": "http://localhost:8000"},
        json={"token": "test-development-secret-32-characters"},
    )
    monkeypatch.setattr(container.printer_status, "_read", lambda *_: {
        "configured": True, "online": True, "state": "IDLE", "progress_percent": 0,
    })
    monkeypatch.setattr(
        container.printer_status, "_credentials", lambda: ("host", "serial", "code")
    )
    status = client.get("/v1/printers/a1/status")
    assert status.status_code == 200 and status.json()["state"] == "IDLE"
    assert "code" not in status.text


def activate_test_swap(client, auth_headers, container, state="verified"):
    trial = client.post(
        "/v1/printers/a1/swap-trials", headers=auth_headers,
        json={"gcode": "G90\nG1 X10 F1000\nG90\n"},
    )
    assert trial.status_code == 201, trial.text
    trial_id = trial.json()["id"]
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute("UPDATE swap_trials SET state=? WHERE id=?", (state, trial_id))
    activated = client.post(
        f"/v1/printers/a1/swap-trials/{trial_id}/activate", headers=auth_headers, json={},
    )
    assert activated.status_code == 200, activated.text


def test_supervised_print_batch_stages_without_starting_printer(
    client, auth_headers, container, tmp_path, monkeypatch
):
    source_root = Path(__file__).resolve().parents[3] / "autoswap_rip"
    for name in ("workflow.py", "swaptool.py", "simon_bridge.py", "eject_sequence.gcode"):
        shutil.copyfile(source_root / name, tmp_path / name)
    container.print_batches.root = tmp_path
    container.print_batches.db = tmp_path / "queue.sqlite3"
    activate_test_swap(client, auth_headers, container, state="configured")
    sequence = client.get("/v1/printers/a1/swap-sequence").json()
    assert "; Move 01: X10 mm (absolute); F1000 mm/min" in sequence["gcode"]
    assert "\n".join(line for line in sequence["gcode"].splitlines() if not line.startswith(";")) == "G90\nG1 X10 F1000\nG90"
    plate = """; EXECUTABLE_BLOCK_START
G28
G1 X10 Y10 Z0.2 E1 F1000
M104 S0
G1 X-48 Y180 F3600
M400
M18 X Y Z
M73 P100 R0
; EXECUTABLE_BLOCK_END
"""
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Metadata/plate_1.gcode", plate)
        output.writestr("Metadata/plate_1.json", "{}")
    data = archive.getvalue()
    response = client.post(
        "/v1/printers/a1/batches", headers=auth_headers,
        data={"metadata": json.dumps([
            {"plate": 1, "use_ams": False}, {"plate": 1, "use_ams": False},
        ])},
        files=[
            ("files", ("first.gcode.3mf", data, "application/octet-stream")),
            ("files", ("second.gcode.3mf", data, "application/octet-stream")),
        ],
    )
    assert response.status_code == 201, response.text
    batch_id = response.json()["id"]
    batches = client.get("/v1/printers/a1/batches").json()
    assert len(batches) == 1 and batches[0]["state"] == "staged"
    assert [job["swap_enabled"] for job in batches[0]["jobs"]] == [1, 0]
    prepared = client.get(
        f"/v1/printers/a1/batches/{batch_id}/jobs/{batches[0]['jobs'][0]['id']}/prepared"
    )
    assert prepared.status_code == 200 and prepared.content.startswith(b"PK")
    with zipfile.ZipFile(BytesIO(prepared.content)) as result:
        gcode = result.read("Metadata/plate_1.gcode").decode()
    assert gcode.index("G1 X-48 Y180 F3600") < gcode.index("; AUTOSWAP_BEGIN")
    assert gcode.index("; AUTOSWAP_END") < gcode.index("M18 X Y Z")
    assert (client.post(
        f"/v1/printers/a1/batches/{batch_id}/start", headers=auth_headers,
        json={"operator_present": False},
    ).status_code == 422)
    launched = []
    monkeypatch.setattr(
        "simon.api.print_batches.subprocess.Popen", lambda *a, **kw: launched.append(a)
    )
    for unavailable in (
        {"online": False, "state": "unknown"},
        {"online": True, "state": "RUNNING", "alerts": 0},
        {"online": True, "state": "FAILED", "alerts": 0},
        {"online": True, "state": "FAILED", "stopped_job_ready": True, "alerts": 1},
    ):
        monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: unavailable)
        rejected = client.post(
            f"/v1/printers/a1/batches/{batch_id}/start", headers=auth_headers,
            json={"operator_present": True},
        )
        assert rejected.status_code == 422
        assert not launched
        assert client.get("/v1/printers/a1/batches").json()[0]["state"] == "staged"
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "FAILED", "stopped_job_ready": True, "alerts": 0,
    })
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute("UPDATE batches SET sequence_sha256='outdated' WHERE id=?", (batch_id,))
    stale = client.post(
        f"/v1/printers/a1/batches/{batch_id}/start", headers=auth_headers,
        json={"operator_present": True},
    )
    assert stale.status_code == 422 and "active swap changed" in stale.text
    assert not launched
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute("UPDATE batches SET sequence_sha256=? WHERE id=?", (sequence["sequence_sha256"], batch_id))
    started = client.post(
        f"/v1/printers/a1/batches/{batch_id}/start", headers=auth_headers,
        json={"operator_present": True, "auto_continue": True},
    )
    assert started.status_code == 200 and launched
    active = client.get("/v1/printers/a1/batches").json()[0]
    assert active["state"] == "running" and active["auto_continue"] is True
    # Double-clicks cannot launch a second runner for an already-started batch.
    assert client.post(
        f"/v1/printers/a1/batches/{batch_id}/start", headers=auth_headers,
        json={"operator_present": True, "auto_continue": True},
    ).status_code == 422
    assert len(launched) == 1
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "IDLE", "alerts": 0,
    })
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute("UPDATE batches SET state='waiting_for_plate' WHERE id=?", (batch_id,))
        database.execute("UPDATE jobs SET state='done' WHERE id=?", (batches[0]["jobs"][0]["id"],))
    blocked = client.post(
        f"/v1/printers/a1/batches/{batch_id}/start", headers=auth_headers,
        json={"operator_present": True, "plate_checked": False},
    )
    assert blocked.status_code == 422 and len(launched) == 1
    continued = client.post(
        f"/v1/printers/a1/batches/{batch_id}/start", headers=auth_headers,
        json={"operator_present": True, "plate_checked": True},
    )
    assert continued.status_code == 200 and len(launched) == 2
    assert client.get("/v1/printers/a1/batches").json()[0]["state"] == "running"
    assert client.delete(f"/v1/printers/a1/batches/{batch_id}", headers=auth_headers).status_code == 422
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute("UPDATE jobs SET state='canceled' WHERE batch_id=? AND state='queued'", (batch_id,))
        database.execute("UPDATE batches SET state='canceled' WHERE id=?", (batch_id,))
    assert client.delete(f"/v1/printers/a1/batches/{batch_id}", headers=auth_headers).status_code == 200
    assert client.get("/v1/printers/a1/batches").json() == []
    assert not (tmp_path / "spool" / "uploads" / batch_id).exists()


def test_one_sliced_file_can_be_repeated_and_its_plate_is_detected(
    client, auth_headers, container, tmp_path
):
    source_root = Path(__file__).resolve().parents[3] / "autoswap_rip"
    for name in ("workflow.py", "swaptool.py", "simon_bridge.py", "eject_sequence.gcode"):
        shutil.copyfile(source_root / name, tmp_path / name)
    container.print_batches.root = tmp_path
    container.print_batches.db = tmp_path / "queue.sqlite3"
    activate_test_swap(client, auth_headers, container)
    plate = """; EXECUTABLE_BLOCK_START
G28
G1 X10 Y10 Z0.2 E1 F1000
M104 S0
G1 X-48 Y180 F3600
M400
M18 X Y Z
M73 P100 R0
; EXECUTABLE_BLOCK_END
"""
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Metadata/plate_2.gcode", plate)
        output.writestr("Metadata/plate_2.json", "{}")
    invalid = client.post(
        "/v1/printers/a1/batches", headers=auth_headers,
        data={"metadata": json.dumps([{"file_index": 0, "use_ams": True, "ams_slot": 5}])},
        files=[("files", ("repeat.3mf", archive.getvalue(), "application/octet-stream"))],
    )
    assert invalid.status_code == 422
    same_slot = client.post(
        "/v1/printers/a1/batches", headers=auth_headers,
        data={"metadata": json.dumps([{
            "file_index": 0, "use_ams": True, "ams_slot": 3, "backup_ams_slot": 3,
        }])},
        files=[("files", ("repeat.3mf", archive.getvalue(), "application/octet-stream"))],
    )
    assert same_slot.status_code == 422
    response = client.post(
        "/v1/printers/a1/batches", headers=auth_headers,
        data={"metadata": json.dumps([
            {"file_index": 0, "use_ams": True, "ams_slot": 3, "backup_ams_slot": 4}
            for _ in range(3)
        ])},
        files=[("files", ("repeat.3mf", archive.getvalue(), "application/octet-stream"))],
    )
    assert response.status_code == 201, response.text
    batch = client.get("/v1/printers/a1/batches").json()[0]
    assert [job["plate"] for job in batch["jobs"]] == [2, 2, 2]
    assert [job["ams_slot"] for job in batch["jobs"]] == [3, 3, 3]
    assert [job["backup_ams_slot"] for job in batch["jobs"]] == [4, 4, 4]
    assert [job["swap_enabled"] for job in batch["jobs"]] == [1, 1, 0]
    assert [job["label"] for job in batch["jobs"]] == ["repeat.3mf"] * 3


def test_repeat_saved_sequence_preserves_order_slots_and_swaps_between_runs(
    client, auth_headers, container, tmp_path,
):
    source_root = Path(__file__).resolve().parents[3] / "autoswap_rip"
    for name in ("workflow.py", "swaptool.py", "simon_bridge.py", "eject_sequence.gcode"):
        shutil.copyfile(source_root / name, tmp_path / name)
    container.print_batches.root = tmp_path
    container.print_batches.db = tmp_path / "queue.sqlite3"
    activate_test_swap(client, auth_headers, container)
    files = []
    for name in ("A", "B"):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(
                "Metadata/plate_1.gcode",
                f"; EXECUTABLE_BLOCK_START\nG28\n; Source {name}\nG1 X1\n"
                "M18 X Y Z\n; EXECUTABLE_BLOCK_END\n",
            )
            output.writestr("Metadata/plate_1.json", "{}")
        files.append(("files", (f"{name}.3mf", archive.getvalue(), "application/octet-stream")))
    original = client.post(
        "/v1/printers/a1/batches", headers=auth_headers,
        data={"metadata": json.dumps([
            {"file_index": 1, "use_ams": True, "ams_slot": 3, "backup_ams_slot": 4},
            {"file_index": 0, "use_ams": False},
        ])}, files=files,
    )
    assert original.status_code == 201, original.text
    original_id = original.json()["id"]
    repeat_route = f"/v1/printers/a1/batches/{original_id}/repeat"
    assert client.post(repeat_route, headers=auth_headers, json={"count": 2}).status_code == 422
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute("UPDATE jobs SET state='done' WHERE batch_id=?", (original_id,))
        database.execute("UPDATE batches SET state='done' WHERE id=?", (original_id,))
    assert client.post(repeat_route, json={"count": 2}).status_code == 403
    for count in (0, 5, True):
        assert client.post(repeat_route, headers=auth_headers, json={"count": count}).status_code == 422
    repeated = client.post(repeat_route, headers=auth_headers, json={"count": 2})
    assert repeated.status_code == 201, repeated.text
    repeated_id = repeated.json()["id"]
    batch = next(item for item in client.get("/v1/printers/a1/batches").json() if item["id"] == repeated_id)
    assert batch["state"] == "staged"
    assert [job["label"] for job in batch["jobs"]] == ["B.3mf", "A.3mf", "B.3mf", "A.3mf"]
    assert [job["ams_slot"] for job in batch["jobs"]] == [3, None, 3, None]
    assert [job["backup_ams_slot"] for job in batch["jobs"]] == [4, None, 4, None]
    assert [job["state"] for job in batch["jobs"]] == ["queued"] * 4
    assert [job["swap_enabled"] for job in batch["jobs"]] == [1, 1, 1, 0]
    for index, job in enumerate(batch["jobs"]):
        prepared = client.get(
            f"/v1/printers/a1/batches/{repeated_id}/jobs/{job['id']}/prepared"
        )
        with zipfile.ZipFile(BytesIO(prepared.content)) as archive:
            gcode = archive.read("Metadata/plate_1.gcode").decode()
        assert gcode.count("; AUTOSWAP_BEGIN") == (1 if index < 3 else 0)
        assert f"; Source {'B' if index % 2 == 0 else 'A'}" in gcode
        assert "M18 X Y Z" in gcode
        if index < 3:
            assert gcode.index("; AUTOSWAP_END") < gcode.index("M18 X Y Z")
    # A repeat owns copies of its source files, so removing the older batch
    # cannot break another repeat or download.
    assert client.delete(f"/v1/printers/a1/batches/{original_id}", headers=auth_headers).status_code == 200
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute("UPDATE jobs SET state='done' WHERE batch_id=?", (repeated_id,))
        database.execute("UPDATE batches SET state='done' WHERE id=?", (repeated_id,))
    again = client.post(
        f"/v1/printers/a1/batches/{repeated_id}/repeat", headers=auth_headers, json={"count": 1},
    )
    assert again.status_code == 201, again.text
    assert len(again.json()["jobs"]) == 4


def test_swap_only_trial_requires_homing_and_inspection(
    client, auth_headers, container, tmp_path, monkeypatch
):
    source_root = Path(__file__).resolve().parents[3] / "autoswap_rip"
    for name in ("workflow.py", "swaptool.py", "simon_bridge.py", "eject_sequence.gcode"):
        shutil.copyfile(source_root / name, tmp_path / name)
    container.print_batches.root = tmp_path
    container.print_batches.db = tmp_path / "queue.sqlite3"
    invalid = client.post(
        "/v1/printers/a1/swap-trials", headers=auth_headers,
        json={"gcode": "G90\nM104 S200\nG1 X10\nG90\n"},
    )
    assert invalid.status_code == 422
    valid = client.post(
        "/v1/printers/a1/swap-trials", headers=auth_headers,
        json={"gcode": "G90\nG1 X10 F1000\nG91\nG1 X-10\nG90\n"},
    )
    assert valid.status_code == 201, valid.text
    trial = valid.json()
    assert trial["moves"] == 2
    assert client.get("/v1/printers/a1/swap-trials").json()[0]["state"] == "validated"
    assert client.post(
        f"/v1/printers/a1/swap-trials/{trial['id']}/activate", headers=auth_headers,
        json={},
    ).status_code == 422
    launched = []
    original_popen = __import__("subprocess").Popen
    monkeypatch.setattr(
        "simon.api.print_batches.subprocess.Popen", lambda *args, **kwargs: launched.append(args)
    )
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "IDLE", "alerts": 0, "homed_axes": "XY",
    })
    path = f"/v1/printers/a1/swap-trials/{trial['id']}/start"
    assert client.post(
        path, headers=auth_headers, json={"operator_present": True}
    ).status_code == 422
    assert not launched
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "FAILED", "stopped_job_ready": True,
        "alerts": 0, "homed_axes": "XYZ",
    })
    assert client.post(
        path, headers=auth_headers, json={"operator_present": False}
    ).status_code == 422
    assert client.post(
        path, headers=auth_headers, json={"operator_present": True}
    ).status_code == 200
    assert len(launched) == 1 and "run-swap" in launched[0][0]
    assert client.post(
        path, headers=auth_headers, json={"operator_present": True}
    ).status_code == 422
    monkeypatch.setattr("simon.api.print_batches.subprocess.Popen", original_popen)
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute(
            "UPDATE swap_trials SET state='accepted_unverified' WHERE id=?", (trial["id"],)
        )
    another = client.post(
        "/v1/printers/a1/swap-trials", headers=auth_headers,
        json={"gcode": "G90\nG1 X5 F1000\nG90\n"},
    ).json()
    blocked = client.post(
        f"/v1/printers/a1/swap-trials/{another['id']}/start", headers=auth_headers,
        json={"operator_present": True},
    )
    assert blocked.status_code == 422 and trial["id"][:8] in blocked.text
    assert client.get("/v1/printers/a1/swap-trials").json()[0]["id"] == trial["id"]
    assert client.post(
        f"/v1/printers/a1/swap-trials/{trial['id']}/resolve", headers=auth_headers,
        json={"outcome": "verified", "inspected": False},
    ).status_code == 422
    assert client.post(
        f"/v1/printers/a1/swap-trials/{trial['id']}/resolve", headers=auth_headers,
        json={"outcome": "verified", "inspected": True},
    ).status_code == 200
    assert client.post(
        f"/v1/printers/a1/swap-trials/{trial['id']}/activate", headers=auth_headers,
        json={},
    ).status_code == 200
    sequence = client.get("/v1/printers/a1/swap-sequence").json()
    assert sequence["active_trial_id"] == trial["id"]
    assert "G1 X10 F1000" in sequence["gcode"]
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "Metadata/plate_1.gcode",
            "; EXECUTABLE_BLOCK_START\nG28\nG1 X1\nM18 X Y Z\n"
            "; EXECUTABLE_BLOCK_END\n",
        )
        output.writestr("Metadata/plate_1.json", "{}")
    batch = client.post(
        "/v1/printers/a1/batches", headers=auth_headers,
        data={"metadata": json.dumps([
            {"plate": 1, "use_ams": False}, {"plate": 1, "use_ams": False},
        ])},
        files=[
            ("files", ("first.gcode.3mf", archive.getvalue(), "application/octet-stream")),
            ("files", ("second.gcode.3mf", archive.getvalue(), "application/octet-stream")),
        ],
    )
    assert batch.status_code == 201, batch.text
    assert batch.json()["sequence_sha256"] == trial["sha256"]
    staged = client.get("/v1/printers/a1/batches").json()[0]
    assert staged["sequence_sha256"] == trial["sha256"]
    prepared = client.get(
        f"/v1/printers/a1/batches/{staged['id']}/jobs/{staged['jobs'][0]['id']}/prepared"
    )
    with zipfile.ZipFile(BytesIO(prepared.content)) as result:
        gcode = result.read("Metadata/plate_1.gcode").decode()
    assert "; AUTOSWAP_BEGIN\n" + trial["gcode"].rstrip() + "\n; AUTOSWAP_END\n" in gcode
    assert gcode.index("; AUTOSWAP_END") < gcode.index("M18 X Y Z")
    assert not (tmp_path / "spool" / "uploads" / staged["id"] / "batch-swap.gcode").exists()
    assert client.delete(
        f"/v1/printers/a1/batches/{staged['id']}", headers=auth_headers,
    ).status_code == 200
    assert client.delete(
        f"/v1/printers/a1/swap-trials/{trial['id']}", headers=auth_headers,
    ).status_code == 200
    assert client.get("/v1/printers/a1/swap-sequence").json()["active_trial_id"] == ""
    assert all(item["id"] != trial["id"] for item in client.get("/v1/printers/a1/swap-trials").json())


def test_home_axes_control_tracks_operator_verified_result(
    client, auth_headers, container, tmp_path, monkeypatch
):
    source_root = Path(__file__).resolve().parents[3] / "autoswap_rip"
    for name in ("workflow.py", "swaptool.py", "simon_bridge.py", "eject_sequence.gcode"):
        shutil.copyfile(source_root / name, tmp_path / name)
    container.print_batches.root = tmp_path
    container.print_batches.db = tmp_path / "queue.sqlite3"
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "FAILED", "stopped_job_ready": True,
        "alerts": 0, "homed_axes": "",
    })
    launched = []
    monkeypatch.setattr(container.print_batches, "launch_home", launched.append)
    route = "/v1/printers/a1/home"
    assert client.post(
        route, headers=auth_headers, json={"operator_present": False}
    ).status_code == 422
    response = client.post(route, headers=auth_headers, json={"operator_present": True})
    assert response.status_code == 202, response.text
    attempt_id = response.json()["id"]
    assert launched == [attempt_id]
    assert client.get("/v1/printers/a1/home-attempts").json()[0]["state"] == "running"
    assert client.post(
        route, headers=auth_headers, json={"operator_present": True}
    ).status_code == 422
    with closing(sqlite3.connect(container.print_batches.db)) as database, database:
        database.execute(
            "UPDATE homing_attempts SET state='accepted_unverified' WHERE id=?", (attempt_id,)
        )
    resolve = f"/v1/printers/a1/home-attempts/{attempt_id}/resolve"
    assert client.post(
        resolve, headers=auth_headers, json={"outcome": "verified", "inspected": True}
    ).status_code == 422
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "IDLE", "alerts": 0, "homed_axes": "XYZ",
    })
    assert client.post(
        resolve, headers=auth_headers, json={"outcome": "verified", "inspected": True}
    ).status_code == 200
    assert client.get("/v1/printers/a1/home-attempts").json()[0]["state"] == "verified"


def test_direct_printer_controls_require_ready_status(
    client, auth_headers, container, tmp_path, monkeypatch
):
    bridge = tmp_path / "simon_bridge.py"
    bridge.write_text(
        "import json,sys\n"
        "print(json.dumps({'action':sys.argv[1].removeprefix('direct-'),"
        "'state':'accepted'}))\n",
        encoding="utf-8",
    )
    container.print_batches.root = tmp_path
    container.print_batches.db = tmp_path / "queue.sqlite3"
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "IDLE", "alerts": 0, "homed_axes": "",
    })
    route = "/v1/printers/a1/control"
    assert client.post(route, json={"action": "home"}).status_code == 403
    assert client.post(
        route, headers=auth_headers, json={"action": "swap"}
    ).status_code == 422
    home = client.post(route, headers=auth_headers, json={"action": "home"})
    assert home.status_code == 200 and home.json()["action"] == "home"
    home_swap = client.post(route, headers=auth_headers, json={"action": "home_swap"})
    assert home_swap.status_code == 200 and home_swap.json()["action"] == "home_swap"
    monkeypatch.setattr(container.print_batches.printer, "status", lambda actor: {
        "online": True, "state": "IDLE", "alerts": 0, "homed_axes": "XYZ",
    })
    swap = client.post(route, headers=auth_headers, json={"action": "swap"})
    assert swap.status_code == 200 and swap.json()["action"] == "swap"
