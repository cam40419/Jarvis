# Permanent local storage

The checkout is application code; ongoing project files and agent workspaces can live
in a separate absolute local data directory. For example, Windows installations can
use `%LOCALAPPDATA%\Simon\data`, with `files` and `agents` children. The database,
provider configuration, credentials, and historical records are otherwise unchanged.

Use the full backup maintenance wrapper's `-MigrateStorageRoot` option to combine a
verified full recovery bundle with migration before service restart. Alternatively,
after stopping **all** API, worker, scheduler, and external file writers, run from the
checkout root:

```powershell
.\venv\Scripts\python.exe scripts/migrate_storage.py C:\SimonData --writers-stopped
```

The command copies both configured directories, compares every file's SHA-256 and
size and all directory names, rechecks the original sources and configuration, and
then atomically changes only `SIMON_LOCAL_FILES_DIR` and `SIMON_AGENT_STATE_DIR` in
`.env`. Unrelated environment bindings, including multiline values, are preserved.
An absolute local destination outside the checkout is required. Redirects,
overlapping paths, unexpected destination entries, conflicting files, and linked
destination files are refused. A prior matching partial copy can be resumed without
overwriting existing bytes. Process-level overrides of either setting must first be
removed so the new `.env` values take effect.

The command prints only paths and file counts. Its private
`.local/storage-migrations/<id>/prechange.env` is an exact configuration backup and
contains secrets: do not publish or paste it. The companion receipt records source
and destination inventories, configuration hashes, and the backup location. On
Windows the backup directory receives an owner-only ACL before configuration bytes
are written; on POSIX it is mode `0700` with files mode `0600`.

Original directories are never moved, deleted, or rewritten. Keep them: historical
environment leases can still refer to absolute paths in the former agent directory.
If copying, verification, or configuration publication fails, the original `.env`
remains in place and services can resume using the old roots. A later activation
failure can be rolled back using the recorded private `prechange.env`; first stop
all writers again and preserve any files created at the new location. Do not restore
old configuration over new writes without reconciling them. Migration is not a live
filesystem snapshot; the explicit stopped-writer maintenance window is required.
