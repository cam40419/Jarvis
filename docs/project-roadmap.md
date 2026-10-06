# Simon project roadmap

Updated October 2, 2026. This is the current follow-up list for project teams and
ongoing work within the [overall delivery roadmap](next-phases.md). Local storage remains
the primary home for project files; cloud connections supplement it.

## Required next behavior: automatic project persistence

The owner requires all project context and outputs to be saved automatically. Project
saving must not depend on a button, an agent remembering to call a tool, or another agent
being assigned just to copy a result. This supersedes the initial optional-copy experience.
The owner's subsequent clarification separates history from files: conversation answers,
planning responses and drafts belong in Simon's retained history. The Files view and Drive
should contain actual deliverables with meaningful filenames, not automatic answer exports.

**Status: first implementation wave delivered; remaining requirements are tracked below.**
The October 2 investigation inspected Simon's source and the official sources below.
Peer products were not benchmarked or deployed during this investigation.

### October 2 implementation wave

- Team members have individual cards with their role, capabilities, readiness and current
  assignment. Existing individual skill choices and edit actions remain available.
- The worker journals initial context, visible responses, candidates before review, review
  outcomes and evidence references. Draft and partial response revisions remain retrievable
  in history; the Files view lists actual deliverables. Saving is automatic; **Create editable copy** is an
  optional second representation, not the first durable save.
- `python -m simon.backfill_outputs` previews or explicitly imports retained text from a
  settled historical run. Ownership, status, paging and idempotency are checked; original
  runs and task outcomes remain unchanged. Historical output never recorded cannot be recovered.
- Explicitly granted journal tools retrieve exact, bounded pages from earlier project
  attempts. New tools appear in the skill catalog without widening saved member grants.
- Composer drafts sync to the server with optimistic versions, local offline fallback and
  conflict choices. Record editors and schedule/wait text use the same ordinary draft
  mechanism; authorization changes still require an explicit settings save.
- Versioned supplier/product/quote/contact/decision/note records and reusable procedures
  retain sources, confidence, checked/review dates and provenance. A compact record index
  joins planning context. Agents have individual read/update skills; they cannot certify
  that the owner confirmed a fact. The substantive brief remains separate from run logs.
- Queued follow-ups, saved questions/replies, safe remaining-work continuation, bounded
  automatic execution, and once/daily/weekly schedules now have API and UI paths. Calendar
  work can target the team or one member. See [continuity behavior](project-continuity.md)
  for exact restart, time-zone, budget, overlap and replay boundaries.
- Configured Drive synchronization scans accepted deliverable files as well as explicitly
  named task files. Controller answer/summary exports and journal responses remain local.
  An explicitly named, unchanged project copy of an answer can be a deliverable, such as
  `research/supplier-comparison.md`. Drive keeps its meaningful basename without a generated
  ID prefix. Durable per-file receipts and scan checkpoints survive retries; local saving
  does not depend on cloud availability. Existing cloud edits are preserved. Drafts and
  planning protocol files remain local. Existing uncertain uploads retain their recorded
  request while reconciling the original provider ID, so a name change cannot duplicate a file.
- `scripts/backup-full.ps1` defers active compute, drains registered services without
  killing them, copies PostgreSQL/managed files/agent state/configuration, resumes only
  previously running services, and verifies a disposable database restore. Its installer
  checks hourly for one verified backup per idle day. Secret keys and encrypted off-server
  delivery remain separate deployment work.

### Local activation on October 2

The API and both workers were drained and restarted with this wave. Managed files and
agent state now live under `%LOCALAPPDATA%/Simon/data/{files,agents}`. Migration compared
43 managed files and 431 agent files before atomically updating the two storage settings;
original roots and a private configuration backup were retained. The full recovery bundle
passed all 476 file checks and a disposable restore of 42 database tables. `Simon-FullBackup`
is registered to check hourly for one verified bundle per idle day.

Historical backfill saved six retained partial outputs across 42 inspected runs. Repeating
the scan found no outstanding candidates. Original runs, Stdout Collective's project
state, its final answer and both research-report hashes remained unchanged. Live assets
and routes loaded successfully, and owner-scoped historical downloads passed against the
new data root. Verification did not dispatch model work. This acceptance covers the local
installation; remote-device access and encrypted off-machine recovery remain separate.

