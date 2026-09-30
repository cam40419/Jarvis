"""Supervised A1 print batches backed by the local AutoSwap queue."""

import hashlib
import html
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from simon.config import Settings
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.models import ActorContext
from simon.services.identity import ROLE_SCOPES, IdentityService
from simon.services.printer_status import PrinterStatusService


class BatchStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator_present: bool
    plate_checked: bool = False
    auto_continue: bool = False


class BatchControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "resume", "cancel"]


class BatchResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["finished", "stopped"]
    inspected: bool


class BatchRepeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=1, ge=1, le=8, strict=True)


class SwapDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gcode: str = Field(min_length=5, max_length=10000)


class SwapPrintDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["active", "editor"] = "editor"
    gcode: str = Field(default="", max_length=10000)
    motion_profile: Literal["current", "slow"] = "current"


class SwapStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator_present: bool


class SwapResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["verified", "failed"]
    inspected: bool


class HomeStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator_present: bool


class HomeResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["verified", "failed"]
    inspected: bool


class DirectPrinterCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["home", "swap", "home_swap"]


def swap_command_sha256(path: Path) -> str:
    # Hash the same canonical commands as AutoSwap; annotations are not motion.
    commands = [" ".join(line.split(";", 1)[0].split()).upper()
                for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return hashlib.sha256(("\n".join(line for line in commands if line) + "\n").encode()).hexdigest()


class PrintBatchService:
    def __init__(
        self, settings: Settings, identity: IdentityService,
        printer: PrinterStatusService,
        root: Path | None = None,
    ) -> None:
        self.settings, self.identity, self.printer = settings, identity, printer
        self.root = root or Path(__file__).resolve().parents[4] / "autoswap_rip"
        self.db = self.root / "queue.sqlite3"

    def authorize(self, actor: ActorContext, *, write: bool = False) -> None:
        member = self.identity.membership(actor.actor_id, actor.household_id)
        scopes = ROLE_SCOPES[member.role] & actor.scopes
        target = self.settings.home_household_id
        if (
            target is None or target != actor.household_id
            or "home:read" not in scopes
            or (write and "jobs:write" not in scopes)
        ):
            raise AuthorizationError("printer batch access denied")

    def direct_command(
        self, actor: ActorContext, body: DirectPrinterCommand
    ) -> dict[str, str]:
        self.authorize(actor, write=True)
        status = self.printer.status(actor)
        if (
            not status.get("online") or (
                status.get("state") not in {"IDLE", "FINISH"}
                and not status.get("stopped_job_ready")
            )
            or status.get("alerts")
        ):
            raise ValidationError("printer must be idle and alert-free")
        if body.action == "swap" and status.get("homed_axes") != "XYZ":
            raise ValidationError("home X, Y, and Z before swapping plates")
        try:
            result = subprocess.run(
                [sys.executable, str(self.root / "simon_bridge.py"),
                 f"direct-{body.action}"],
                cwd=self.root, capture_output=True, text=True,
                timeout=150 if body.action == "home_swap" else 45, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ValidationError(
                "printer command timed out; inspect the printer before sending another"
            ) from None
        if result.returncode:
            try:
                message = json.loads(result.stdout)["error"]
            except (ValueError, KeyError):
                message = "printer command failed; inspect the printer before retrying"
            raise ValidationError(html.unescape(str(message)).strip()[:300])
        return cast(dict[str, str], json.loads(result.stdout))

    def connection(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only and not self.db.exists():
            raise FileNotFoundError(self.db)
        connection = sqlite3.connect(
            f"file:{self.db.as_posix()}?mode=ro" if read_only else self.db,
            uri=read_only, timeout=10,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def batches(self, actor: ActorContext) -> list[dict[str, Any]]:
        self.authorize(actor)
        if not self.db.exists():
            return []
        with closing(self.connection(read_only=True)) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'"
            ).fetchone():
                return []
            records = connection.execute(
                "SELECT * FROM batches WHERE household_id=? AND actor_id=? "
                "ORDER BY created DESC LIMIT 30",
                (str(actor.household_id), str(actor.actor_id)),
            ).fetchall()
            result = []
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            kind = "kind" if "kind" in columns else "'print' AS kind"
            for record in records:
                jobs = connection.execute(
                    "SELECT id,label,plate,use_ams,ams_slot,backup_ams_slot,position,swap_enabled,"
                    "state,note,progress,"
                    "layer,total_layers,remaining_minutes,updated,sha256,"
                    + kind + " FROM jobs "
                    "WHERE batch_id=? ORDER BY position", (record["id"],)
                ).fetchall()
                result.append({
                    "id": record["id"], "state": record["state"], "note": record["note"],
                    "created": record["created"], "sequence_sha256": record["sequence_sha256"],
                    "auto_continue": bool(dict(record).get("auto_continue", False)),
                    "runner_stale": record["state"] == "running"
                    and time.time() - (record["heartbeat"] or record["created"]) > 300,
                    "jobs": [dict(job) for job in jobs],
                })
            return result

    def delete_batch(self, actor: ActorContext, batch_id: str) -> dict[str, Any]:
        self.authorize(actor, write=True)
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self.owned(connection, actor, batch_id)
            if record["state"] not in {"staged", "done", "canceled"}:
                raise ValidationError("stop or resolve this batch before deleting it")
            jobs = connection.execute(
                "SELECT id,prepared,state FROM jobs WHERE batch_id=?", (batch_id,)
            ).fetchall()
            if any(job["state"] in {"starting", "running", "cancel_requested", "uncertain"}
                   for job in jobs):
                raise ValidationError("resolve the print jobs before deleting this batch")
            spool = (self.root / "spool").resolve()
            prepared = [Path(job["prepared"]).resolve() for job in jobs]
            if any(path.parent != spool or path.name != f"{job['id']}.gcode.3mf"
                   for path, job in zip(prepared, jobs)):
                raise ValidationError("batch file path is invalid")
            uploads_parent = (spool / "uploads").resolve()
            uploads = (uploads_parent / batch_id).resolve()
            if uploads.parent != uploads_parent:
                raise ValidationError("batch upload path is invalid")
            connection.execute("DELETE FROM jobs WHERE batch_id=?", (batch_id,))
            connection.execute("DELETE FROM batches WHERE id=?", (batch_id,))
        for path in prepared:
            path.unlink(missing_ok=True)
        if uploads.is_dir():
            shutil.rmtree(uploads)
        return {"id": batch_id, "deleted": True}

    def owned(
        self, connection: sqlite3.Connection, actor: ActorContext, batch_id: str
    ) -> sqlite3.Row:
        record = connection.execute(
            "SELECT * FROM batches WHERE id=? AND household_id=? AND actor_id=?",
            (batch_id, str(actor.household_id), str(actor.actor_id)),
        ).fetchone()
        if record is None:
            raise NotFoundError("print batch not found")
        return cast(sqlite3.Row, record)

    def stage(self, actor: ActorContext, files: list[UploadFile], metadata: str) -> dict[str, Any]:
        self.authorize(actor, write=True)
        if not 1 <= len(files) <= 8:
            raise ValidationError("choose one to eight sliced print files")
        if not (self.root / "simon_bridge.py").is_file():
            raise ValidationError("AutoSwap bridge is unavailable on this server")
        try:
            choices = json.loads(metadata)
            if not isinstance(choices, list) or not 1 <= len(choices) <= 8:
                raise ValueError
            normalized = []
            for index, choice in enumerate(choices):
                if (
                    not isinstance(choice, dict)
                    or type(choice.get("use_ams")) is not bool
                ):
                    raise ValueError
                file_index = choice.get("file_index", index)
                if type(file_index) is not int or not 0 <= file_index < len(files):
                    raise ValueError
                ams_slot = choice.get("ams_slot")
                backup_ams_slot = choice.get("backup_ams_slot")
                if choice["use_ams"]:
                    if type(ams_slot) is not int or not 1 <= ams_slot <= 4:
                        raise ValueError
                    if (
                        backup_ams_slot is not None
                        and (
                            type(backup_ams_slot) is not int
                            or not 1 <= backup_ams_slot <= 4
                            or backup_ams_slot == ams_slot
                        )
                    ):
                        raise ValueError
                elif ams_slot is not None or backup_ams_slot is not None:
                    raise ValueError
                normalized.append({
                    "file_index": file_index, "use_ams": choice["use_ams"],
                    "ams_slot": ams_slot, "backup_ams_slot": backup_ams_slot,
                })
        except (ValueError, TypeError):
            raise ValidationError("provide one to eight valid print steps") from None
        batch_id = uuid4().hex
        folder = self.root / "spool" / "uploads" / batch_id
        folder.mkdir(parents=True, exist_ok=False)
        uploads = []
        try:
            for index, upload in enumerate(files):
                name = Path(upload.filename or "").name
                if not name.lower().endswith(".3mf") or len(name) > 160:
                    raise ValidationError("choose sliced .3mf files")
                path = folder / f"{index + 1}.gcode.3mf"
                size = 0
                with path.open("xb") as output:
                    while chunk := upload.file.read(1024 * 1024):
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise ValidationError("each print file must be under 64 MB")
                        output.write(chunk)
                if not size:
                    raise ValidationError("print file is empty")
                uploads.append({"path": str(path), "name": name})
            entries = [{**uploads[choice["file_index"]], "use_ams": choice["use_ams"],
                        "ams_slot": choice["ams_slot"],
                        "backup_ams_slot": choice["backup_ams_slot"]}
                       for choice in normalized]
            return self.prepare_entries(actor, batch_id, folder, entries)
        except Exception:
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
            folder.rmdir()
            raise

    def prepare_entries(
        self, actor: ActorContext, batch_id: str, folder: Path, entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps({
            "batch_id": batch_id, "household_id": str(actor.household_id),
            "actor_id": str(actor.actor_id), "files": entries,
        }), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(self.root / "simon_bridge.py"), "stage", str(manifest)],
            cwd=self.root, capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode:
            try:
                message = json.loads(result.stdout)["error"]
            except (ValueError, KeyError):
                message = (
                    result.stderr.strip().splitlines()[-1] if result.stderr.strip()
                    else "Print preparation failed. Check the sliced files."
                )
            raise ValidationError(str(message)[:300])
        return cast(dict[str, Any], json.loads(result.stdout))

    def repeat(self, actor: ActorContext, batch_id: str, body: BatchRepeat) -> dict[str, Any]:
        self.authorize(actor, write=True)
        new_id = uuid4().hex
        folder = self.root / "spool" / "uploads" / new_id
        created_folder = False
        try:
            with closing(self.connection()) as connection, connection:
                # Copy retained sliced files while deletion is excluded. Release
                # the transaction before the preparation subprocess writes new jobs.
                connection.execute("BEGIN IMMEDIATE")
                record = self.owned(connection, actor, batch_id)
                if record["state"] not in {"done", "canceled"}:
                    raise ValidationError("finish or cancel the batch before repeating its sequence")
                jobs = connection.execute(
                    "SELECT * FROM jobs "
                    "WHERE batch_id=? ORDER BY position,id", (batch_id,),
                ).fetchall()
                if any(dict(job).get("kind") == "swap_test" for job in jobs):
                    raise ValidationError("prepare another print-mode test from the swap editor")
                if not jobs or len(jobs) * body.count > 8:
                    raise ValidationError("a repeated sequence can contain at most eight prints")
                if any(job["state"] in {"starting", "running", "cancel_requested", "uncertain"}
                       for job in jobs):
                    raise ValidationError("resolve the print jobs before repeating this batch")
                upload_root = (self.root / "spool" / "uploads").resolve()
                source_folder = (upload_root / batch_id).resolve()
                if source_folder.parent != upload_root:
                    raise ValidationError("batch upload path is invalid")
                sources = [Path(job["source"]).resolve() for job in jobs]
                if any(path.parent != source_folder or not path.is_file() for path in sources):
                    raise ValidationError("saved sliced files are unavailable; upload them to build a new sequence")
                folder.mkdir(parents=True, exist_ok=False)
                created_folder = True
                copies: dict[Path, Path] = {}
                entries = []
                for job, source in zip(jobs, sources):
                    if source not in copies:
                        destination = folder / f"{len(copies) + 1}.gcode.3mf"
                        shutil.copyfile(source, destination)
                        copies[source] = destination
                    entries.append({
                        "path": str(copies[source]), "name": job["label"],
                        "use_ams": bool(job["use_ams"]), "ams_slot": job["ams_slot"],
                        "backup_ams_slot": job["backup_ams_slot"],
                    })
            # Prepare from original sliced files so every inter-print swap,
            # including the boundary between repetitions, is inserted once.
            return self.prepare_entries(actor, new_id, folder, entries * body.count)
        except Exception:
            if created_folder and folder.is_dir():
                for path in folder.iterdir():
                    if path.is_file():
                        path.unlink(missing_ok=True)
                folder.rmdir()
            raise

    def prepare_swap_print(self, actor: ActorContext, body: SwapPrintDraft) -> dict[str, Any]:
        self.authorize(actor, write=True)
        if "\x00" in body.gcode:
            raise ValidationError("swap sequence contains an invalid character")
        if body.source == "editor" and len(body.gcode.strip()) < 5:
            raise ValidationError("enter the swap commands or choose the active sequence")
        batch_id = uuid4().hex
        folder = self.root / "spool" / "uploads" / batch_id
        folder.mkdir(parents=True, exist_ok=False)
        try:
            (folder / "swap.gcode").write_text(body.gcode, encoding="utf-8", newline="\n")
            manifest = folder / "manifest.json"
            manifest.write_text(json.dumps({
                "batch_id": batch_id, "household_id": str(actor.household_id),
                "actor_id": str(actor.actor_id), "motion_profile": body.motion_profile,
                "source": body.source,
            }), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(self.root / "simon_bridge.py"),
                 "stage-swap-print", str(manifest)],
                cwd=self.root, capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode:
                try:
                    message = json.loads(result.stdout)["error"]
                except (ValueError, KeyError):
                    message = "The print-mode test could not be prepared."
                raise ValidationError(str(message)[:300])
            return cast(dict[str, Any], json.loads(result.stdout))
        except Exception:
            for path in folder.iterdir():
                path.unlink(missing_ok=True)
            folder.rmdir()
            raise

    def sequence(self, actor: ActorContext) -> dict[str, str]:
        self.authorize(actor)
        path = self.root / "eject_sequence.gcode"
        active_id = ""
        if self.db.exists():
            with closing(self.connection(read_only=True)) as connection:
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control'"
                ).fetchone():
                    row = connection.execute(
                        "SELECT value FROM control WHERE key='active_swap_trial'"
                    ).fetchone()
                    if row:
                        active_id = row["value"]
                        trial = connection.execute(
                            "SELECT path,state FROM swap_trials WHERE id=?", (active_id,)
                        ).fetchone()
                        if trial and trial["state"] in {"verified", "configured"}:
                            path = Path(trial["path"])
        return {"name": path.name, "gcode": path.read_text(encoding="utf-8")[:10000],
                "active_trial_id": active_id, "sequence_sha256": swap_command_sha256(path)}

    def swap_trials(self, actor: ActorContext) -> list[dict[str, Any]]:
        self.authorize(actor)
        if not self.db.exists():
            return []
        with closing(self.connection(read_only=True)) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='swap_trials'"
            ).fetchone():
                return []
            active = connection.execute(
                "SELECT value FROM control WHERE key='active_swap_trial'"
            ).fetchone()
            rows = connection.execute(
                "SELECT id,sha256,state,note,created,updated FROM swap_trials "
                "WHERE household_id=? AND actor_id=? ORDER BY "
                "CASE WHEN state IN ('running','accepted_unverified','unknown') "
                "THEN 0 ELSE 1 END, created DESC LIMIT 20",
                (str(actor.household_id), str(actor.actor_id)),
            ).fetchall()
            return [{**dict(row), "active": bool(active and active["value"] == row["id"])}
                    for row in rows]

    def delete_swap_trial(self, actor: ActorContext, trial_id: str) -> dict[str, Any]:
        self.authorize(actor, write=True)
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.trial(connection, actor, trial_id)
            if row["state"] not in {"validated", "verified", "configured", "failed"}:
                raise ValidationError("resolve the swap test before deleting it")
            path = Path(row["path"]).resolve()
            parent = (self.root / "spool" / "swap_trials").resolve()
            if path.parent != parent or path.name != f"{trial_id}.gcode":
                raise ValidationError("swap test file path is invalid")
            connection.execute("UPDATE control SET value='' WHERE key='active_swap_trial' AND value=?",
                               (trial_id,))
            connection.execute("DELETE FROM swap_trials WHERE id=?", (trial_id,))
        path.unlink(missing_ok=True)
        return {"id": trial_id, "deleted": True}

    @staticmethod
    def homing_pending(connection: sqlite3.Connection) -> bool:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='homing_attempts'"
        ).fetchone():
            return False
        return bool(connection.execute(
            "SELECT 1 FROM homing_attempts WHERE state IN "
            "('running','accepted_unverified','unknown') LIMIT 1"
        ).fetchone())

    def homing_attempts(self, actor: ActorContext) -> list[dict[str, Any]]:
        self.authorize(actor)
        if not self.db.exists():
            return []
        with closing(self.connection(read_only=True)) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='homing_attempts'"
            ).fetchone():
                return []
            rows = connection.execute(
                "SELECT id,state,note,created,updated FROM homing_attempts "
                "WHERE household_id=? AND actor_id=? ORDER BY created DESC LIMIT 10",
                (str(actor.household_id), str(actor.actor_id)),
            ).fetchall()
            return [dict(row) for row in rows]

    def home_axes(self, actor: ActorContext, body: HomeStart) -> dict[str, str]:
        self.authorize(actor, write=True)
        if not body.operator_present:
            raise ValidationError("confirm that you will supervise homing motion")
        status = self.printer.status(actor)
        if (
            not status.get("online") or (
                status.get("state") not in {"IDLE", "FINISH"}
                and not status.get("stopped_job_ready")
            )
            or status.get("alerts")
        ):
            raise ValidationError("printer must be idle and alert-free before homing")
        result = subprocess.run(
            [sys.executable, str(self.root / "simon_bridge.py"), "init-db"],
            cwd=self.root, capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode:
            raise ValidationError("AutoSwap database is unavailable")
        attempt_id = uuid4().hex
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM batches WHERE state IN "
                "('running','waiting_for_plate','needs_attention') LIMIT 1"
            ).fetchone() or connection.execute(
                "SELECT 1 FROM jobs WHERE state IN "
                "('starting','running','cancel_requested','uncertain') LIMIT 1"
            ).fetchone() or connection.execute(
                "SELECT 1 FROM swap_trials WHERE state IN "
                "('running','accepted_unverified','unknown') LIMIT 1"
            ).fetchone() or self.homing_pending(connection):
                raise ValidationError("resolve current printer activity before homing")
            connection.execute(
                "INSERT INTO homing_attempts(id,household_id,actor_id,state,created,updated) "
                "VALUES(?,?,?,'running',?,?)",
                (attempt_id, str(actor.household_id), str(actor.actor_id),
                 time.time(), time.time()),
            )
        try:
            self.launch_home(attempt_id)
        except OSError:
            with closing(self.connection()) as connection, connection:
                connection.execute(
                    "UPDATE homing_attempts SET state='unknown',"
                    "note='runner did not start',updated=? WHERE id=?",
                    (time.time(), attempt_id),
                )
            raise ValidationError("the homing runner could not start") from None
        return {"id": attempt_id, "state": "running"}

    def launch_home(self, attempt_id: str) -> None:
        log_dir = Path(__file__).resolve().parents[3] / ".local" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "autoswap-batches.log").open("ab") as log:
            subprocess.Popen(
                [sys.executable, str(self.root / "simon_bridge.py"), "run-home", attempt_id],
                cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

    def resolve_homing(
        self, actor: ActorContext, attempt_id: str, body: HomeResolve
    ) -> dict[str, str]:
        self.authorize(actor, write=True)
        if not body.inspected:
            raise ValidationError("inspect the printer before recording the homing result")
        if body.outcome == "verified":
            status = self.printer.status(actor)
            if (
                not status.get("online") or status.get("state") not in {"IDLE", "FINISH"}
                or status.get("homed_axes") != "XYZ" or status.get("alerts")
            ):
                raise ValidationError("printer must report idle and XYZ homed to verify homing")
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM homing_attempts WHERE id=? AND household_id=? AND actor_id=?",
                (attempt_id, str(actor.household_id), str(actor.actor_id)),
            ).fetchone()
            if row is None:
                raise NotFoundError("homing attempt not found")
            if row["state"] not in {"accepted_unverified", "unknown"}:
                raise ValidationError("homing attempt is not awaiting inspection")
            connection.execute(
                "UPDATE homing_attempts SET state=?,note='operator inspected homing',"
                "updated=? WHERE id=?", (body.outcome, time.time(), attempt_id),
            )
        return {"id": attempt_id, "state": body.outcome}

    def trial(
        self, connection: sqlite3.Connection, actor: ActorContext, trial_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM swap_trials WHERE id=? AND household_id=? AND actor_id=?",
            (trial_id, str(actor.household_id), str(actor.actor_id)),
        ).fetchone()
        if row is None:
            raise NotFoundError("swap trial not found")
        return cast(sqlite3.Row, row)

    def validate_swap(self, actor: ActorContext, body: SwapDraft) -> dict[str, Any]:
        self.authorize(actor, write=True)
        if "\x00" in body.gcode:
            raise ValidationError("swap sequence contains an invalid character")
        trial_id = uuid4().hex
        folder = self.root / "spool" / "swap_trials"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{trial_id}.gcode"
        path.write_text(body.gcode, encoding="utf-8", newline="\n")
        try:
            result = subprocess.run(
                [sys.executable, str(self.root / "simon_bridge.py"), "validate-swap",
                 trial_id, str(path), str(actor.household_id), str(actor.actor_id)],
                cwd=self.root, capture_output=True, text=True, timeout=20, check=False,
            )
            if result.returncode:
                try:
                    message = json.loads(result.stdout)["error"]
                except (ValueError, KeyError):
                    message = "The swap sequence could not be validated."
                raise ValidationError(str(message)[:300])
            return cast(dict[str, Any], json.loads(result.stdout))
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def start_swap(
        self, actor: ActorContext, trial_id: str, body: SwapStart
    ) -> dict[str, str]:
        self.authorize(actor, write=True)
        if not body.operator_present:
            raise ValidationError("confirm that you will supervise this swap motion")
        status = self.printer.status(actor)
        if (
            not status.get("online") or (
                status.get("state") not in {"IDLE", "FINISH"}
                and not status.get("stopped_job_ready")
            )
            or status.get("alerts") or status.get("homed_axes") != "XYZ"
        ):
            raise ValidationError("printer must be idle, alert-free, and homed on X, Y, and Z")
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.trial(connection, actor, trial_id)
            if row["state"] != "validated":
                raise ValidationError("this swap trial has already been used")
            if connection.execute(
                "SELECT 1 FROM batches WHERE state IN "
                "('running','waiting_for_plate','needs_attention') LIMIT 1"
            ).fetchone() or connection.execute(
                "SELECT 1 FROM jobs WHERE state IN "
                "('starting','running','cancel_requested','uncertain') LIMIT 1"
            ).fetchone():
                raise ValidationError("resolve the current print batch before running a swap")
            pending = connection.execute(
                "SELECT id,state FROM swap_trials WHERE state IN "
                "('running','accepted_unverified','unknown') "
                "ORDER BY created DESC LIMIT 1"
            ).fetchone()
            if pending:
                raise ValidationError(
                    f"swap trial {pending['id'][:8]} is {pending['state']}; "
                    "inspect the printer and record its outcome below before running another"
                )
            if self.homing_pending(connection):
                raise ValidationError("inspect and resolve the current homing attempt first")
            connection.execute(
                "UPDATE swap_trials SET state='running',updated=? WHERE id=?",
                (time.time(), trial_id),
            )
        log_dir = Path(__file__).resolve().parents[3] / ".local" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (log_dir / "autoswap-batches.log").open("ab") as log:
                subprocess.Popen(
                    [sys.executable, str(self.root / "simon_bridge.py"), "run-swap", trial_id],
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
        except OSError:
            with closing(self.connection()) as connection, connection:
                connection.execute(
                    "UPDATE swap_trials SET state='unknown',note='runner did not start' "
                    "WHERE id=?", (trial_id,),
                )
            raise ValidationError("the swap runner could not start") from None
        return {"id": trial_id, "state": "running"}

    def resolve_swap(
        self, actor: ActorContext, trial_id: str, body: SwapResolve
    ) -> dict[str, str]:
        self.authorize(actor, write=True)
        if not body.inspected:
            raise ValidationError("inspect the actual plate motion before recording its result")
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.trial(connection, actor, trial_id)
            if row["state"] not in {"accepted_unverified", "unknown"}:
                raise ValidationError("swap trial is not awaiting inspection")
            connection.execute(
                "UPDATE swap_trials SET state=?,note='operator inspected motion',updated=? "
                "WHERE id=?", (body.outcome, time.time(), trial_id),
            )
        return {"id": trial_id, "state": body.outcome}

    def activate_swap(self, actor: ActorContext, trial_id: str) -> dict[str, str]:
        self.authorize(actor, write=True)
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self.trial(connection, actor, trial_id)
            if row["state"] not in {"verified", "configured"}:
                raise ValidationError("verify this swap motion before using it for print batches")
            path = Path(row["path"]).resolve()
            if not path.is_relative_to((self.root / "spool" / "swap_trials").resolve()):
                raise ValidationError("swap trial file is outside the trial directory")
            if not path.is_file() or swap_command_sha256(path) != row["sha256"]:
                raise ValidationError("validated swap file changed")
            connection.execute(
                "INSERT INTO control(key,value) VALUES('active_swap_trial',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (trial_id,),
            )
        return {"id": trial_id, "state": "active"}

    def prepared(self, actor: ActorContext, batch_id: str, job_id: str) -> Path:
        self.authorize(actor)
        with closing(self.connection(read_only=True)) as connection:
            self.owned(connection, actor, batch_id)
            record = connection.execute(
                "SELECT prepared FROM jobs WHERE batch_id=? AND id=?", (batch_id, job_id)
            ).fetchone()
            if record is None:
                raise NotFoundError("prepared print not found")
            path = Path(record["prepared"]).resolve()
            if not path.is_relative_to((self.root / "spool").resolve()) or not path.is_file():
                raise NotFoundError("prepared print not found")
            return path

    def start(self, actor: ActorContext, batch_id: str, body: BatchStart) -> dict[str, Any]:
        self.authorize(actor, write=True)
        if not body.operator_present:
            raise ValidationError("confirm that an operator is present for this print test")
        status = self.printer.status(actor)
        if (
            not status.get("online")
            or not (
                status.get("state") in {"IDLE", "FINISH"}
                or (status.get("state") == "FAILED" and status.get("stopped_job_ready") is True)
            )
            or status.get("alerts")
        ):
            state = str(status.get("state") or "unknown")
            error = f" (error {status['error_code_hex']})" if status.get("error_code_hex") else ""
            codes = ", ".join(status.get("hms_codes") or [])
            alerts = f", active alert {codes}" if codes else (
                f", {status['alerts']} active alert(s)" if status.get("alerts") else ""
            )
            raise ValidationError(
                f"printer is not ready: {state}{error}{alerts}; clear the printer error "
                "and confirm it is idle before starting"
            )
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self.owned(connection, actor, batch_id)
            if record["state"] not in {"staged", "waiting_for_plate"}:
                raise ValidationError("batch cannot start from its current state")
            first_job = connection.execute(
                "SELECT * FROM jobs WHERE batch_id=? AND state='queued' ORDER BY position LIMIT 1",
                (batch_id,),
            ).fetchone()
            if first_job and first_job["swap_enabled"] and dict(first_job).get("kind") != "swap_test":
                active = connection.execute(
                    "SELECT sha256 FROM swap_trials WHERE id=(SELECT value FROM control WHERE key='active_swap_trial')"
                ).fetchone()
                if active is None or active["sha256"] != record["sequence_sha256"]:
                    raise ValidationError("the active swap changed; prepare this sequence again so it matches the regular swap test")
            if not first_job:
                raise ValidationError("no queued prints remain in this batch")
            if body.auto_continue and dict(first_job).get("kind") == "swap_test":
                raise ValidationError("automatic continuation is only available for print batches")
            if (first_job and dict(first_job).get("kind") == "swap_test"
                    and status.get("homed_axes") != "XYZ"):
                raise ValidationError(
                    "home X, Y, and Z first; the print-mode test does not home automatically"
                )
            if record["state"] == "waiting_for_plate" and not body.plate_checked:
                raise ValidationError("verify that the new plate is seated before continuing")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE state IN "
                "('starting','running','cancel_requested') LIMIT 1"
            ).fetchone():
                raise ValidationError("another print has an unresolved active state")
            if connection.execute(
                "SELECT 1 FROM swap_trials WHERE state IN "
                "('running','accepted_unverified','unknown') LIMIT 1"
            ).fetchone():
                raise ValidationError("resolve the standalone swap before starting a print")
            if self.homing_pending(connection):
                raise ValidationError("inspect and resolve homing before starting a print")
            connection.execute(
                "UPDATE batches SET state='running',note='',heartbeat=?,auto_continue=? WHERE id=?",
                (time.time(), int(body.auto_continue), batch_id),
            )
            connection.execute("DELETE FROM control WHERE key='action'")
        log_dir = Path(__file__).resolve().parents[3] / ".local" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (log_dir / "autoswap-batches.log").open("ab") as log:
                subprocess.Popen(
                    [sys.executable, str(self.root / "simon_bridge.py"), "run", batch_id],
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
        except OSError:
            with closing(self.connection()) as connection, connection:
                connection.execute(
                    "UPDATE batches SET state='needs_attention',"
                    "note='runner did not start' WHERE id=?",
                    (batch_id,),
                )
            raise ValidationError("the print runner could not start") from None
        return {"id": batch_id, "state": "running"}

    def control(self, actor: ActorContext, batch_id: str, body: BatchControl) -> dict[str, Any]:
        self.authorize(actor, write=True)
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self.owned(connection, actor, batch_id)
            active = connection.execute(
                "SELECT id,state FROM jobs WHERE batch_id=? AND state IN "
                "('starting','running','cancel_requested') LIMIT 1", (batch_id,)
            ).fetchone()
            if body.action in {"pause", "resume"}:
                if record["state"] != "running" or not active or active["state"] != "running":
                    raise ValidationError("no running print in this batch")
                connection.execute(
                    "INSERT INTO control(key,value) VALUES('action',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.action,),
                )
            else:
                if record["state"] in {"done", "canceled"}:
                    raise ValidationError("batch has already ended")
                connection.execute(
                    "UPDATE jobs SET state='canceled' WHERE batch_id=? AND state='queued'",
                    (batch_id,),
                )
                if active and active["state"] != "cancel_requested":
                    connection.execute(
                        "UPDATE jobs SET state='cancel_requested' WHERE id=?", (active["id"],)
                    )
                connection.execute(
                    "UPDATE batches SET state=? WHERE id=?",
                    ("running" if active else "canceled", batch_id),
                )
        return {"id": batch_id, "action": body.action}

    def resolve(self, actor: ActorContext, batch_id: str, body: BatchResolve) -> dict[str, Any]:
        self.authorize(actor, write=True)
        if not body.inspected:
            raise ValidationError("inspect the printer and plate before resolving this batch")
        status = self.printer.status(actor)
        if status.get("online") and status.get("state") in {"PREPARE", "RUNNING", "PAUSE"}:
            raise ValidationError("printer still reports an active print")
        with closing(self.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self.owned(connection, actor, batch_id)
            if record["state"] != "needs_attention":
                raise ValidationError("only a batch needing attention can be resolved")
            active = connection.execute(
                "SELECT * FROM jobs WHERE batch_id=? AND state IN "
                "('starting','running','cancel_requested','uncertain') ORDER BY position LIMIT 1",
                (batch_id,),
            ).fetchone()
            if body.outcome == "finished":
                if not active:
                    raise ValidationError("no uncertain print to mark finished")
                connection.execute(
                    "UPDATE jobs SET state='done',note='resolved after operator inspection',"
                    "progress=100,remaining_minutes=0,layer=COALESCE(total_layers,layer),"
                    "finished=?,updated=? WHERE id=?", (time.time(), time.time(), active["id"]),
                )
                more = connection.execute(
                    "SELECT 1 FROM jobs WHERE batch_id=? AND state='queued' LIMIT 1",
                    (batch_id,),
                ).fetchone()
                next_state = "waiting_for_plate" if more and active["swap_enabled"] else "done"
            else:
                connection.execute(
                    "UPDATE jobs SET state='canceled',note='resolved after operator inspection' "
                    "WHERE batch_id=? AND state IN "
                    "('queued','starting','running','cancel_requested','uncertain')",
                    (batch_id,),
                )
                next_state = "canceled"
            connection.execute(
                "UPDATE batches SET state=?,note='resolved after operator inspection' WHERE id=?",
                (next_state, batch_id),
            )
        return {"id": batch_id, "state": next_state}


def print_batch_router(
    service: PrintBatchService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1/printers/a1", tags=["printer batches"])

    @router.post("/control")
    def direct_command(
        body: DirectPrinterCommand, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, str]:
        return service.direct_command(actor, body)

    @router.get("/batches")
    def batches(actor: Annotated[ActorContext, Depends(authenticate)]) -> list[dict[str, Any]]:
        return service.batches(actor)

    @router.delete("/batches/{batch_id}")
    def delete_batch(
        batch_id: str, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.delete_batch(actor, batch_id)

    @router.get("/swap-sequence")
    def sequence(actor: Annotated[ActorContext, Depends(authenticate)]) -> dict[str, str]:
        return service.sequence(actor)

    @router.get("/swap-trials")
    def swap_trials(actor: Annotated[ActorContext, Depends(authenticate)]) -> list[dict[str, Any]]:
        return service.swap_trials(actor)

    @router.delete("/swap-trials/{trial_id}")
    def delete_swap_trial(
        trial_id: str, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.delete_swap_trial(actor, trial_id)

    @router.get("/home-attempts")
    def home_attempts(
        actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> list[dict[str, Any]]:
        return service.homing_attempts(actor)

    @router.post("/home", status_code=202)
    def home_axes(
        body: HomeStart, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, str]:
        return service.home_axes(actor, body)

    @router.post("/home-attempts/{attempt_id}/resolve")
    def resolve_homing(
        attempt_id: str, body: HomeResolve,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, str]:
        return service.resolve_homing(actor, attempt_id, body)

    @router.post("/swap-trials", status_code=201)
    def validate_swap(
        body: SwapDraft, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.validate_swap(actor, body)

    @router.post("/swap-print-tests", status_code=201)
    def prepare_swap_print(
        body: SwapPrintDraft, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.prepare_swap_print(actor, body)

    @router.post("/swap-trials/{trial_id}/start")
    def start_swap(
        trial_id: str, body: SwapStart,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, str]:
        return service.start_swap(actor, trial_id, body)

    @router.post("/swap-trials/{trial_id}/resolve")
    def resolve_swap(
        trial_id: str, body: SwapResolve,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, str]:
        return service.resolve_swap(actor, trial_id, body)

    @router.post("/swap-trials/{trial_id}/activate")
    def activate_swap(
        trial_id: str, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, str]:
        return service.activate_swap(actor, trial_id)

    @router.get("/batches/{batch_id}/jobs/{job_id}/prepared")
    def prepared(
        batch_id: str, job_id: str, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> FileResponse:
        return FileResponse(
            service.prepared(actor, batch_id, job_id),
            media_type="application/octet-stream",
            filename=f"prepared-{job_id}.gcode.3mf",
        )

    @router.post("/batches", status_code=201)
    def stage(
        actor: Annotated[ActorContext, Depends(authenticate)],
        files: Annotated[list[UploadFile], File()],
        metadata: Annotated[str, Form()],
    ) -> dict[str, Any]:
        return service.stage(actor, files, metadata)

    @router.post("/batches/{batch_id}/start")
    def start(
        batch_id: str, body: BatchStart,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.start(actor, batch_id, body)

    @router.post("/batches/{batch_id}/repeat", status_code=201)
    def repeat_batch(
        batch_id: str, body: BatchRepeat,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.repeat(actor, batch_id, body)

    @router.post("/batches/{batch_id}/control")
    def control(
        batch_id: str, body: BatchControl,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.control(actor, batch_id, body)

    @router.post("/batches/{batch_id}/resolve")
    def resolve(
        batch_id: str, body: BatchResolve,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.resolve(actor, batch_id, body)

    return router
