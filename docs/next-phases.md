# Simon roadmap

Updated October 1, 2026. This is the authoritative delivery order for Simon. Detailed
architecture documents explain design choices; runbooks describe implemented behavior.
The remaining subsystem plans provide design context and do not define the current backlog.

The [project continuity roadmap](project-roadmap.md) details durable response waits
and schedules for teams or individual agents within this delivery direction.

Simon is a local workspace for delegating outcomes to configurable teams. The next
milestone is **reviewed project delivery**: submit a brief, receive validated files,
request a revision, recover an interruption, and inspect the accumulated cost through
one project experience.

## Product boundaries

- Simon owns conversations, goals, project execution, team configuration, permissions,
  findings, deliverables, and the review experience. Standalone work and personal projects
  must remain usable without company or home configuration.
- Connected project boards own their business task fields and human collaboration.
  The implemented ClickUp bridge maintains a bounded local execution mirror. Avoid
  creating a competing company task authority inside Simon.
- Managed files and artifacts remain local by default. Connected storage supplies
  authorized inputs and export destinations; each artifact needs one canonical location.
- RobbinsHome owns devices and physical automation. Simon uses its optional external
  API. Printer control, plate swapping, and home workflows belong to that repository.
- Keep the modular monolith and separate workers. Add infrastructure when a measured
  workflow requires another failure, privilege, hardware, or scaling boundary.

## Status definitions

| Status                      | Meaning                                                                                                      |
| --------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Implemented                 | Code and documented usage exist. This does not certify a configured provider or a production deployment.     |
| Needs deployment acceptance | Implementation exists, but installation, account setup, or checks on the actual deployment remain necessary. |
| Next                        | Prioritized application work, ordered below.                                                                 |
| Deferred                    | Retained direction with an explicit condition for revisiting it.                                             |

## Implemented foundation

