# Completed source extraction: Simon and RobbinsHome

Jarvis owns models, conversations, memory/context, voice, projects/files, connected services,
AI tasks, and work sessions. RobbinsHome owns physical devices, inventory and organization,
command receipts, metering, printers, displays, home UI, household authentication, storage,
automation schedules/triggers, and the physical automation worker.

The former `WorkflowService` only supported device inventory/control, printer conditions,
and timing/echo steps. It moved with the home framework. Simon's AI task and work-session
coordinators stay here. The former combined worker was split into independent processes.

There are no Python imports, editable-package dependencies, runtime database reads/writes,
or source-checkout requirements across repositories. Both package and CI configurations
install their own project. Simon's only integration is `adapters/home_client.py`, optional
`SIMON_HOME_API_URL` / `SIMON_HOME_API_TOKEN`, external tool schemas, and response DTOs used
for historical receipt rendering. Provider secrets and hardware settings were removed.

RobbinsHome has independent password/passkey identity, a PostgreSQL database, household
permissions, and a revocable allowlist for external tokens. Identity headers select an
explicit server grant; callers cannot choose their scopes or gain access to another
household. Browser session cookies differ between applications.

Command claims are committed before device I/O. Stable actor/household/run/device keys
prevent concurrent or delayed retries from actuating twice. Accepted/verified/failed/unknown
receipts remain home-owned. Simon checks its active run before dispatch and fetches bounded
history through the API. Home outages do not disable the assistant's other functionality.

## Deployment and existing data

See [RobbinsHome startup and cutover](https://github.com/cam40419/RobbinsHome#existing-installation-cutover) for exact commands.
The API defaults to port 8001; its database defaults to port 5433. Simon keeps port 8000 and
its existing database. Existing home data is transferred with the read-only source exporter
and transactional importer in `robbinshome.transfer`. IDs, device state, command history,
power samples, workflows, and selected identity records transfer without AI data. Imported
schedules/triggers are disabled, active workflows paused, and executing receipts unknown.
The asset-copy utility verifies file hashes and refuses overlapping/existing destinations.

The refactor does not automatically operate on production data, restart running services,
change Windows scheduled tasks, or switch physical controllers. Deployment cutover requires
stopped services and the documented export/import. Source data and SQL history remain intact
for rollback. No destructive table-removal migration is included.

Existing `Simon-Workflow` launch paths remain compatible but now run only AI tasks/sessions.
Home automations must use RobbinsHome's separate worker. The companion AutoSwap project is
an existing external printer dependency; its adapter and all repository-owned print logic
now live in RobbinsHome.