Following the owner's filename clarification, Drive sync and the Files view now distinguish
conversation responses from actual deliverables. The live Stdout folder was cleaned using
verified original upload receipts and exact content hashes: 15 unchanged answer exports
were moved to reversible trash, and the manufacturing-strategy and competition reports
were renamed to their authored `.md` basenames. Both report hashes and local originals
remained unchanged. A subsequent live sync returned ready and recreated no answer files.

### Remaining work after this wave

These requirements are **not complete**: mid-task checkpoint rehydration; provider/mailbox
event correlation; automatic handling of uncertain external actions; concurrent project
branches; monthly/daily spending ledgers; schedules outside a project; configurable missed-run
and overlap policies; automatic sourced-brief consolidation and digests; actor-shared project
ACLs; searchable source content across the entire archive; bidirectional/multi-provider cloud
conflict handling; streamed large-media publication; filesystem/database orphan reconciliation;
encrypted off-server backups and retention/disk-capacity policy. Current calendar schedules
coalesce downtime, serialize project work and require a finite run count. Historical drafts
that never reached a durable record cannot be reconstructed.

### Findings that motivated this wave (pre-change audit)

- Successful answers already become immutable local artifacts. `project.output_save`
  creates a separate editable project copy. Persistence and project discovery are disconnected.
- Candidates remain in memory until the worker returns; a crash during review can lose
  the candidate. Checkpoint metadata is not a complete continuation record.
- Partial/failed task text can remain in run history but is excluded from the normal
  project-output inventory, which filters for successful tasks.
- Full tool results are archived, but the internal evidence reader only accesses the
  current worker's buffer. Later attempts cannot directly reuse that archive through it.
- Restart recovery identifies interrupted work but does not automatically resume a saved
  worker checkpoint. A project-level wait currently blocks and pauses the project.
- Project briefs, activity, output artifacts, local files and cloud files have separate
  retrieval paths. Drive sync uses a different artifact registry from team outputs.
- User commands still require plan approval. Project scheduling uses completion-relative
  intervals and a finite cycle count, rather than full calendar/event scheduling.
- Project knowledge and local paths are actor-partitioned. Shared company context needs
  project membership, visibility and attribution, while preserving private source access.
- Nightly database backup excludes artifact/file bytes. Complete recovery-bundle tooling
  exists; scheduled verified off-server delivery is outstanding.

Source entry points: [outputs](../src/simon/services/project_outputs.py),
[worker context](../src/simon/services/agent_worker.py),
[publication](../src/simon/services/agent_dispatcher.py),
[recovery](../src/simon/services/agent_runs.py),
[coordination](../src/simon/services/project_coordinator.py), and
[storage recovery](runbooks/storage-recovery.md).

### Persistence contract

1. **Save before advancing.** Persist requests, supplied context and source references,
   plan/configuration versions, tool requests/results, visible model responses, drafts,
   reviews, final outputs, task changes, waits and action receipts at their execution
   boundaries. Save each candidate before reviewing it, including useful partial output.
2. **One project inventory.** Register every output/evidence record with a stable ID,
   meaningful title, content hash, source references, task/agent identity, visibility,
   timestamps, versions and draft/partial/accepted status. Failed review changes status;
   it does not erase a draft. Auto-saving a claim does not make it a verified fact.
3. **Local storage is authoritative.** Use a permanent data volume outside the checkout
   for bytes and PostgreSQL for metadata/checkpoints. Atomic publication and a durable
   reconciliation queue must close filesystem/database commit gaps. Report Saved only
   after local commitment; surface disk-full failures explicitly.
4. **Immediately useful project files.** Automatically create project library entries and
   readable working-file views, backed by immutable revisions. Offer Open, Revise, Export
   and Share; remove optional Save-to-project as a persistence step. Deliberate document
   editing retains its existing permissions, and saving never implies external publication.
5. **Full archive, relevant working context.** Keep source snapshots, conversations,
   receipts, candidates and outputs searchable. Feed agents a compact current briefing
   plus relevant references, with exact retrieval paths to the full record. Keep credentials
   in the secret store; private provider reasoning is not a project deliverable to collect.