| Area                      | Current behavior and limits                                                                                                                                                 | Reference                                                                                                                  |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Identity and conversation | Private accounts, sessions, passkeys, saved conversations, personal preferences, shared text and voice context                                                              | [Identity](runbooks/identity.md), [shared context](runbooks/shared-context.md)                                             |
| Project teams             | Editable members and skills, lead planning, validated dependencies, final synthesis, saved backlog and findings, bounded scheduled cycles                                   | [Project teams](runbooks/project-teams.md)                                                                                 |
| Worker execution          | Concurrent bounded execution, durable run claims and reservations, cancellation, isolated Docker environments, explicit reconciliation after interruption; one manager host | [Agent execution](runbooks/agent-execution.md)                                                                             |
| Files and outputs         | Managed/project file operations, authenticated downloads, text and native file publication, bounded ZIP bundles with hashes                                                 | [Work platform](runbooks/work-platform.md), [local files](runbooks/local-files.md)                                         |
| Tool adapters             | Local and Google tools, local Git/Python, document/media processing, static browser capture, scriptable CAD/PCB, optional cloud storage and MCP                             | [Installed adapters](runbooks/work-platform.md#integration-readiness)                                                      |
| Business boards           | ClickUp task selection, explicit publishing/status bindings, durable write reconciliation; provider owns business fields                                                    | [Project boards](runbooks/project-boards.md)                                                                               |
| External actions          | Exact proposal review, optional gateway commitments and prerecorded phone messages, unknown-outcome reconciliation                                                          | [External actions](runbooks/external-actions.md)                                                                           |
| Model controls            | Chat profiles and feedback; worker routing; conservative per-run and scheduled-cycle token-cost admission estimates                                                         | [Model routing](runbooks/model-routing.md), [project teams](runbooks/project-teams.md#manual-review-and-ongoing-schedules) |
| Operations                | Combined database/file recovery bundles, isolated restore checks, HTTPS configuration staging, ingress limits, Windows startup/recovery tooling                             | [Storage recovery](runbooks/storage-recovery.md), [remote access](runbooks/remote-access.md)                               |

## Needs deployment acceptance

These tasks can proceed alongside application work. Record evidence from the actual
installation before marking them complete; historical test totals are not current acceptance.

- Select permanent file, artifact, and configuration paths outside the checkout. Migrate
  with writers stopped, verify hashes, and exercise authorized downloads.
- Select encrypted off-machine backup storage. Schedule complete database/file bundles;
  the existing nightly database-only job does not protect all deliverables. Exercise a
  restore and preserve credential recovery material separately.
- Configure the final private HTTPS origin and provider authentication. Test phone login,
  private uploads/previews/downloads, streaming, voice, Google callbacks, and account isolation.
- Verify cold-start and recovery behavior on the deployed host. Windows logon tasks require
  a signed-in user and Docker Desktop; they do not provide availability before sign-in.
- Provision only the selected workflows' model endpoints, token prices, worker images,
  provider accounts, and scoped grants. A catalog entry or configuration check is not a
  successful live integration test.

## Next application work

### 1 Reviewed artifact handoff

Dependency file transfer is implemented: Docker tasks receive verified predecessor
deliverables and a checksum-bearing input manifest, with the exact references saved on
the task. Workers without a local Docker workspace receive references explicitly marked
as unavailable. Owner-reported file checks and explicit, versioned acceptance are also implemented in Work.
Automated inspection evidence and repair orchestration remain outstanding.
See [artifact handoff](runbooks/agent-execution.md#dependency-artifact-handoff).

- Implemented: reference authorized predecessor artifacts and immutable input revisions.
- Implemented for Docker: import verified files and intact bundles into isolated task
  workspaces with dependency manifests. Remote machine transfer remains unsupported.
- Implemented for owner review: record format, procedure, observations, and pass/fail checks
  against the exact artifact hash. Add independently recorded worker inspection and validator receipts.
- Implemented: explicit acceptance of passing owner reviews, retained accepted-revision history,
  and conflicting-promotion rejection. Add downstream replanning when accepted inputs change.
- Support a bounded repair and review cycle, with source files, previews, and exports
  accessible together from the project.

Acceptance: a maker publishes editable files; a reviewer opens the exact candidate and
finds a seeded defect; a repair creates a new revision; the accepted package and earlier
revision remain retrievable. Unauthorized or altered artifacts cannot enter the handoff.

### 2 Recoverable execution and operations

- Classify interrupted attempts as safely resumable, definitively failed, or requiring
  reconciliation. Preserve the existing prohibition on replaying uncertain commitments.
- Add checkpoint packets, explicit ownership/lease evidence, and stale-worker rejection
  for any new automatic continuation path.
- Show worker health, exhausted resources, pending reconciliation, and actionable recovery
  steps in the project. Keep interactive capacity available under background load.
- Move remote history reads and large local-file/ZIP operations out of broad workspace
  transactions using short claims and version-checked completion; preserve duplicate protection.
- Complete scheduled encrypted backup delivery and measure restore duration.

Acceptance: terminate a worker at representative boundaries; recover safe work without
losing inputs; retain uncertain provider calls for investigation; reject stale publication;
restore project records and their files together.

### 3 One project experience

Creative tooling foundation: opt-in full Blender scripting, a structured machine-application
transport, and an initial Windows UI Automation bridge are implemented. Native inspection and
saving connectors now cover Fusion, Houdini, Rhino, Cinema 4D, SOLIDWORKS, Photoshop, and
Premiere, with parameter edits for Fusion and Houdini. Host scripts, an Adobe UXP panel,
contract tests, and [setup instructions](runbooks/native-connectors.md) are included.
Licensed-host acceptance, richer editing operations, desktop deployment, remote artifact
transfer, and live screen capture remain outstanding; use the
[compatibility matrix](runbooks/agent-environments.md#application-compatibility-targets).

Implemented visibility: active runs and project teams show per-task tool/model activity,
checkpoint timestamps and recent history; published images can be previewed in Work. Live
application screens still require a desktop-session adapter. Prioritize a concrete Fusion or
Premiere workflow before adding general desktop control, and bind any screen feed to the
same authenticated task/lease boundary.

- Connect chat requests, project commands, assistant sessions, agent runs, external reviews,
  and deliverables through a consistent goal and execution history.
- Show the outcome, current plan, progress, blocker, next decision, costs, and accepted files
  without requiring the user to understand the underlying runtime.
- Support corrections and replanning with explicit input/configuration versions.
- Improve provider connect/test/repair flows. Extend scoped OAuth enrollment and refresh
  to a selected provider when a pilot requires it.

Acceptance: begin a request in chat, inspect and steer it from Work, reopen it on another
authorized device, and retrieve the same status and deliverables after a restart.

### 4 Shared spending controls and quality evidence

- Introduce a shared account/project ledger across conversation, voice, planning,
  specialists, reviews, retries, and paid tools, with time-period allowances.
- Reserve against applicable ceilings atomically; settle known usage and retain conservative
  reservations for unknown outcomes. Label estimates and unpriced charges explicitly.
- Expose accumulated usage and projected cost before admitting additional work.
- Evaluate routing and context selection with representative tasks and user feedback.

Acceptance: concurrent work cannot independently spend the same allowance; children and
reviews count toward their parent; unknown usage stays visible; media/tool charges are
accounted for or explicitly blocked by the configured spending policy.

### 5 Event driven ongoing work

- Add structured, source-linked project decisions and business records with effective dates.
  Keep personal memory, scratch work, and accepted project knowledge distinct.
- Resume a bounded project on a correlated reply, approval, or completed tool job rather
  than repeatedly asking a model whether it can continue.
- Persist long tool-job identifiers and reconcile before resubmitting paid work.
- Evaluate a durable workflow engine when a real multi-day process requires multiple
  external events, timers, approvals, and compensating actions. Give one engine ownership
  of each process's transitions.

Acceptance: a supplier/reply or comparable process survives downtime, waits without model
polling, resumes on the correct event, and never repeats an uncertain commitment.

## Pilot workflows and measures

Use these two workflows to drive the next milestone rather than implementing every target
adapter at once:

1. Research to decision package: sourced report, comparison spreadsheet, checked references
   and calculations, recommendation, and unresolved owner decisions.
2. Engineering brief to reviewed artifact package: editable sources, exports, previews,
   validation reports, actual-file review, and a follow-up revision from a changed requirement.

Measure first-pass acceptance, owner correction time, cost per accepted deliverable, artifact
retrieval success, recovery success, and interactive latency under background load. Establish
baselines before setting numerical targets or increasing concurrency.

## Deferred expansion

| Work                                                                     | Revisit when                                                                                                 |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| Additional board providers and company administration                    | A real organization cannot use the existing board boundary or needs a self-hosted provider.                  |
| Broad authenticated browser and desktop automation                       | A selected workflow lacks a suitable API or scriptable adapter and has a defined validation contract.        |
| More CAD/PCB applications and video generation                           | The pilot requires a concrete missing output or editing operation.                                           |
| Remote Git publishing and richer coding runtimes                         | A repository workflow needs reviewed branch delivery beyond the implemented local Git and GitHub operations. |
| Distributed runners, larger seat pools, and alternative storage backends | Measured resource pressure, placement, or artifact volume justifies the operational cost.                    |
| Local inference infrastructure                                           | Representative evaluations demonstrate a quality, privacy, availability, or total-cost benefit.              |
| General checkout and interactive phone agents                            | A specific provider/workflow and exact authorization/reconciliation contract are selected.                   |
| Broader sharing, retention, and semantic memory extraction               | Explicit scope, deletion/retention semantics, and retrieval-quality acceptance tests are defined.            |

## Document ownership and maintenance

This roadmap owns priority and status. Runbooks own setup, operations, and current limits.
The [platform design](architecture/multi-agent-company-platform-plan.md) remains the detailed
target architecture; its original delivery table is design history, not a second backlog.
The [repository boundary](architecture/repository-separation.md) supersedes combined-system
Home/Workshop plans.

The [model management plan](model-management-plan.md) and
[accounts and workshop plan](accounts-workshop-plan.md) retain subsystem design context.
Their dated future-work statements must be reconciled here before becoming new work.
The superseded roadmap, Phase 1 readiness checklist, and combined Simon/Home architecture
have been removed; current behavior belongs in runbooks and boundaries in the repository
separation document. Committed historical versions remain available through Git.

When a change ships, update its runbook and this roadmap in the same change. Separate
implementation from deployment acceptance, link repeatable evidence, and preserve unresolved
limits. Do not use a historical coverage number or an available adapter as proof of readiness.
