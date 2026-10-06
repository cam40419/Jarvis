# Local storage and recovery

Simon keeps managed project files and generated artifacts on the server. Remote
clients use the authenticated file APIs; remote access does not require cloud
storage. Configured Drive synchronization copies accepted deliverable files after local
saving, using the authored basename without generated ID prefixes. Controller answers,
legacy task summaries and journal responses remain in Simon's history. An explicitly named
project copy can also sync when its current bytes still match the saved source revision;
generic answer-copy names do not qualify. Editable local-file changes are not mirrored;
Drive-side edits are preserved. Drafts, evidence and planning records remain local.

For installations that previously copied controller answers to Drive, the one-time
`scripts/preview_drive_output_cleanup.py` audit matches exact original upload receipts,
folder/account bindings, names, content hashes and current revisions. Its corresponding
`scripts/apply_drive_output_cleanup.py` defaults to preview-only; execution requires the
reviewed manifest's SHA-256. It rechecks every target, renames verified real deliverables
and moves verified answer-only copies to reversible Drive trash. Local history remains
available, edited or moved files are skipped, and original upload receipts prevent
duplicate copies. Restart services with the corrected sync rules before applying cleanup.

## Permanent server paths

Use absolute paths outside the source checkout, consistently in the API,
assistant worker and agent dispatcher environments. Example Windows deployment:

```dotenv
SIMON_STORAGE_BACKEND=postgres
SIMON_LOCAL_FILES_DIR=D:/SimonData/files
SIMON_AGENT_STATE_DIR=D:/SimonData/agents
SIMON_AGENT_MANIFEST_FILE=D:/SimonConfig/agent-platform.json
```

Changing these settings alone does not migrate old files. Use the
[verified storage migration helper](../storage-migration.md) with all writers stopped,
or combine it with the maintenance wrapper:

```powershell
.\scripts\backup-full.ps1 -Force -MigrateStorageRoot "$env:LOCALAPPDATA\Simon\data"
```

It copies both roots, compares hashes, retains originals and a private configuration
backup, and atomically changes only the two storage settings. Keep the originals:
historical environment leases can still reference old absolute paths. The account/project
identifiers inside the roots remain unchanged. Keep the lease SQLite journal on the
execution host's local disk.

The current Windows installation completed this migration on October 2, 2026. Managed
files and agent state use `%LOCALAPPDATA%/Simon/data/files` and
`%LOCALAPPDATA%/Simon/data/agents`; two owner-scoped historical artifact downloads matched
their original hashes afterward. Operator configuration remains in the checkout.

The `files` root includes private pre-edit versions. The `agents` root includes
artifacts, execution journals, full tool evidence, the environment journal and retained
working directories. PostgreSQL
holds business records and references to these files. Owner-accessible Desktop,
Documents, Downloads and additional host roots are NOT part of managed storage
and need their own backup policy. Cloud files also need a separate export/backup
policy; cloud synchronization alone is not a backup.

## Combined recovery bundle

The existing nightly `backup_local.py` job backs up only PostgreSQL. The
`backup_bundle.py` command creates a complete managed-data recovery bundle during
a maintenance window. The manual command itself does not stop services.

For the registered Windows installation, run `scripts/backup-full.ps1` for the full
workflow, or install `scripts/install-full-backup-task.ps1`. The task checks hourly,
creates at most one verified bundle per idle day and defers while agent/assistant compute
is queued or running. It honors existing maintenance/stop requests, owns its stop markers,
drains all three registered services, then resumes only those previously running. A drain
timeout preserves the stop markers for operator review; no process is killed.

The default destination is `%LOCALAPPDATA%/Simon/backups`, outside the checkout. Use
`-BackupRoot` for a different private volume and `-Force` to bypass the recent-success
check. The restore check uses a new disposable database. `last-success.json` is written
only after file checks and database restoration pass. Existing bundles are retained;
capacity, off-machine delivery and encrypted key recovery need deployment configuration.
This automation assumes the configured database is the repository's Compose `jarvis`
database. It compares cluster and database identities before stopping services and refuses
a mismatch; external database installations must adapt the dump/restore adapter first.