6. **Cloud replication after local saving.** Configured Drive/other destinations consume a
   durable sync queue. Show pending/synced/conflicted states; local work continues through
   cloud outages. Retry transfer without regenerating the document or overwriting conflicts.
7. **Complete backups and explicit retention.** Schedule database/files/configuration
   backups and isolated restore checks. Deduplicate bytes, monitor disk space, and preserve
   history until deliberate archive/delete policies apply. Do not silently expire work.

Large media/CAD outputs also need streamed publication and an explicit size/quota policy;
the current bounded artifact publisher is not a general unlimited file store. Preserve a
recoverable pending record when publication cannot complete, rather than dropping the result.

Backfill existing artifacts, project copies and retained failed candidates using current
ownership and hashes. Link copies to source revisions instead of duplicating them. Report
historical gaps that cannot be reconstructed. Basic persistence belongs to the application,
not to an optional agent skill.

### Smoother and more autonomous work

These recommendations follow from the code audit and peer patterns. The implementation
summary above distinguishes delivered behavior from remaining work; this table describes
the full target.

| Improvement                   | Intended behavior                                                                                                                                                                                          |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One continuing work item      | A request owns its attempts, evidence, files and outcome. Continue unfinished work through linked attempts without asking the owner to reconstruct a new request.                                          |
| Project briefing              | Maintain sourced objectives, business facts, constraints, decisions, open questions and current deliverables. Each member receives the relevant authorized view.                                           |
| Structured business records   | Maintain supplier, product, quote, contact and decision records with evidence and checked dates. Reports summarize these records; later work can update individual facts without rereading every document. |
| Reuse before discovery        | Search retained evidence and revisions first. Refresh stale or changed sources deliberately; preserve the earlier evidence.                                                                                |
| Capability-aware delegation   | Preflight tools/accounts, select members with ready capabilities, and combine related research/writing work. Access changes revalidate affected pending tasks.                                             |
| Adaptive plans                | Revise remaining assignments when findings or priorities change; preserve completed work. Independent tasks continue while another waits.                                                                  |
| Durable waits                 | Save the awaited person/event, correlation ID, deadline and continuation. Release compute and wake only when a relevant event or timer arrives.                                                            |
| Standing autonomy rules       | Support supervised or bounded automatic execution within grants, budgets and project rules. Routine authorized work and local persistence need no repeated plan approval.                                  |
| Reusable procedures           | Separate tools from versioned ways of working: supplier research, weekly reports, source verification or sample-order preparation, each with completion criteria.                                          |
| Reliable routine checks       | Build file URLs, resolve IDs, validate schemas and calculate arithmetic in application code. Use model review for substance and evidence quality.                                                          |
| Focused repair                | Retry the failed read/format/publication stage within limits. Reconcile uncertain external actions against provider state before any resubmission.                                                         |
| One useful project view       | Show the latest outcome, readable answer, documents, active work, pending decisions and upcoming runs. Link attempts and diagnostics beneath them.                                                         |
| Questions during execution    | Answer status questions from saved state without replacing the active request. Record corrections as steering events.                                                                                      |
| Autosaved input drafts        | Persist composer/editor drafts with version/conflict handling and a visible saved state. Keep permission changes clearly staged and applied once.                                                          |
| Attention inbox and digests   | Group missing decisions, exceptions and meaningful milestones. Avoid notifications for every tool/model step.                                                                                              |
| ClickUp continuity            | Keep ClickUp as the business board; synchronize authorized task/progress updates through recorded operations. Simon owns the linked execution state.                                                       |
| Background memory maintenance | Consolidate useful findings/procedures with provenance and revisions. Keep hypotheses, verified findings, owner decisions and operating rules distinct.                                                    |

For the clothing brand, the intended flow is: research and retain supplier evidence;
produce versioned manufacturing and competition reports; update relevant business tasks;
request any missing commercial decision; release compute while waiting; resume the affected
branch on a reply; and include the changes in the next digest. The owner should not need
to request saving or run separate recovery commands for completed stages.

### What other agent systems document

Checked October 2, 2026. These are documented mechanisms, not reliability benchmarks.
The last column contains recommendations for Simon.

