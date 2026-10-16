# Storage and recovery

PostgreSQL stores identity, connections, chat records, native projects, memberships,
tasks, scoped agent records, intake context, source metadata, planning history,
project models/encrypted credentials, resource policies and model usage receipts.
Native execution grants, dependencies, runs/checkpoints, waits/signals, schedules,
reference receipts and hashed runner credentials also reside in PostgreSQL.
Generic account files and immutable intake originals use `SIMON_LOCAL_FILES_DIR`.
Use an absolute private data directory outside the source checkout for a persistent
installation. Preserve the encryption key with database backups so saved connections
and project model keys remain decryptable. There is no old-project migration or artifact backfill utility.

The backup libraries and `scripts/backup_bundle.py` operate on explicitly selected
sources and destinations. Stop all writers before creating a bundle; the
`--writers-stopped` argument records the operator's assertion, not a live filesystem
snapshot. A backup is not complete until copied file hashes and a disposable database
restore have been checked.

```powershell
.\venv\Scripts\python.exe scripts/backup_bundle.py create E:/SimonBackups/recovery --writers-stopped
.\venv\Scripts\python.exe scripts/backup_bundle.py verify E:/SimonBackups/recovery --database
```

The command targets the repository's Compose PostgreSQL service by default. Confirm
that target before use; it does not silently redirect to any configured external
database. Verify against a new disposable database, never restore over an active
installation during a verification check. Inspect the script's `--help` for the
supported database/destination options.

Bundles contain private data and are not encrypted by this command. Restrict local
access and encrypt independent copies. Explicit secret inclusion must also capture
any process-only or external secret-store values through the operator's recovery
procedure. Losing a required key can leave restored connections unusable.

When `SIMON_MODEL_CATALOG_FILE` is configured, bundles preserve that administrator catalog
as `configuration/model-catalog.json`. Catalog templates contain no provider keys;
project credentials are scoped ciphertext in the database. Originals under the managed files root's `.project-sources`
directory are included, even when a revision has been revoked in the application. A missing
configured catalog fails backup creation before dumping the database rather than silently
omitting model configuration. `--include-secrets` additionally includes `.env` and the
available integration encryption-key file. If `SIMON_GOOGLE_TOKEN_KEY` supplies the master
key through a process environment or external secret store, preserve that value separately.
It takes precedence over `SIMON_INTEGRATION_KEY_FILE`. Process-only or external credentials
used by independent services also require explicit preservation by the operator.

