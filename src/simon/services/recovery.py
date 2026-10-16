"""Installation fencing outside the restored database and managed file bundle.

Only the local operator CLI changes this witness. A supported restore starts by
rotating it while writers are stopped. It is deliberately never auto-initialized
by an application process: a missing witness must not make an old backup active.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from simon.domain.errors import RecoveryQuarantinedError, ValidationError
from simon.domain.models import utc_now

_MESSAGE = (
    "Core execution is quarantined for recovery. An operator must reconcile the "
    "restored state, complete recovery and restart obsolete server processes."
)
_LOCKS: dict[str, tuple[Any, Any]] = {}
_REGISTRY_LOCK = threading.Lock()


class RecoveryEvidence(BaseModel):
    """Trusted operator attestations with references to retained verification reports."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operator: str = Field(min_length=3, max_length=200)
    backup_id: str = Field(min_length=3, max_length=500)
    database_verification: str = Field(min_length=10, max_length=2000)
    files_verification: str = Field(min_length=10, max_length=2000)
    keys_verification: str = Field(min_length=10, max_length=2000)
    provider_reconciliation: str = Field(min_length=10, max_length=2000)
    liability_reconciliation: str = Field(min_length=10, max_length=2000)
    inflight_termination: str = Field(min_length=10, max_length=2000)
    unresolved_effects: list[str] = Field(max_length=1000)
    unresolved_liabilities: list[str] = Field(max_length=1000)


class RecoveryState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    epoch: UUID
    status: Literal["quarantined", "active"]
    operator: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=10, max_length=2000)
    backup_id: str = Field(min_length=3, max_length=500)
    started_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime | None = None
    evidence: RecoveryEvidence | None = None

    @field_validator("evidence")
    @classmethod
    def bounded_evidence(cls, value: RecoveryEvidence | None) -> RecoveryEvidence | None:
        if value is not None and len(value.model_dump_json().encode()) > 128 * 1024:
            raise ValueError("Recovery evidence is too large.")
        return value


