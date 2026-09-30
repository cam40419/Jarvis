# Simon: scheduled workflows and monitoring

Status: target design. The workflow foundation, basic Work view, Automations dashboard, one-time schedules, and receipt-backed `home.set` actions for enabled devices are implemented. The A1 card reads LAN status only. Recurrence, telemetry conditions, branches, a visual editor, chat tools, and printer dispatch remain planned. The [workflow architecture](workflow-architecture.md) documents the current APIs and worker.

## User experience

An automation is a versioned workflow with a trigger, connected steps, timing rules, monitoring conditions, and execution limits. It can run for seconds or days independently of an open chat or voice call. Scenes are reusable actions inside workflows, rather than a separate scheduling system.

Examples of requests the completed system should support:

- “Every weekday at 8, turn on my office lights and run the purifier for two hours.”
- “Prepare these models this afternoon, start the batch at 7 tonight, and tell me if the printer needs attention.”
- “After each print finishes, wait for the fixture's cooling conditions, swap the plate, verify it, and start the next item.”
- “Pause tonight's workflow after the current print.”
- “What is the batch waiting for, and when is the next step due?”

Chat translates requests into a typed definition and reports the saved steps and schedule. Ordinary explicitly requested home actions execute directly. Creating a routine from an explicit request grants only its specified scope; it does not add a confirmation to every step. Ask for missing information only when it affects execution, such as an unidentified device or an unspecified print batch limit. Printer routines use the bounded authorization described below.

## Workflow building blocks

| Step | Behavior |
|---|---|
| Action | Invoke a registered tool with validated inputs and capture its receipt/output |
| Schedule or delay | Wait until an absolute time, a permitted window, or a duration after another step |
| Condition wait | Monitor a predicate until satisfied, timed out, or interrupted |
| Branch | Select a named path from validated outputs or observed state |
| Parallel and join | Execute independent branches with an explicit all/any completion rule |
| Bounded repeat | Process a fixed list or repeat within a maximum count and elapsed-time limit |
| Notification | Deliver a status or attention request through a configured, authorized channel |
| Human task | Pause for a specific decision or physical check that cannot be established automatically |
| Subworkflow | Invoke a pinned version of a reusable routine with scoped inputs |

Conditions and input mappings use a small typed expression language, not Python, shell commands, or arbitrary model-generated code. Validate references, types, branches, cycles, resource conflicts, and bounds before activation. Repetition has explicit limits; parallel branches may not simultaneously control the same exclusive resource. A language model may help author or explain a workflow but is not needed for each timer or monitoring sample.

## Scheduling semantics

- Support manual, one-time, recurring, and device-event triggers. Store the workspace's IANA timezone, the original local schedule, and resolved UTC occurrences. Show the next occurrences before activation.
- Give each step dependencies, an optional earliest start, a start-by deadline, a permitted execution window, and a maximum runtime. Distinguish “must start by” from “must finish by.” A window closing does not implicitly cut power or abort a running print.
- Delays are relative to a named persisted event, such as successful completion of slicing. Store the resulting wake time in the database; do not hold a sleeping request or voice session.
- Make daylight-saving behavior explicit. Default to skipping nonexistent local times and running once at the first occurrence of an ambiguous local time. Allow an explicit alternative and preview it.
- Define missed-trigger behavior per routine: skip, run once within a grace period, or bounded catch-up. Default physical start/motion steps to skipping stale occurrences; do not start an overdue print after a home-server outage without a valid remaining window and revalidated authorization.
- Deduplicate recurring and event triggers using a unique occurrence/event key. Define overlap as skip, queue with expiry, or allow within a concurrency limit. Printer workflows serialize through a resource lease even if other work runs in parallel.
- Disabling a schedule prevents new runs; pausing a run affects that run. Editing a definition creates a new version for future runs. Existing runs retain their pinned version unless explicitly replaced from a reviewed checkpoint.

## Monitoring and waits

A condition wait declares its data source, predicate, polling interval or event subscription, freshness limit, timeout, and timeout/failure path. Examples include printer state, slicer process completion, artifact availability, connector health, device power, or plate-presence feedback.

Persist observations with source, device/job identity, observation time, and receipt time. Evaluate only evidence correlated with the current action or print job. A stale “completed” state from yesterday cannot complete today's print step. Missing or stale telemetry is unknown, never false, successful, or zero power by default.

