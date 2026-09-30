"""Read-only Bambu A1 LAN status. No print or motion commands are available here."""

import json
import sqlite3
import ssl
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import uuid4

from simon.config import Settings
from simon.domain.errors import AuthorizationError
from simon.domain.identity import DEV_HOUSEHOLD_ID
from simon.domain.models import ActorContext
from simon.services.identity import ROLE_SCOPES, IdentityService


def _env_values(path: Path) -> dict[str, str]:
    """Read only the three known AutoSwap settings without executing its file."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in {"BAMBU_HOST", "BAMBU_SERIAL", "BAMBU_ACCESS_CODE"}:
            values[key] = value.strip('"\'').split(" #", 1)[0].strip()
    return values


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hms_codes(alerts: Any) -> list[str]:
    result = []
    for alert in alerts if isinstance(alerts, list) else []:
        if not isinstance(alert, dict):
            continue
        try:
            attribute, code = int(alert["attr"]), int(alert["code"])
        except (KeyError, TypeError, ValueError):
            continue
        result.append(
            f"HMS_{attribute >> 16:04X}_{attribute & 0xFFFF:04X}_"
            f"{code >> 16:04X}_{code & 0xFFFF:04X}"
        )
    return result[:8]


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Keep a small allowlist of status fields; never return the raw MQTT payload."""
    state = str(report.get("gcode_state") or "unknown").upper()
    progress = _number(report.get("mc_percent"))
    remaining = _number(report.get("mc_remaining_time"))
    if state == "FINISH":
        progress, remaining = 100, 0
    alerts = report.get("hms")
    try:
        home_flag = int(report.get("home_flag") or 0)
    except (TypeError, ValueError):
        home_flag = 0
    try:
        error_code = int(report.get("print_error") or 0)
    except (TypeError, ValueError):
        error_code = -1
    stopped_safe = (
        state == "FAILED" and str(report.get("stg_cur")) in {"0", "255"}
        and error_code in {0, 0x0500C011} and not alerts
    )
    return {
        "state": state,
        "progress_percent": round(max(0, min(100, progress)), 1) if progress is not None else None,
        "remaining_minutes": max(0, round(remaining)) if remaining is not None else None,
        "layer": _number(report.get("layer_num")),
        "total_layers": _number(report.get("total_layer_num")),
        "nozzle_c": _number(report.get("nozzle_temper")),
        "bed_c": _number(report.get("bed_temper")),
        "print_error": str(report.get("print_error")) if report.get("print_error") else None,
        "error_code_hex": f"0x{error_code:08X}" if error_code > 0 else None,
        "stopped_job_ready": stopped_safe,
        "alerts": len(alerts) if isinstance(alerts, list) else 0,
        "hms_codes": _hms_codes(alerts),
        "homed_axes": "".join(
            axis for bit, axis in enumerate("XYZ") if home_flag & (1 << bit)
        ),
        "job_name": str(report.get("subtask_name") or report.get("gcode_file") or "")[:160],
        "observed_at": datetime.now(UTC).isoformat(),
    }


