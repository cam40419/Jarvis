# Storage and recovery

PostgreSQL stores identity, connections, chat records, native projects, memberships,
tasks, scoped agent records, intake context, source metadata and planning history.
Generic account files and immutable intake originals use `SIMON_LOCAL_FILES_DIR`.
Use an absolute private data directory outside the source checkout for a persistent
installation. Preserve the encryption key with database backups so saved connections
remain decryptable. There is no old-project migration or artifact backfill utility.

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

When `SIMON_INTAKE_MODELS_FILE` is configured, bundles preserve that administrator catalog
as `configuration/intake-models.json`; its environment-variable references do not include
the actual provider keys. Originals under the managed files root's `.project-sources`
directory are included, even when a revision has been revoked in the application. A missing
configured catalog fails backup creation before dumping the database rather than silently
omitting model configuration. `--include-secrets` additionally includes `.env` and the
available integration encryption key; process-only or external provider credentials still
require explicit preservation by the operator.

Restore with all writers stopped and stage into a fresh destination. Point
`SIMON_INTAKE_MODELS_FILE` at the restored catalog and `SIMON_LOCAL_FILES_DIR` at the
restored files, and restore the matching database before accepting traffic. Re-establish
the catalog's named environment secrets separately. A recovered running planning attempt
is not redispatched: once its deadline expires, intake reads mark it unknown and preserve
any unresolved reservation. Never treat a database-only restore as evidence that original
files or model configuration have also been recovered.

Host Desktop/Documents/Downloads folders and cloud files are outside managed account
storage and need their own backups. Standalone runtime adapter experiments own their
own lease/artifact directories; the application does not discover or back them up as
a retired agent state root. Validate a recovery into a fresh destination before
choosing a deployment cutover. Source removal in this change does not restart services,
delete external files or change a deployed database.
