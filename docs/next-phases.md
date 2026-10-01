# Simon: practical-use roadmap

> Historical roadmap for the former combined system. Home control now lives in
> RobbinsHome; several items below have since shipped or moved. For the current
> implementation use the README and agent runbooks. The next storage/recovery
> sequence is recorded in [local storage and recovery](runbooks/storage-recovery.md).

## September 30: current implementation and remaining setup

The [Work dashboard](runbooks/work-platform.md) now provides reusable teams, task
dependencies, plan review, durable execution, cancellation and authenticated
deliverable downloads. Tool discovery includes configuration blockers and a
searchable catalog. Local files remain the primary storage; cloud adapters add
bounded access to connected sources rather than replacing the local filesystem.

Concrete adapters now cover local/project files, Google, Docker Python/Git,
documents/OCR/media, static browser capture, OpenSCAD/Blender CAD, KiCad PCB checks/exports,
image generation/transcription,
GitHub, WebDAV, Dropbox, Box, OneDrive/SharePoint and remote MCP tools. Configuration
templates for other applications remain disabled; template presence does not mean
the application is installed. The starter has nine teams and separate grants for
readers, writers and isolated workers.

[Project teams](runbooks/project-teams.md) now support a custom roster, lead and role
responsibilities. Natural-language commands create durable lead planning runs, validated
specialist dependency graphs and final reviews. Saved todos, findings, archived tasks and
activity support bounded scheduled work with pause and recovery controls. Project state
uses the existing PostgreSQL job journal; closing a browser does not stop its workers.

[Managed project boards](runbooks/project-boards.md) connect selected ClickUp tasks to
the local execution backlog. ClickUp supplies the shared company workspace and board
views. Explicit bindings control task publishing, progress comments and status updates;
durable operation records hold interrupted writes for reconciliation. Local artifacts
remain on the server. A ClickUp account, token and allowed workspace/list IDs must be
configured before using the connection. OpenProject is the planned self-hosted alternative.

[External actions](runbooks/external-actions.md) add reviewable booking/order/reservation
proposals, optional merchant quote/commit adapters and Twilio outbound phone messages.
Live commitments require exact user confirmation and configured providers. Unknown outcomes
are held for reconciliation instead of being submitted again. Interactive conversational
phone agents and general website checkout remain additional integrations.

Deployment work that depends on the operator's infrastructure:

- Select the permanent data volume and an encrypted off-machine backup destination.
  Combined database/file bundles and isolated restore checks are implemented.
- Select and authenticate the private HTTPS hostname for remote access; update
  passkey enrollment and Google callbacks on that origin.
- Provision repository/folder-scoped tokens for additional cloud services and
  define their workspace/actor grants. Their API adapters are implemented; a shared
  in-app OAuth enrollment/refresh flow for these providers is still future work.

The HTTPS setup tool now stages a consistent origin, passkey RP, callback and proxy
configuration with private rollback copies. Startup/recovery tasks, HSTS and HTTP
body/authentication limits are implemented. Provider selection/login and final
remote-device acceptance remain deployment steps.

Additional application work remains for other CAD/PCB desktop integrations, video
generation, interactive authenticated browser tasks, remote Git publishing,
artifact transfer between isolated tasks, and distributed machine-runner software.
These require their own concrete adapters, application provisioning and acceptance
tests. They are not enabled by turning on a generic template.

## Workflow execution foundation

