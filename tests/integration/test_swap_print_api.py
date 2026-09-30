import shutil
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path


def test_print_mode_test_preparation_auth_homing_start_and_cleanup(
    client, auth_headers, container, tmp_path, monkeypatch
):
    source = Path(__file__).resolve().parents[3] / "autoswap_rip"
    for name in ("workflow.py", "swaptool.py", "simon_bridge.py", "swap_print_template.gcode.3mf"):
        shutil.copyfile(source / name, tmp_path / name)
    service = container.print_batches
    service.root = tmp_path
    service.db = tmp_path / "queue.sqlite3"
    body = {"gcode": "G90\nG1 X0 Z260 F900\nG1 Y260 F3000\nG90\n", "motion_profile": "slow"}
    url = "/v1/printers/a1/swap-print-tests"
    assert client.post(url, json=body).status_code == 403
    assert (
        client.post(url, headers=auth_headers, json={**body, "gcode": "G90\nG28\nG90"}).status_code
        == 422
    )
    response = client.post(url, headers=auth_headers, json=body)
    assert response.status_code == 201, response.text
    test = response.json()
    assert client.post(url, headers=auth_headers, json=body).status_code == 422
    record = client.get("/v1/printers/a1/batches").json()[0]
    assert record["state"] == "staged" and record["jobs"][0]["kind"] == "swap_test"
    with closing(sqlite3.connect(service.db)) as db:
        path = Path(db.execute("SELECT prepared FROM jobs").fetchone()[0])
        with zipfile.ZipFile(path) as archive:
            commands = archive.read("Metadata/plate_1.gcode").decode()
            executable = commands.split("; EXECUTABLE_BLOCK_START\n", 1)[1]
            assert "M204 S500" in executable and "G28" not in executable
            assert "; CONFIG_BLOCK_START\n" in commands
    launched = []
    original_popen = __import__("subprocess").Popen
    monkeypatch.setattr(
        "simon.api.print_batches.subprocess.Popen", lambda *a, **k: launched.append(a)
    )
    monkeypatch.setattr(
        service.printer,
        "status",
        lambda actor: {"online": True, "state": "IDLE", "homed_axes": "XY"},
    )
    start = f"/v1/printers/a1/batches/{test['id']}/start"
    assert (
        client.post(start, headers=auth_headers, json={"operator_present": True}).status_code == 422
    )
    assert not launched
    monkeypatch.setattr(
        service.printer,
        "status",
        lambda actor: {"online": True, "state": "IDLE", "homed_axes": "XYZ"},
    )
    assert (
        client.post(start, headers=auth_headers, json={"operator_present": True}).status_code == 200
    )
    assert len(launched) == 1
    assert (
        client.post(start, headers=auth_headers, json={"operator_present": True}).status_code == 422
    )
    assert len(launched) == 1
    monkeypatch.setattr("simon.api.print_batches.subprocess.Popen", original_popen)
    with closing(sqlite3.connect(service.db)) as db, db:
        db.execute("UPDATE jobs SET state='done'")
        db.execute("UPDATE batches SET state='done'")
    assert (
        client.post(
            f"/v1/printers/a1/batches/{test['id']}/repeat", headers=auth_headers, json={"count": 1}
        ).status_code
        == 422
    )
    assert (
        client.delete(f"/v1/printers/a1/batches/{test['id']}", headers=auth_headers).status_code
        == 200
    )
    assert not path.exists()
    assert (
        client.post(
            url, headers=auth_headers, json={**body, "motion_profile": "current"}
        ).status_code
        == 201
    )


def test_active_print_test_ignores_stale_editor_and_includes_annotated_master(
    client, auth_headers, container, tmp_path
):
    source = Path(__file__).resolve().parents[3] / "autoswap_rip"
    for name in ("workflow.py", "swaptool.py", "simon_bridge.py", "swap_print_template.gcode.3mf"):
        shutil.copyfile(source / name, tmp_path / name)
    service = container.print_batches
    service.root, service.db = tmp_path, tmp_path / "queue.sqlite3"
    trial = client.post("/v1/printers/a1/swap-trials", headers=auth_headers,
                        json={"gcode": "G90\nG1 Y260 F3000\nG91\nG1 Y-212.2 F3000\nG90\n"}).json()
    with closing(sqlite3.connect(service.db)) as db, db:
        db.execute("UPDATE swap_trials SET state='configured' WHERE id=?", (trial["id"],))
    activated = client.post(f"/v1/printers/a1/swap-trials/{trial['id']}/activate", headers=auth_headers, json={})
    assert activated.status_code == 200, activated.text
    result = client.post("/v1/printers/a1/swap-print-tests", headers=auth_headers,
                         json={"source": "active", "motion_profile": "current", "gcode": "G90\nG1 Y999 F999\nG90\n"})
    assert result.status_code == 201, result.text
    assert result.json()["sequence_sha256"] == trial["sha256"]
    with closing(sqlite3.connect(service.db)) as db:
        path = Path(db.execute("SELECT prepared FROM jobs").fetchone()[0])
    with zipfile.ZipFile(path) as archive:
        gcode = archive.read("Metadata/plate_1.gcode").decode().split("; EXECUTABLE_BLOCK_START", 1)[1]
    assert "Y999" not in gcode
    assert "; Move 02: BACK 8.354 in (212.2 mm)" in gcode
    assert "G1 Y-212.2 F3000" in gcode