| System               | Documented mechanism and limits                                                                                                                                                                                                                                                                                                                                                                                                                           | Pattern to adopt                                                         |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| OpenHands            | Configured conversation persistence automatically records state and events. Cross-conversation memory is separately opt-in, with compact user/project indexes and detailed notes. [Persistence](https://docs.openhands.dev/sdk/guides/convo-persistence), [memory](https://docs.openhands.dev/sdk/guides/persistent-memory).                                                                                                                              | Automatic runtime persistence and a small curated memory view.           |
| LangGraph            | Checkpoints retain thread state; stores hold cross-thread data. Interrupts support external-input waits, but resuming can re-run earlier code inside a node. Production needs a durable backend. [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts).                                                                                                    | Continuation checkpoints, event correlation and duplicate protection.    |
| Letta                | Current docs describe git-backed MemFS and background agents consolidating conversations into inspectable memory. [Memory and dreaming](https://docs.letta.com/configuration/memory), [MemFS](https://docs.letta.com/concepts/memfs).                                                                                                                                                                                                                     | Versioned project memory and background consolidation.                   |
| Claude Code / Cowork | Code has local resumable history and a bounded editable memory index. Cowork projects combine context and tasks; local-folder projects remain on that machine. These are separate product surfaces. [Code sessions](https://code.claude.com/docs/en/how-claude-code-works), [Code memory](https://code.claude.com/docs/en/memory), [Cowork projects](https://support.claude.com/en/articles/14116274-organize-your-tasks-with-projects-in-claude-cowork). | Clear resume/new/branch actions and explicit storage location.           |
| Manus                | Projects supply shared context to new tasks; file changes apply to new tasks. Schedules can continue a task or create a separate one, with an artifact target. [Projects](https://manus.im/docs/features/projects), [schedules](https://manus.im/blog/manus-schedules).                                                                                                                                                                                   | Versioned context inheritance and schedules tied to actual work.         |
| Devin                | Session tools connect progress to commands/edits/browser activity. Skills load procedures on demand; currently one can be active at a time. The older Knowledge feature is deprecated. [Session tools](https://docs.devin.ai/work-with-devin/devin-session-tools), [Skills](https://docs.devin.ai/product-guides/skills), [Knowledge migration](https://docs.devin.ai/product-guides/knowledge).                                                          | Inspectable progress, noninterrupting questions and reusable procedures. |
| Paperclip            | Describes goal-linked tasks, atomic task checkout, budgets and agents woken by schedules, assignments, mentions or approval resolution. Its fixed reporting tree need not become Simon's team model. [Core concepts](https://github.com/paperclipai/paperclip/blob/master/docs/start/core-concepts.md).                                                                                                                                                   | Persistent work queues, task ownership and useful event-driven wakeups.  |

Adopt these behaviors through Simon's existing storage, PostgreSQL and worker boundaries
first. Evaluate the current executor against LangGraph or [Temporal](https://docs.temporal.io/temporal)
with a narrow restart/wait/reply prototype before selecting a migration. Adding a framework
alone does not unify project files, memory or external-action receipts.

### Delivery order and acceptance

1. **Automatic persistence and project library.** Save candidates, register drafts/outputs,
   provide stable links and backfill history. Generate a document without a save tool,
   retrieve it in the next task, and recover the exact draft after a crash during review.
2. **Continuation and evidence reuse.** Restore context and resume only unfinished work.
   Fail after one of two documents completes; prove that document is reused and no settled
   writes or external commitments are repeated.
3. **Project memory and work visibility.** Search authorized records and maintain a sourced
   briefing. A new member must find current facts; a status question must leave work running.
4. **Bounded autonomy, waits and schedules.** Add standing policies, event wakes, calendar
   schedules and digests. Test restart, duplicate/late replies, DST, downtime, overlap and
   spending limits for both team and single-agent work.
5. **Replication and operational continuity.** Sync the unified inventory to configured
   cloud destinations and schedule complete backups. Disconnect Drive, continue locally,
   reconnect and verify the same revision transfers without duplicate files.

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

## Further acceptance: durable waiting and schedules

The first project inbox and calendar implementation is described above. The full behavior
below remains the acceptance target, including external replies and mid-task suspension.

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