class PrinterStatusService:
    cache_seconds = 8

    def __init__(self, settings: Settings, identity: IdentityService):
        self.settings = settings
        self.identity = identity
        self._lock = Lock()
        self._cached: dict[str, Any] | None = None
        self._checked = 0.0

    def _credentials(self) -> tuple[str, str, str]:
        source = self._env_path()
        values = _env_values(source)
        return (
            self.settings.bambu_host or values.get("BAMBU_HOST", ""),
            self.settings.bambu_serial or values.get("BAMBU_SERIAL", ""),
            self.settings.bambu_access_code.get_secret_value()
            if self.settings.bambu_access_code else values.get("BAMBU_ACCESS_CODE", ""),
        )

    def _env_path(self) -> Path:
        default = Path(__file__).resolve().parents[4] / "autoswap_rip" / ".env"
        return self.settings.bambu_env_file or default

    def _active_runner_status(self) -> dict[str, Any] | None:
        """Use the queue's telemetry instead of competing for the A1 MQTT session."""
        database = self._env_path().parent / "queue.sqlite3"
        if not database.is_file():
            return None
        try:
            with closing(sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro", uri=True, timeout=0.2,
            )) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT b.heartbeat,j.state,j.label,j.progress,j.layer," 
                    "j.total_layers,j.remaining_minutes,j.updated "
                    "FROM batches b LEFT JOIN jobs j ON j.batch_id=b.id AND "
                    "j.state IN ('starting','running','cancel_requested') "
                    "WHERE b.state='running' ORDER BY j.position LIMIT 1"
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        if row is None or row["heartbeat"] is None:
            return None
        heartbeat = float(row["heartbeat"])
        if time.time() - heartbeat > 30:
            return None
        job_state = row["state"]
        observed = float(row["updated"] or 0)
        if not observed or time.time() - observed > 30:
            return {
                "configured": True,
                "online": False,
                "model": "Bambu Lab A1",
                "state": "unknown",
                "progress_percent": row["progress"],
                "remaining_minutes": row["remaining_minutes"],
                "layer": row["layer"],
                "total_layers": row["total_layers"],
                "job_name": str(row["label"] or ""),
                "observed_at": (
                    datetime.fromtimestamp(observed, UTC).isoformat() if observed else None
                ),
                "message": "Printer telemetry is stale; reconnecting without replaying the print.",
                "source": "active_print_runner",
            }
        state = {
            "starting": "PREPARE",
            "running": "RUNNING",
            "cancel_requested": "PAUSE",
        }.get(job_state, "FINISH")
        return {
            "configured": True,
            "online": True,
            "model": "Bambu Lab A1",
            "state": state,
            "progress_percent": row["progress"],
            "remaining_minutes": row["remaining_minutes"],
            "layer": row["layer"],
            "total_layers": row["total_layers"],
            "nozzle_c": None,
            "bed_c": None,
            "print_error": None,
            "error_code_hex": None,
            "stopped_job_ready": False,
            "alerts": 0,
            "hms_codes": [],
            "homed_axes": "",
            "job_name": str(row["label"] or ""),
            "observed_at": datetime.fromtimestamp(observed, UTC).isoformat(),
            "source": "active_print_runner",
        }

    def status(self, actor: ActorContext) -> dict[str, Any]:
        member = self.identity.membership(actor.actor_id, actor.household_id)
        scopes = ROLE_SCOPES[member.role] & actor.scopes
        if "home:read" not in scopes:
            raise AuthorizationError("printer status access denied")
        target = self.settings.home_household_id or (
            DEV_HOUSEHOLD_ID if self.settings.environment == "development" else None
        )
        if target is None or actor.household_id != target:
            raise AuthorizationError("printer status access denied")
        host, serial, code = self._credentials()
        if not all((host, serial, code)):
            return {"configured": False, "online": False, "state": "unknown",
                    "message": "Bambu LAN credentials are not configured."}
        runner_status = self._active_runner_status()
        if runner_status is not None:
            return {"serial_suffix": serial[-4:], **runner_status}
        with self._lock:
            if self._cached is not None and monotonic() - self._checked < self.cache_seconds:
                return self._cached.copy()
            try:
                result = self._read(host, serial, code)
            except Exception:
                result = {"configured": True, "online": False, "state": "unknown",
                          "message": "Printer status is unavailable. Check its LAN connection."}
            self._cached, self._checked = result, monotonic()
            return result.copy()

    @staticmethod
    def _read(host: str, serial: str, code: str) -> dict[str, Any]:
        import paho.mqtt.client as mqtt
        from paho.mqtt.enums import CallbackAPIVersion

        received = Event()
        result: dict[str, Any] = {}
        client = mqtt.Client(CallbackAPIVersion.VERSION2,
                             client_id="simon-monitor-" + uuid4().hex[:12])
        client.username_pw_set("bblp", code)
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)

        def connected(connection: Any, _userdata: Any, _flags: Any,
                      reason: Any, _properties: Any) -> None:
            if reason.is_failure:
                received.set()
                return
            connection.subscribe(f"device/{serial}/report")
            connection.publish(
                f"device/{serial}/request",
                json.dumps({"pushing": {"sequence_id": uuid4().hex[:12], "command": "pushall"}}),
                qos=1,
            )

        def message(_connection: Any, _userdata: Any, packet: Any) -> None:
            try:
                body = json.loads(packet.payload)
            except (ValueError, UnicodeDecodeError, TypeError):
                return
            report = body.get("print")
            if (
                isinstance(report, dict)
                and report.get("command") == "push_status"
                and report.get("gcode_state")
            ):
                result.update(summarize_report(report))
                received.set()

        client.on_connect = connected
        client.on_message = message
        try:
            client.connect_async(host, 8883, keepalive=30)
            client.loop_start()
            received.wait(8)
        finally:
            client.disconnect()
            client.loop_stop()
        if not result:
            raise TimeoutError("printer did not report status")
        return {"configured": True, "online": True, "model": "Bambu Lab A1",
                "serial_suffix": serial[-4:], **result}