Support consecutive-sample requirements, dwell time, and hysteresis where a fluctuating signal would otherwise trigger repeatedly. Reuse the existing power sampling service where possible, respecting its sampling interval; requesting a faster workflow check does not make older samples fresh. Power draw can indicate activity but does not alone prove print completion or a seated plate.

For the plate changer, use the actual fixture profile and available hardware feedback. Do not invent temperature thresholds or infer safe seating from a command acknowledgement. If reliable feedback is unavailable, the workflow pauses at a clearly described physical verification task.

Timers and subscriptions are durable records. A worker restart reconstructs pending waits, rechecks observation freshness, and continues from persisted state. Monitoring has bounded poll rates and account/device concurrency limits. The UI shows what is being measured, the latest reading, its age, the target condition, and the deadline.

## Durable execution and recovery

Start with PostgreSQL-backed scheduling and a separate worker process alongside Simon on the home server. Keep its interfaces separate from the API process and the future home connector. Browser timers and API background tasks must not own workflow execution. An always-on host is required for on-time work; downtime is visible and handled through the missed-trigger policy.

Proposed records:

| Record | Purpose |
|---|---|
| `workflow_definitions` / `workflow_versions` | Workspace ownership, editable identity, immutable typed graph and tool versions |
| `workflow_triggers` / `workflow_occurrences` | Trigger configuration, next due time, unique occurrence and chosen run |
| `workflow_runs` | Pinned version, initiating actor/grant, input snapshot, overall state and limits |
| `workflow_step_runs` / `workflow_attempts` | Dependencies, resolved inputs, due/deadline times, attempts, outputs, dispatch receipts |
| `workflow_waits` | Persisted timers, predicates, event correlation, last observation and timeout |
| `workflow_events` | Ordered timeline of transitions, decisions, interventions, and redacted errors |
| `resource_leases` | Exclusive ownership, expiry, heartbeat and fencing token for printer/other resources |
| `workflow_grants` | Revocable standing authorization, permitted capabilities and bounded scope |

All records are workspace scoped; references are validated within that workspace. Use the existing job/audit infrastructure for action execution and traceability. A workflow step links its action job and command receipt instead of creating a second source of truth for whether a device command succeeded.

Workers atomically claim due work with leases and optimistic versions. Commit transitions and their outbox events in the same transaction. Persist an action intent before dispatch and use a stable logical action idempotency key across retries. Store individual attempt IDs separately. Heartbeats and lease expiry permit recovery; connectors must reject stale fencing tokens. When a device cannot enforce fencing, its connector must serialize dispatch and reconcile prior in-flight commands before accepting a replacement owner.

Delivery may happen more than once. Physical effects cannot be promised exactly once. Status reads and other proven idempotent steps can retry with bounded backoff. An uncertain print start, plate motion, email delivery, or other non-repeatable effect enters reconciliation, not blind replay. Query correlated device/provider state or require intervention if the result cannot be established. Lease expiry alone is not permission to repeat a physical command.

Expose run states such as queued, running, waiting, paused, needs-attention, succeeded, failed, cancelled, and expired. Preserve attempts and receipts after failure. Retry a failed eligible step from a checkpoint without rerunning successful physical steps. Never silently substitute a newer artifact, tool version, or profile during recovery.

## Permissions, pause, and cancellation

Every dispatch revalidates the account, workspace membership, connector binding, enabled capabilities, resource readiness, and the run's grant. A scheduled run uses its server-side grant; it does not retain a browser session token or expire simply because the browser closes. Account revocation or an expired/revoked grant prevents further dispatch and marks the run as needing attention.

A saved printer routine may authorize a particular printer, pinned files/profiles, maximum items, allowed start window, fixture behavior, and runtime/material limits. Once those bounds are explicitly authorized, it can continue without repeated chat confirmations. Editing the routine cannot silently broaden that grant. New accounts never inherit another account's workflows, roots, connectors, devices, or authorization.

“Pause after this step” prevents subsequent dispatch while continuing observation of an in-flight action. Cancelling a workflow stops future steps and cancels work only where the tool supports it. If a print remains in progress, show “workflow cancelled; printer still running.” A separate printer-stop action has its own receipt. Ending voice or interrupting speech has no effect on an independently authorized workflow.