def _read(path: Path) -> RecoveryState:
    # The witness directory is private operator-controlled state. Never follow a
    # configured redirect or accept a partial/corrupt replacement as active.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise OSError("Recovery witness must not use filesystem redirects.")
    with path.open("rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("Recovery witness is too large.")
    value = RecoveryState.model_validate_json(raw)
    if value.status == "active" and (
        value.evidence is None
        or value.completed_at is None
        or value.evidence.backup_id != value.backup_id
        or value.evidence.unresolved_effects
        or value.evidence.unresolved_liabilities
    ):
        raise ValueError("Active recovery witness has no complete reconciliation.")
    return value


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Reentrant thread/process lock; all application DB locks precede this lock."""
    name = os.path.normcase(str(path.absolute()))
    with _REGISTRY_LOCK:
        lock, local = _LOCKS.setdefault(name, (threading.RLock(), threading.local()))
    with lock:
        if getattr(local, "depth", 0):
            local.depth += 1
            try:
                yield
            finally:
                local.depth -= 1
            return
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + 5
            while True:
                try:
                    stream.seek(0)
                    if sys.platform == "win32":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RecoveryQuarantinedError(_MESSAGE) from None
                    time.sleep(0.02)
            local.depth = 1
            try:
                yield
            finally:
                local.depth = 0
                stream.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write(path: Path, value: RecoveryState) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            if sys.platform != "win32":
                os.fchmod(stream.fileno(), 0o600)
            stream.write((value.model_dump_json(indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            move.restype = wintypes.BOOL
            if not move(str(temporary), str(path), 0x1 | 0x8):
                raise OSError("Recovery witness replacement failed.")
        else:
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(path: Path, value: RecoveryState) -> None:
    # Keep every operator decision alongside the witness, outside backup rewind.
    _write(path.with_name(f"{path.name}.{value.epoch}.{value.status}.{uuid4().hex}.json"), value)
    _write(path, value)


def begin_recovery(
    path: Path, *, operator: str, reason: str, backup_id: str, writers_stopped: bool
) -> RecoveryState:
    if not writers_stopped:
        raise ValidationError("Stop all writers before beginning restoration.")
    path = path.absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValidationError("Recovery witness must not use filesystem redirects.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(path):
        # Repeated begin does not discard unresolved evidence or silently release
        # quarantine. It produces a fresh epoch; completion must name that epoch.
        value = RecoveryState(
            epoch=uuid4(),
            status="quarantined",
            operator=operator,
            reason=reason,
            backup_id=backup_id,
        )
        _publish(path, value)
        return value


def complete_recovery(path: Path, epoch: UUID, evidence: RecoveryEvidence) -> RecoveryState:
    path = path.absolute()
    with _locked(path):
        current = _read(path)
        if current.epoch != epoch or evidence.backup_id != current.backup_id:
            raise ValidationError("Recovery epoch or backup evidence does not match.")
        if evidence.unresolved_effects or evidence.unresolved_liabilities:
            raise ValidationError("Unresolved effects or liabilities keep recovery quarantined.")
        if current.status == "active":
            if current.evidence != evidence:
                raise ValidationError("Recovery was already completed with different evidence.")
            return current
        value = current.model_copy(
            update={
                "status": "active",
                "completed_at": utc_now(),
                "evidence": evidence,
            }
        )
        _publish(path, value)
        return value


class RecoveryGuard:
    def __init__(self, path: Path | None = None, *, managed: bool = True) -> None:
        self.path = path.absolute() if path is not None else None
        self.managed = managed
        self.epoch: str | None = "ephemeral" if not managed else None
        if managed and self.path is not None:
            with suppress(OSError, ValueError):
                self.epoch = str(_read(self.path).epoch)

    @classmethod
    def ephemeral(cls) -> RecoveryGuard:
        """Process-local fixtures have no persistent database to restore."""
        return cls(managed=False)

    def assert_active(self) -> str:
        if not self.managed:
            return "ephemeral"
        try:
            if self.path is None:
                raise ValueError("No witness configured")
            state = _read(self.path)
            if state.status != "active" or str(state.epoch) != self.epoch:
                raise ValueError("Recovery state changed")
            return str(state.epoch)
        except (OSError, ValueError):
            raise RecoveryQuarantinedError(_MESSAGE) from None

    def assert_epoch(self, epoch: str | None) -> str:
        active = self.assert_active()
        if epoch != active and (self.managed or epoch is not None):
            raise RecoveryQuarantinedError("This authority predates recovery; re-enroll it.")
        return active

    def matches_epoch(self, epoch: str | None) -> bool:
        """Accounting must distinguish restored reservations from definitely unsent ones."""
        if not self.managed:
            return epoch in {None, "ephemeral"}
        try:
            return self.path is not None and epoch == str(_read(self.path).epoch)
        except (OSError, ValueError):
            return False

    def is_active(self) -> bool:
        try:
            self.assert_active()
            return True
        except RecoveryQuarantinedError:
            return False

    def status(self) -> dict[str, Any]:
        # Public status excludes private operator evidence and filesystem paths.
        return {"managed": self.managed, "dispatch_allowed": self.is_active()}

    @contextmanager
    def accounting(self) -> Iterator[None]:
        """Serialize ledger writes with begin, without blocking reconciliation."""
        if not self.managed or self.path is None or not self.path.parent.exists():
            yield
            return
        with _locked(self.path):
            yield

    @contextmanager
    def operation(self) -> Iterator[str]:
        if not self.managed:
            yield self.assert_active()
            return
        if self.path is None:
            raise RecoveryQuarantinedError(_MESSAGE)
        with ExitStack() as stack:
            try:
                stack.enter_context(_locked(self.path))
            except OSError:
                raise RecoveryQuarantinedError(_MESSAGE) from None
            yield self.assert_active()
