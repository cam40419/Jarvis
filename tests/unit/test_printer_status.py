import sqlite3
import time
from pathlib import Path

from simon.config import Settings
from simon.services.printer_status import PrinterStatusService, _env_values, summarize_report


def test_printer_report_exposes_only_status_fields():
    status = summarize_report({
        "command": "push_status", "gcode_state": "RUNNING", "mc_percent": "41",
        "mc_remaining_time": "68", "layer_num": "52", "total_layer_num": "120",
        "nozzle_temper": "208.5", "bed_temper": "60",
        "hms": [{"attr": 0x03001800, "code": 0x00010003}],
        "subtask_name": "test plate", "access_code": "do-not-return",
    })
    assert status["state"] == "RUNNING"
    assert status["progress_percent"] == 41
    assert status["remaining_minutes"] == 68
    assert status["alerts"] == 1
    assert status["error_code_hex"] is None
    assert status["hms_codes"] == ["HMS_0300_1800_0001_0003"]
    assert "access_code" not in status
    assert status["observed_at"]


def test_autoswap_credentials_reader_only_reads_known_keys(tmp_path: Path):
    source = tmp_path / ".env"
    source.write_text("BAMBU_HOST=192.168.1.2\nBAMBU_SERIAL='serial'\n"
                      "BAMBU_ACCESS_CODE=code # comment\nOTHER=ignored\n")
    assert _env_values(source) == {
        "BAMBU_HOST": "192.168.1.2", "BAMBU_SERIAL": "serial", "BAMBU_ACCESS_CODE": "code",
    }


def test_stopped_failed_job_can_be_homed_only_when_inactive_and_alert_free():
    stopped = {"gcode_state": "FAILED", "stg_cur": 255, "print_error": 0, "hms": []}
    assert summarize_report(stopped)["stopped_job_ready"] is True
    assert summarize_report({**stopped, "stg_cur": 4})["stopped_job_ready"] is False
    assert summarize_report({**stopped, "hms": [{"code": 1}]})["stopped_job_ready"] is False
    assert summarize_report({**stopped, "print_error": 123})["stopped_job_ready"] is False
    assert summarize_report({**stopped, "print_error": 123})["error_code_hex"] == "0x0000007B"


def test_finished_report_displays_complete_even_with_an_older_percentage():
    report = {"gcode_state": "FINISH", "mc_percent": 97, "mc_remaining_time": 1}
    assert summarize_report(report)["progress_percent"] == 100
    assert summarize_report(report)["remaining_minutes"] == 0
    assert summarize_report({**report, "gcode_state": "RUNNING"})["progress_percent"] == 97
    assert summarize_report({**report, "gcode_state": "IDLE"})["progress_percent"] == 97


def test_active_runner_telemetry_avoids_a_competing_printer_connection(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text(
        "BAMBU_HOST=192.168.1.2\nBAMBU_SERIAL=serial\nBAMBU_ACCESS_CODE=code\n"
    )
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE batches (id TEXT, state TEXT, heartbeat REAL)"
        )
        connection.execute(
            "CREATE TABLE jobs (batch_id TEXT, state TEXT, label TEXT, progress REAL, "
            "layer INTEGER, total_layers INTEGER, remaining_minutes INTEGER, "
            "updated REAL, position INTEGER)"
        )
        connection.execute(
            "INSERT INTO batches VALUES ('batch','running',?)", (time.time(),)
        )
        connection.execute(
            "INSERT INTO jobs VALUES "
            "('batch','running','second.gcode.3mf',42,12,30,8,?,1)",
            (time.time(),),
        )
    service = PrinterStatusService.__new__(PrinterStatusService)
    service.settings = Settings(_env_file=None, bambu_env_file=environment)
    status = service._active_runner_status()
    assert status is not None
    assert status["state"] == "RUNNING"
    assert status["progress_percent"] == 42
    assert status["source"] == "active_print_runner"


def test_active_runner_does_not_report_stale_telemetry_as_online(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text(
        "BAMBU_HOST=192.168.1.2\nBAMBU_SERIAL=serial\nBAMBU_ACCESS_CODE=code\n"
    )
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE batches (id TEXT, state TEXT, heartbeat REAL)")
        connection.execute(
            "CREATE TABLE jobs (batch_id TEXT, state TEXT, label TEXT, progress REAL, "
            "layer INTEGER, total_layers INTEGER, remaining_minutes INTEGER, "
            "updated REAL, position INTEGER)"
        )
        connection.execute("INSERT INTO batches VALUES ('batch','running',?)", (time.time(),))
        connection.execute(
            "INSERT INTO jobs VALUES "
            "('batch','running','second.gcode.3mf',42,12,30,8,?,1)",
            (time.time() - 31,),
        )
    service = PrinterStatusService.__new__(PrinterStatusService)
    service.settings = Settings(_env_file=None, bambu_env_file=environment)

    status = service._active_runner_status()

    assert status is not None
    assert status["online"] is False
    assert status["state"] == "unknown"
    assert "reconnecting" in status["message"]