Restore with all writers stopped and stage into a fresh destination. Point
`SIMON_MODEL_CATALOG_FILE` at the restored catalog and `SIMON_LOCAL_FILES_DIR` at the
restored files, and restore the matching database and encryption master key before
accepting traffic. Native project models do not fall back to provider environment keys.
Follow the [model recovery contract](project-models.md#credential-storage-and-recovery).
A recovered planning attempt is not redispatched: expiry releases unsent reservations
and marks interrupted dispatches unknown, preserving monetary holds and call slots until
definitive settlement or evidenced reconciliation. Verify preserved model/key scope,
policies, usage history, source bytes and planning receipts in the disposable restoration.
Never treat a database-only restore as evidence that originals, master keys or model
configuration have also been recovered.

## Enforced native-core restoration quarantine

Persistent PostgreSQL installations require `SIMON_RECOVERY_STATE_FILE`, an absolute
private path outside **every restored root**. It must be shared by all API processes
serving that installation. The application never creates or initializes this witness.
A missing, corrupt or quarantined witness blocks native model qualification, intake,
artifact review, execution claims, schedules, reference effects and result publication.
Authenticated reads, cancellation, revocation and usage reconciliation remain available.
`GET /v2/recovery` reports whether native dispatch is permitted without exposing the
operator's private evidence. Process-local memory development remains ephemeral.

Apply migration `0038_recovery_epochs.sql` with the normal migration runner. It preserves
historical rows and marks their authority as predating managed recovery. Existing runners,
execution grants and schedules require explicit reissue after initial setup as well as
restoration. Model enrollment and accepted artifact bytes remain intact.

1. Stop API processes, runners and other writers. Configure the private witness path.
   **Before replacing any live database/files**, begin a new recovery generation:

   ```powershell
   .\venv\Scripts\python.exe scripts/recovery.py begin --operator "operator identity" --reason "Restore verified installation backup" --backup-id "verified bundle identifier" --writers-stopped
   ```

   Retain the printed epoch. This writes quarantine outside the database; restoring
   database rows cannot reset it. `--writers-stopped` is an operator assertion, not a
   process terminator. Repeated begin rotates the epoch again and invalidates earlier
   completion commands. Do not include the witness, its lock or its audit siblings in
   an installation backup or restore. Preserve them separately as current control state.

2. Restore and verify matching database, files, catalog and keys. Restart the server
   in quarantine to inspect state and reconcile. Confirm old processes and provider
   requests terminated; fence obsolete credentials. Old API processes cannot regain
   dispatch after completion because they retain their original generation.

3. Reconcile **all provider effects and costs since the backup**, including activity
   absent from the restored ledger. Retain supporting provider receipts and restore
   known charges to the accounting record; a zero price does not prove an effect ended.
   A restored expired reservation becomes unknown rather than being released as unsent.
   Its monetary hold and call slot remain until evidenced owner reconciliation. Old
   responses cannot overwrite restored state or silently settle/release those holds.
   The completion CLI refuses any remaining database reservations, dispatched calls or
   unknown calls, including zero-fee calls. Do not bypass this by editing statuses.

4. Write a private JSON evidence report with these required fields: `operator`,
   `backup_id`, `database_verification`, `files_verification`, `keys_verification`,
   `provider_reconciliation`, `liability_reconciliation`, `inflight_termination`,
   `unresolved_effects` and `unresolved_liabilities`. Verification/reconciliation fields
   contain references to actual retained reports (10–2,000 characters); the two unresolved
   fields are lists. They must both be empty before release. Evidence is a trusted operator
   attestation; the command cannot independently interrogate arbitrary providers or prove
   the truth of a supplied report. Initial setup uses the same procedure with evidence
   of a fresh installation and no previous effects.

   ```powershell
   .\venv\Scripts\python.exe scripts/recovery.py complete --epoch "epoch printed by begin" --evidence E:/SimonRecovery/reconciliation.json
   .\venv\Scripts\python.exe scripts/recovery.py status
   ```

   The epoch and backup identifier must match. Begin/completion decisions are retained
   alongside the witness. Unresolved or mismatched evidence cannot release quarantine.

5. Reissue reviewed execution grants and schedules, enroll fresh runners, and explicitly
   cancel obsolete queued work or start reviewed replacement work. Restored runs and
   checkpoints remain inspectable but do not automatically resume in a new generation.
   Historical approvals do not grant new execution authority.

The enforcement covers the native project core and its shared model ledger; legacy
standalone adapters have their own lifecycle and must remain stopped during restoration.
All service instances must use the same private witness and supported filesystem locking.
A privileged database rewind that skips `begin`, or rolling back the witness itself, is
outside this supported restore contract and cannot be inferred from database contents.
This implementation does not establish physical power-loss durability on every filesystem,
automatic provider reconciliation or measured production restoration acceptance. See the
[native execution recovery boundary](native-execution.md#storage-api-and-restoration).

### Verification recorded October 12, 2026

The focused recovery checks passed 38 distinct non-PostgreSQL cases across witness/CLI,
execution rewind, qualification/intake/review publication, authenticated API status and
ledger contracts. The direct rewind regression restores all in-memory database state
while retaining the external witness: the old runner remains fenced before and after
completion and the mocked provider call count remains one. Existing execution service/
domain/memory contracts passed 100 cases, and execution HTTP tests passed 32.

The selected PostgreSQL 16.15/pgvector 0.8.6 program passed 42 cases covering migration,
legacy snapshot backfill, immutable epochs, restart, ledger concurrency, execution and
storage. One existing assertion was updated to include the newly bound recovery epoch;
its rerun passed. Disposable clusters stopped. Python lint, formatting and type checks
passed. These synthetic checks do not establish PostgreSQL 17 acceptance, live provider
quality, a full current coverage result or CORE-ACCEPT.

Host Desktop/Documents/Downloads folders and cloud files are outside managed account
storage and need their own backups. Standalone runtime adapter experiments own their
own lease/artifact directories; the application does not discover or back them up as
a retired agent state root. Validate a recovery into a fresh destination before
choosing a deployment cutover. Source removal in this change does not restart services,
delete external files or change a deployed database.
