# Simon project roadmap

Updated October 1, 2026. This is the current follow-up list for project teams and
ongoing work. Earlier plans in `next-phases.md` are historical. Local storage remains
the primary home for project files; cloud connections supplement it.

## Completed implementation wave

The user authorized these three priorities first:

1. **Worker reliability:** correct startup/recovery failures, wait for PostgreSQL,
   preserve deliberate stops, retain useful private-safe logs, and verify restart recovery.
2. **File handoffs:** let authorized agents read/import previous run outputs and save
   selected outputs into project files, with project ownership and source provenance.
3. **Long-term project knowledge:** a persistent brief, pinned decisions, and searchable
   historical findings/activity that agents can retrieve beyond the most recent cycle.

Implemented and activated locally on October 1. The API, assistant worker and agent dispatcher
are running with the new tools and knowledge-history index. Scheduled starts now preserve
intentional stop requests; use `scripts/resume-local.ps1` for an explicit resume. Worker startup
waits for PostgreSQL, logs its phase, and bounds migration lock/statement waits. New tools are
individually selectable; existing custom project-member grants are preserved.

Validation passed for unit tests, PostgreSQL persistence and API regressions, browser workflows,
real worker start/drain/restart, scheduled-stop preservation and explicit resume. The pre-change
recovery bundle was restored in isolation: all 42 tables and 192 managed files matched. Local
activation evidence is `.local/project-continuity-activation.json`.

Validation should cover permissions, persistence, restart behavior, interrupted startup,
the real project UI, and a multi-step file workflow. An overnight soak and an actual
Windows reboot are separate operational checks and must not be claimed from unit tests.

## Next: durable waiting and schedules

Requested in this conversation for a later implementation wave. Existing bounded project
autonomy and assistant schedules provide a foundation; the full behavior below still needs
implementation and acceptance testing.

### Work that waits for a response

- Support a task waiting for a person, another agent, an external provider, an approval,
  or an expected event, including waits lasting days or weeks.
- Persist the exact question/request, expected responder or event, correlation identifier,
  checkpoint, files, deadlines, and next action. Resume from saved state after browser close,
  server restart, worker replacement, or a temporary provider outage.
- Release worker slots, containers, and model execution while waiting. Do not repeatedly
  call a model to ask whether a response has arrived.
- Accept responses through a clear project inbox and supported provider callbacks/polling.
  Authenticate incoming responses, keep project access controls, deduplicate events, and
  associate late/out-of-order responses with the correct pending request.
- Show waiting, blocked, scheduled, resumed, completed, and canceled states distinctly,
  including what is needed, who is responsible, and the next deadline.
- Allow reminders, deadlines, escalation, cancellation, and manual resumption. Define what
  happens when a reply never arrives or comes after cancellation.
- Revalidate current permissions and approval requirements before resuming external actions.
  An unknown booking/order/call outcome must be reconciled before any retry.
- Acceptance: start a request, restart the server while it waits, deliver a reply twice,
  and verify exactly one continuation without duplicate side effects or lost context.

### Schedules for a team or one agent

- Create schedules within a project/team or assign them to one selected agent. Save the
  task instructions, agent/team identity, project context, tools, budget, and approval policy.
- Provide one-time dates and times; daily schedules at a selected time; weekly schedules
  on selected days; and explicit custom recurrence where useful. A standalone agent should
  not require creating an artificial multi-agent team.
- Require an explicit time zone, defaulting to the user's configured zone. Display the
  next several occurrences in local time. Specify daylight-saving behavior for nonexistent
  and repeated wall-clock times.
- Persist schedules and occurrences independently of a browser session. Support pause,
  resume, edit, cancel, run now, and a visible execution history with files and findings.
- Define missed-run behavior after downtime (skip, one catch-up, or bounded backfill),
  overlap policy (skip, queue, or explicitly allowed parallel runs), and bounded retries.
- Use durable occurrence IDs, leases, and idempotency so multiple workers and restarts
  cannot silently launch the same occurrence twice. Avoid duplicate external commitments
  when a worker loses contact after a provider accepts an action.
- Separate a scheduled occurrence that is waiting for a response from an actively running
  occurrence. Make later-occurrence behavior explicit if the prior one is still waiting.
- Add notifications for upcoming approvals, failed runs, long waits, and completed results.
  Include direct links to the correct project/task and controls for quiet hours and digesting.
- Acceptance: daily and weekly team/agent schedules; one-time execution; time-zone/DST
  boundaries; overlapping runs; paused schedules; duplicate scheduler workers; restart
  during dispatch; and downtime that crosses several scheduled occurrences.

## Remaining deployment and product work

### Connect ClickUp and validate one real project

Keep ClickUp as the selected project-management integration. Configure the account,
allowed workspace/List IDs, and scoped credentials. Validate task creation, assignments,
dependencies, status updates, progress comments, files, and lead review with a small real
project and individually configured team members. Test interrupted-write reconciliation.
The adapter exists; provider configuration and live acceptance are still required.

### HTTPS, permanent storage, and cloud files

Choose the production HTTPS hostname/provider and authenticate it; update passkey and
Google callback settings consistently. Verify login, project files, runs, cancellation,
and downloads from a device outside the local network.

Choose a permanent local data volume outside the source checkout. Connect the desired
Google Drive account and selected folders for imports/exports. Verify the chosen scope
and file access. Drive is not an automatic mirror of the local volume.

Schedule combined database/files/configuration backups, encrypt them, copy them off the
server, define retention, and perform periodic isolated restores. The current nightly
backup covers PostgreSQL only; combined recovery-bundle tooling already exists.

### Supervision and spending

Add opt-in notifications for blockers, approvals, completed work, and failures; useful
progress digests; consistent displayed estimates and execution reservations; measured
project spending; and project/monthly/company limits. Validate long-running operation
over time, including dependency outages and safe operator recovery.

For unattended operation, move beyond the current Windows sign-in/Docker Desktop
dependency or explicitly document that operational requirement.

### Advanced external actions and developer tools

Connect and test booking, ordering, reservation, and phone providers with appropriate
review and reconciliation. Add interactive authenticated browser workflows and
conversational phone handling. Complete remote Git publishing with scoped credentials,
reviewable changes, and a tested recovery path. Existing adapter/template presence is
not evidence that a provider is connected or a live action has been validated.

## Related operating guides

- [Project teams](runbooks/project-teams.md)
- [Local operations](runbooks/local-operations.md)
- [Work platform](runbooks/work-platform.md)
- [Remote access](runbooks/remote-access.md)
- [Storage and recovery](runbooks/storage-recovery.md)
- [ClickUp setup](runbooks/clickup-provider.md)
- [External actions](runbooks/external-actions.md)