Failure handling is explicit per step: retry when eligible, take a named branch, pause for help, or fail the run. Cleanup/compensation actions are declared and independently authorized; they cannot undo a physical print or imply rollback of a sent email. Turning off a smart plug is not the default printer cancellation mechanism.

## Automations UI and chat

Proposed navigation:

```text
Automations
  Workflows       saved definitions, enabled state, next trigger
  Workflow        steps and branches, schedule, conditions, execution limits
  Runs            active, upcoming, completed, failed, needs attention
  Run detail      timeline, current waits, outputs, receipts, intervention controls
```

Start with an accessible ordered step editor and branch groups; add a visual graph after execution semantics are stable. On phones, show a compact timeline. Surface current state, next scheduled action, observed progress, expected versus actual timing, latest heartbeat, and stale/offline indicators. Estimates must be labelled as estimates.

Run detail supports pause, resume, cancel, eligible retry, and completing an assigned human task. Explain their effect on any running device action before execution in the control's label/help text. Chat and voice use the same APIs for creating, naming, editing, scheduling, starting, querying, pausing, and cancelling workflows; they do not bypass worker policy.

Persist in-app notifications first. Email or other external notifications are optional configured steps with explicit destinations and authorization; a saved notification step can provide that authorization without another approval on every run. Record delivery failures separately so they do not rerun the print. Deduplicate and rate-limit repeated alerts. A disconnected browser can see missed alerts and run history on reconnect.

## Example: scheduled Bambu A1 batch

1. This afternoon, validate the selected project files and pinned A1/filament/fixture profiles. Slice the bounded list and generate/validate required plate-swap artifacts. Stop on invalid outputs.
2. Wait until 7 p.m. in the configured timezone. Start only within the authorized start window; expire or pause if it is missed.
3. Acquire the printer lease. Check fresh readiness, matching machine/profile, exact artifact hashes, plate state, and remaining batch authorization.
4. Upload/start the selected item once, record its identity, and monitor that print until verified completion, failure, or timeout.
5. Wait for the fixture-defined cooling and parked-state conditions, with fresh evidence and a bounded timeout.
6. If another item is queued and the grant still permits it, execute the validated plate-swap artifact and verify completion and seating. Pause for physical verification if the hardware cannot establish it.
7. Recheck readiness and the next item's start window, then repeat from step 4 until the bounded list is complete. Leave the final plate in place unless the routine explicitly requests a final swap.
8. Record results/artifacts and notify the owner. At any attention state, stop future dispatch, retain monitoring where available, and explain the unresolved condition.

This is a proposed workflow, dependent on the actual A1 transport, installed slicer, existing plate-swap generator, fixture limits, and completion sensors. It is not currently executable.

## Delivery sequence

1. **Workflow foundation, before printer execution:** versioned definitions, typed action/wait graph, persistent runs/attempts/events, manual trigger, scheduler worker, account scope, and restart recovery. Prove execution with deterministic fake tools and read-only inventory/status actions.
2. **Schedules and monitoring:** one-time/recurring/event triggers, timezone previews, missed-run policy, persistent conditions, resource leases, bounded retries, and existing home-action adapters. Exercise explicitly configured routines without changing household devices during automated tests.
3. **Chat and UI:** workflow editor, run timeline, next occurrence, monitoring freshness, pause/resume/cancel, in-app alerts, and chat tools using the same endpoints.
4. **Projects and offline workshop steps:** integrate filesystem grants, immutable artifacts, pinned slicer execution, and the existing generator's prepare/validate tools.
5. **Supervised A1 workflow:** verify actual transport and hardware evidence; complete one print and one swap with recorded outcomes. Add bounded batches only after restart/uncertainty behavior is demonstrated.
6. **Expanded workflows:** reusable subworkflows, richer branches/parallel joins, configured external notifications, per-account budgets, and separate home connectors. Schema design anticipates these; the first release need not expose every building block.

Acceptance checks must cover duplicate trigger delivery; two workers claiming the same step; crash before and after dispatch; stale leases and late responses; restart during waits; DST transitions; missed windows and overlap; condition timeout and stale/wrong-job telemetry; revocation while queued; cross-account references; pause/cancel during physical activity; expired batch authorization; failed plate verification; and notification failure without replaying an action. Use a controllable clock and simulated tools rather than waiting real hours or moving hardware in the test suite.

The home server must show worker health and pending/overdue work, start the worker after reboot, and retain database/artifact backups. A successful UI save is not evidence that a scheduled worker is online.
