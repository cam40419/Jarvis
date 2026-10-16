"""Persistent restore fencing is independent of a rewound application database."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from simon.config import Settings
from simon.domain.errors import RecoveryQuarantinedError, ValidationError
from simon.services.recovery import RecoveryGuard, begin_recovery, complete_recovery
from tests.recovery_helpers import activate, evidence


def begin(path):
    return begin_recovery(
        path,
        operator="recovery-test",
        reason="Restore the earlier synthetic snapshot.",
        backup_id="synthetic-backup",
        writers_stopped=True,
    )


def test_missing_corrupt_and_partial_witness_fail_closed(tmp_path):
    path = tmp_path / "witness.json"
    assert not RecoveryGuard().is_active()
    assert not RecoveryGuard(path).is_active()
    path.write_text("{partial", encoding="utf-8")
    guard = RecoveryGuard(path)
    assert guard.status() == {"managed": True, "dispatch_allowed": False}
    with pytest.raises(RecoveryQuarantinedError), guard.operation():
        pytest.fail("A corrupt witness authorized dispatch")
    state = activate(path)
    assert not guard.is_active()  # A server started without authority needs restart.
    assert RecoveryGuard(path).assert_epoch(str(state.epoch)) == str(state.epoch)
    value = state.model_dump(mode="json")
    value["evidence"] = None
    path.write_text(json.dumps(value), encoding="utf-8")
    assert not RecoveryGuard(path).is_active()


def test_begin_fences_old_process_and_old_authority_after_completion(tmp_path):
    path = tmp_path / "witness.json"
    previous = activate(path)
    old = RecoveryGuard(path)
    current = begin(path)
    new = RecoveryGuard(path)
    assert not old.is_active() and not new.is_active()
    complete_recovery(path, current.epoch, evidence())
    assert not old.is_active()
    assert new.is_active()
    for epoch in (None, str(previous.epoch)):
        with pytest.raises(RecoveryQuarantinedError):
            new.assert_epoch(epoch)
    assert new.assert_epoch(str(current.epoch)) == str(current.epoch)
    assert len(list(tmp_path.glob("witness.json.*.json"))) == 4


def test_completion_requires_matching_evidenced_recovery_and_no_unknowns(tmp_path):
    path = tmp_path / "witness.json"
    current = begin(path)
    for epoch, report in (
        (uuid4(), evidence()),
        (current.epoch, evidence("different-backup")),
        (current.epoch, evidence().model_copy(update={"unresolved_effects": ["lost response"]})),
        (current.epoch, evidence().model_copy(update={"unresolved_liabilities": ["unknown cost"]})),
    ):
        with pytest.raises(ValidationError):
            complete_recovery(path, epoch, report)
        assert not RecoveryGuard(path).is_active()
    completed = complete_recovery(path, current.epoch, evidence())
    assert complete_recovery(path, current.epoch, evidence()) == completed
    with pytest.raises(ValidationError):
        complete_recovery(path, current.epoch, evidence().model_copy(update={"operator": "other"}))


def test_begin_requires_stopped_writers_assertion(tmp_path):
    with pytest.raises(ValidationError):
        begin_recovery(
            tmp_path / "witness.json",
            operator="test",
            reason="Restore test state.",
            backup_id="synthetic-backup",
            writers_stopped=False,
        )
    assert list(tmp_path.iterdir()) == []


def test_short_operations_serialize_with_begin_and_are_reentrant(tmp_path):
    path = tmp_path / "witness.json"
    original = activate(path)
    guard, other = RecoveryGuard(path), RecoveryGuard(path)
    entered, rotated = Event(), Event()

    def rotate():
        entered.set()
        state = begin(path)
        rotated.set()
        return state

    with ThreadPoolExecutor(max_workers=1) as pool:
        with guard.operation(), other.operation():
            pending = pool.submit(rotate)
            assert entered.wait(2)
            assert not rotated.wait(0.05)
            assert guard.assert_active() == str(original.epoch)
        assert pending.result(timeout=5).epoch != original.epoch
    assert not guard.is_active()


def test_accounting_can_reconcile_during_quarantine_but_cannot_match_old_epoch(tmp_path):
    path = tmp_path / "witness.json"
    previous = activate(path)
    guard = RecoveryGuard(path)
    current = begin(path)
    with guard.accounting():
        assert not guard.matches_epoch(str(previous.epoch))
        assert guard.matches_epoch(str(current.epoch))
        assert not guard.is_active()


def test_configuration_keeps_witness_outside_managed_restore_root(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        Settings(
            _env_file=None, local_files_dir=tmp_path, recovery_state_file=tmp_path / "witness.json"
        )
    with pytest.raises(ValueError, match="absolute"):
        Settings(_env_file=None, recovery_state_file=Path("relative-witness.json"))


def test_operator_cli_begin_status_and_complete_are_explicit(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "recovery.py"
    path = tmp_path / "private" / "witness.json"
    environ = {
        **os.environ,
        "SIMON_RECOVERY_STATE_FILE": str(path),
        "SIMON_LOCAL_FILES_DIR": str(tmp_path / "files"),
    }

    def cli(*args):
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=tmp_path,
            env=environ,
            capture_output=True,
            text=True,
            timeout=20,
        )

    missing = cli("status")
    assert missing.returncode == 0
    assert json.loads(missing.stdout)["dispatch_allowed"] is False
    started = cli(
        "begin",
        "--operator",
        "synthetic-test",
        "--reason",
        "Synthetic restore drill.",
        "--backup-id",
        "synthetic-backup",
        "--writers-stopped",
    )
    assert started.returncode == 0, started.stderr
    state = json.loads(started.stdout)
    report = tmp_path / "report.json"
    report.write_text(evidence().model_dump_json(), encoding="utf-8")
    done = cli("complete", "--epoch", state["epoch"], "--evidence", str(report))
    assert done.returncode == 0, done.stderr
    assert json.loads(cli("status").stdout)["dispatch_allowed"] is True


def test_operator_database_errors_are_redacted(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from scripts.recovery import require_reconciled_usage

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("synthetic-private-connection-detail")

    monkeypatch.setattr("simon.adapters.postgres.PostgresStore", unavailable)
    with pytest.raises(ValidationError, match="verify the recovery usage ledger") as caught:
        require_reconciled_usage(Settings(_env_file=None, storage_backend="postgres"))
    assert "synthetic-private" not in str(caught.value)