The [workflow architecture](https://github.com/cam40419/RobbinsHome/blob/main/docs/workflow-architecture.md) now has an executable first phase: versioned
definitions, private runs, timed waits, dependencies, a separate durable worker, attempt/job records,
lease recovery, pause/resume/cancel, and a timeline API. Migration 0014 adds its storage. Only echo
and saved-inventory reads execute today. Start the worker using `scripts/start-workflow-worker.ps1`.
Condition monitoring, recurring triggers, Automations UI/chat controls, and physical actions follow.

## Shared context across text and voice

Implemented account-scoped conversation recall and personal memory tools. Voice starts with a
bounded context snapshot and delegates recall/saving to the same backend as chat. Personal facts,
preferences, and projects can be saved from current user statements, corrected, or retracted.
Existing stored text and voice history is searchable without a backfill. New conversations are
private; legacy shared threads keep their visibility and exclude private context. Migration 0013
adds conversation visibility and recall indexes. See the [context runbook](runbooks/shared-context.md).

## Account and workshop follow-up

Private account invitations, access revocation, and per-user personality/voice preferences are now implemented.
The [accounts and workshop plan](accounts-workshop-plan.md) is the proposed layout and delivery sequence
for project folders, file management, Bambu A1 slicing, the existing plate-swap generator, and print automation.
Filesystem and printer execution have not been enabled. Per-workspace home-provider credentials,
connector pairing, explicit sharing, and account budgets remain planned.

## September 16 phase: Home, voice, and remote access preparation

Implemented:

- A dedicated Home dashboard groups existing inventory by room. Light cards support power, brightness, and color according to device capabilities; outlet cards link to existing naming/load setup.
- Direct dashboard changes reuse permission checks, idempotent receipts, live preflight, and readback verification. Uncertain outcomes require a status check before another UI command.
- Shelly charts show 1/6/24-hour power histories and observed kWh, coverage, missing readings, and counter resets. The UI never treats unavailable samples as zero consumption. Status is refreshed explicitly; the existing backend continues sampling power.
- Live OpenAI WebRTC voice, independent of the text model, with server-side delegation into Simon's existing Auto routing and tools. Captions, recent call transcripts, mute, end call, and Stop task are available in chat.
- Session ownership, CSRF, single-call admission, duration/start limits, heartbeat expiry, cancellation revalidation, server-only delegation results, and cumulative usage persistence.
- `/simon` URL prefix support for pages, assets, auth, chat, streaming, and Google callbacks. Vercel external rewrite and named-tunnel examples, a production launcher, and passkey enrollment instructions.

The live synthetic WebRTC test connected and received final usage confirmation. Mocked integration tests exercise delegation and cancellation. Human speech quality, interruption timing on the user's phone, and physical home-device behavior still need acceptance testing. The public tunnel and Vercel portfolio routes were deployed September 16, then paused in favor of the faster local app at `http://localhost:8000/login`. Windows startup tasks were installed later that day, as described below.

Verification: 447 tests passed with PostgreSQL and browser checks enabled, at 95.23% coverage. Four opt-in paid tests were skipped in that suite; the new live WebRTC test passed separately. Ruff, strict mypy, and the production configuration check passed. Migration 0011 was applied to the local development database. No household devices were switched during these checks.

See [remote and voice setup](runbooks/remote-voice.md). Migration 0011 stores voice-session records. The first version requires one server worker and a foreground browser tab; it has no background wake word or mobile app service.

## Local PC rollout

The `cam40419` account is the configured site administrator. Simon runs at localhost under one
non-reloading server worker, with password login available and development-token login disabled.
The seven transferred devices remain associated with this account's workspace. Windows logon,
recovery, and daily backup tasks are installed for PostgreSQL and Simon. A controlled shutdown,
stopped-container recovery, and a 37-table restore comparison passed. The tunnel remains stopped.
See [local operations](runbooks/local-operations.md). An actual Windows reboot is still needed to
confirm the logon behavior from a cold start; the task triggers and manual recovery passed.

Next:

1. Exercise each of the seven transferred devices from `cam40419`: read status, on/off, brightness/color where supported, readback, meter samples, and unavailable-device handling. Record any device-specific fixes without switching unrelated loads.
2. If remote access is needed again, serve only the browser interface from the website and proxy its authenticated API/streaming calls to this PC. Implement remote-origin login, cookies, CSRF, throttling, voice, and browser tests before enabling the route.

## Next: workflows, scenes, and schedules

The [workflow automation plan](https://github.com/cam40419/RobbinsHome/blob/main/docs/workflow-automation-plan.md) expands automations into full workflows: scheduled individual steps, condition waits, monitoring, branches, durable recovery, and a run timeline. Build the execution foundation before printer automation, using simulated tools and existing read-only home capabilities first. Chat and UI will share the same workflow controls and authorization rules.

- Persist named scenes such as Work, Wind down, and All lights off; edit their device membership and settings from Home and chat. Capture per-device receipts and partial failures.
- Add an actual scheduling worker with timezone-aware routines, repeat rules, retry/idempotency policy, and an execution history. Existing job records alone do not provide scheduled automation.
- Extend power views with daily aggregates before offering weekly/monthly charts; account for missing coverage and meter resets. Add tariff/cost estimates only after the user provides a rate.

## Next: faster and more capable assistance

- Measure first speech, first text, delegation latency, and interruption latency on realistic personal tasks. Tune routing with those measurements rather than a claimed speed target.
- Add configurable budgets and usage visibility across speech and delegated reasoning. The existing call limits do not provide an account-wide spending cap.
- Evaluate direct handling of well-defined device commands and reusable scene actions. Retain ambiguity checks and permissions.
- Extend Google with calendar edits/recurrence and inbox reading, then evaluate reservation-specific integrations. Arbitrary website form completion is still unavailable.

Keep [model management](model-management-plan.md) and [home integration](https://github.com/cam40419/RobbinsHome/blob/main/docs/home-integration-plan.md) as the detailed subsystem plans.