1. Stop the API, assistant worker, agent dispatcher and any external file writers.
   Disable any recovery/startup tasks that could restart them during the operation.
   Leave PostgreSQL running. Finish or cancel active agent work first; do not copy
   a journal while containers or machine runners are still writing workspaces.
2. Run from the repository root with the same configuration as the services. Both
   configured storage directories must exist; a missing root fails rather than
   silently producing an incomplete backup. On a fresh installation, deliberately
   initialize the two empty directories before using this command.
3. Use a new destination outside both source roots, on a private backup volume:

```powershell
.\venv\Scripts\python.exe scripts/backup_bundle.py create E:/SimonBackups/recovery-2026-09-30 --writers-stopped
.\venv\Scripts\python.exe scripts/backup_bundle.py verify E:/SimonBackups/recovery-2026-09-30 --database
```

This command targets the repository's Compose PostgreSQL service, user `jarvis`,
database `jarvis`. Use `--database NAME` on **create** for another database in that
same service. It does not infer an external database from `SIMON_DATABASE_URL`.
On **verify**, `--database` means perform a restore drill, not select a database.

Creation format-checks the dump, copies managed roots and the configured agent
manifest, and compares every copied file's size/hash and every public database
table's content hash. Source changes prevent publication. A complete bundle is
published by renaming its staging directory. The stopped-writers flag is an
operator assertion, not an enforced filesystem snapshot; before/after checks
cannot detect every possible concurrent write. Large installations may require
filesystem snapshots and a more scalable database verification strategy later.

`verify --database` restores the saved dump into a uniquely named temporary
database, compares its public tables to the bundle's recorded hashes, and removes
only that temporary database. It never restores over the live database. Plain
`verify` checks file integrity without requiring Docker. Hashes detect accidental
corruption, not malicious replacement of both files and manifest.

Bundles are **unencrypted** and contain private user data and encrypted database
credentials. Restrict the backup destination to the service owner using filesystem
ACLs before creation; Unix staging directories use mode 0700, while Windows ACLs
are inherited. Encrypt independent/off-server copies with your backup system.

`.env` is excluded by default. `create --include-secrets` explicitly includes it
as `configuration/server.env`. This does not capture secrets supplied only through
process environment or external secret stores. Preserve those separately, especially
`SIMON_GOOGLE_TOKEN_KEY`. Losing that key requires reconnecting Google even after
database recovery. Protect every bundle, whether or not this flag is used.

Failed operations leave uniquely named `.partial` directories for inspection;
they may contain sensitive data and must not be treated as complete backups.
No bundle retention or deletion runs automatically. Keep known-good copies until
replacement bundles pass the database restore drill and independent storage checks.

## Stage a recovery without touching the running installation

```powershell
.\venv\Scripts\python.exe scripts/backup_bundle.py restore-files E:/SimonBackups/recovery-2026-09-30 D:/SimonRecovery/recovery-2026-09-30
```

The destination must not exist. The command rejects filesystem redirects, verifies
the source and copied bytes, and publishes a new directory containing `files/`,
`agents/`, optional `configuration/`, `database.dump` and `manifest.json`. It never
overwrites live files, imports secrets, switches settings, or starts services.

For actual cutover, keep writers stopped, restore `database.dump` into a new
database following the [persistence runbook](persistence.md), and configure Simon
to use that database and the recovered `files`/`agents` paths together. Reconcile
interrupted runs and leases before enabling dispatch: copied journal records do
not recreate Docker containers or machine runners. Account permissions and
artifact checksums still apply after recovery. Test login, project file download
and artifact retrieval before reopening writes.

## Deployment acceptance and next work

Local/Google agent tools, source-file and binary artifact publication, and the Work
agent dashboard are implemented; see [Work platform](work-platform.md). Private HTTPS
configuration tooling is also implemented; choose the actual origin and complete the
[remote acceptance checks](remote-access.md#acceptance-checks).

The [current roadmap](../next-phases.md) separates these deployment tasks from application
work. Scheduled encrypted off-server backup delivery remains next work. Resumable cloud
transfers and selective synchronization should follow an actual workflow requirement.
