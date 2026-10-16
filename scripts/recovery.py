"""Fence a supported installation restore and record its operator reconciliation.

The private SIMON_RECOVERY_STATE_FILE must be outside every restored backup root.
Stop all writers before begin; never restore this witness from a backup.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from simon.config import Settings
from simon.domain.errors import DomainError, ValidationError
from simon.services.recovery import (
    RecoveryEvidence,
    RecoveryGuard,
    begin_recovery,
    complete_recovery,
)


def require_reconciled_usage(settings: Settings) -> None:
    if settings.storage_backend != "postgres":
        return
    from psycopg import Error as PostgresError

    from simon.adapters.postgres import PostgresStore

    store = None
    try:
        store = PostgresStore(settings.database_url.get_secret_value())
        if store.recovery_usage_pending():
            raise ValueError("Reconcile all restored model reservations and unknown calls first.")
    except PostgresError:
        raise ValidationError("Could not verify the recovery usage ledger.") from None
    finally:
        if store is not None:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    begin = commands.add_parser("begin")
    begin.add_argument("--operator", required=True)
    begin.add_argument("--reason", required=True)
    begin.add_argument("--backup-id", required=True)
    begin.add_argument("--writers-stopped", required=True, action="store_true")
    complete = commands.add_parser("complete")
    complete.add_argument("--epoch", required=True, type=UUID)
    complete.add_argument("--evidence", required=True, type=Path)
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        settings = Settings()
        path = settings.recovery_state_file
        if path is None:
            parser.error("Configure an absolute SIMON_RECOVERY_STATE_FILE outside restored files.")
        if args.action == "begin":
            state = begin_recovery(
                path,
                operator=args.operator,
                reason=args.reason,
                backup_id=args.backup_id,
                writers_stopped=args.writers_stopped,
            )
            print(json.dumps({"epoch": str(state.epoch), "status": state.status}))
        elif args.action == "complete":
            with args.evidence.open("rb") as stream:
                raw = stream.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                parser.error("Recovery evidence exceeds 128 KiB.")
            # Exact completion replay must remain safe after newly authorized
            # work starts. Epoch/evidence equality is still checked under lock.
            if not RecoveryGuard(path).is_active():
                require_reconciled_usage(settings)
            state = complete_recovery(path, args.epoch, RecoveryEvidence.model_validate_json(raw))
            print(json.dumps({"epoch": str(state.epoch), "status": state.status}))
        else:
            print(json.dumps(RecoveryGuard(path).status()))
    except (DomainError, OSError, ValueError):
        # Validation exceptions can include supplied evidence or configuration.
        parser.exit(
            1,
            "Recovery operation failed; quarantine remains enforced. Check evidence, "
            "epoch, configured private path and filesystem permissions.\n",
        )


if __name__ == "__main__":
    main()
