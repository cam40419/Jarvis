# Storage and recovery

PostgreSQL stores identity, connections, chat records, native projects, memberships,
tasks, scoped agent records, intake context, source metadata, planning history,
project models/encrypted credentials, resource policies and model usage receipts.
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

Host Desktop/Documents/Downloads folders and cloud files are outside managed account
storage and need their own backups. Standalone runtime adapter experiments own their
own lease/artifact directories; the application does not discover or back them up as
a retired agent state root. Validate a recovery into a fresh destination before
choosing a deployment cutover. Source removal in this change does not restart services,
delete external files or change a deployed database.
