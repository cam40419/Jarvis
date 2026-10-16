"""Synthetic recovery evidence; never use this fixture for a real installation."""

from simon.services.recovery import RecoveryEvidence, begin_recovery, complete_recovery


def evidence(backup_id="synthetic-backup"):
    return RecoveryEvidence(
        operator="synthetic-test-operator",
        backup_id=backup_id,
        database_verification="Synthetic database verification receipt.",
        files_verification="Synthetic matching files verification receipt.",
        keys_verification="Synthetic key scope verification receipt.",
        provider_reconciliation="Mock provider effect inventory fully reconciled.",
        liability_reconciliation="Mock known charges retained; no unresolved liabilities.",
        inflight_termination="All synthetic pre-restore workers terminated.",
        unresolved_effects=[],
        unresolved_liabilities=[],
    )


def activate(path, backup_id="synthetic-backup"):
    state = begin_recovery(
        path,
        operator="synthetic-test-operator",
        reason="Initialize isolated recovery test.",
        backup_id=backup_id,
        writers_stopped=True,
    )
    return complete_recovery(path, state.epoch, evidence(backup_id))
