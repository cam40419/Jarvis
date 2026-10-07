# Simon roadmap

Updated October 7, 2026. This is the authoritative delivery order for Simon. Detailed
architecture documents explain design choices; runbooks describe implemented behavior.
The core-first sequence below supersedes the earlier private-pilot ordering. Existing implementation summaries and gaps remain evidence, not a second competing backlog.

The [project continuity roadmap](project-roadmap.md) records the October 2 requirement
for automatic saving of all project context and outputs, peer-system research, durable
response waits, and team/agent schedules. The first integrated persistence, team-card,
workspace-record, continuity and calendar wave is implemented; the roadmap distinguishes
its tested boundaries from the remaining company-scale autonomy work.

Simon is being designed for public SaaS, starting with local development and later hosted operation with optional local workers. The next milestone is **an accepted platform core**: create and steer a project/team, manage human and agent work, review versioned artifacts, enforce resource/model limits and recover interruptions. Real business tools follow individually after that gate.

**The AI platform is the primary deliverable. Stdout is its pilot and evaluation project.** Use the supplied brand archive to specify and test reusable platform behavior. Brand strategy, collection development and company operations become work performed through accepted platform capabilities; resolving those business decisions is not a prerequisite for architecture or core development. The prepared private review package is input evidence, not proof that Simon can produce it autonomously.

## Confirmed direction and current delivery order

The owner selected eventual public SaaS with configurable full administration; local development first and primarily hosted operation later; optional local workers; compute/storage subscription tiers; full project-board operations for both agents and humans; the stdout clothing-brand pilot; and interactive creative reviews configured per project. Future SaaS keeps a free default model profile with project paid API keys and cost limits. Paid OpenAI is allowed for stdout after secure key enrollment and an explicit cap.

No meaningful Simon projects require migration, so a clean rebuild is permitted where justified. The separate stdout brand archive is valuable existing work: 218 source files remain preserved unchanged in `Downloads/stdout_context`. Private intake inventories, product details and review drafts stay outside committed platform documentation.

**Complete and test the initial core, then implement and accept one business tool at a time.** Model transport, generic files and reference/test tools are necessary core infrastructure. They do not imply implementing all business adapters during the foundation build.

| Order | Work                                                                                                  | Acceptance reference                                                                                                                      |
| ----- | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | PLAN-01 platform workflow contracts, evaluation scenarios and core backlog using stdout inputs        | [Pilot charter](architecture/clothing-brand-pilot-charter.md); platform acceptance does not depend on settling brand direction.           |
| 2     | PLAN-02 board/hosting/workflow decisions and clean-build versus reuse prototype                       | [Platform plan](architecture/autonomous-work-platform-plan.md) and [hosting comparison](architecture/hosting-and-capacity-plan.md).       |
| 3     | SaaS tenants/admin, projects/boards, model registry/keys/budgets, artifacts and entitlement contracts | Shared isolated state and enforceable human/agent/resource authority.                                                                     |
| 4     | Core intake/staffing/execution/review UI, waits/schedules, optional runner and reference connector    | Actual workflows with deterministic fixtures, free/local policy tests and selected capped inference.                                      |
| 5     | Security, concurrency, failure recovery, restoration, usability and model evaluation                  | All mandatory [core acceptance scenarios](architecture/clothing-brand-pilot-charter.md#core-acceptance-matrix) pass with recorded limits. |
| 6     | Select, implement, test and accept one real tool; repeat based on clothing-pilot needs                | [Integration work packages](architecture/integration-delivery-plan.md); do not expose unaccepted capabilities as production-ready.        |
| 7     | Hosted beta and commercial release, including billing as its own integration                          | Hosted capacity, tenant lifecycle, operations and supported tool acceptance.                                                              |

The first implementation uses native project and board records, reusing Python/FastAPI, PostgreSQL and session identity. External board adapters remain optional, with one authority per project. Exact host/model and subscription prices remain design choices. Agents and humans having full board access is confirmed; it does not give agents billing/publishing authority. The source assessment favors retaining useful contracts and tests, while the implementation and schema can be replaced without mandatory historical backfill.

The prior delivery estimate mixed several tools with a private pilot and does not estimate this new scope. Re-estimate from the core backlog. Product decisions above supersede any conflicting sequencing statement in older plans or the implementation gap inventory below.

The [native project foundation](runbooks/native-projects.md) starts DATA-01/DATA-02: explicit shared projects, membership, human/pool tasks, optimistic edits, idempotent commands and atomic claims are implemented behind `/v2/projects`. Native migration, persistence and HTTP acceptance passed locally on PostgreSQL 16.15 with pgvector 0.8.6, including non-UTC timestamp consistency and independent connection races. The [database test runner](runbooks/database-testing.md) makes that verification repeatable. PostgreSQL 17 remains the CI/container target; an actual version 17 run is still pending. This does not complete SAAS-01, automatic staffing, artifact review or the full core gate.

The native interface at `/projects` now begins UI-01: brief creation/editing, shared task boards, human assignment and claims, membership controls, archive/restore, search/filtering, conflict review and uncertain-write retries. Browser acceptance covers actual authenticated services, mobile/keyboard use, pagination and failure recovery. It provides basic brief intake; AI-led interviews, file ingestion, staffing, artifact review and model settings are still open. See the [native project runbook](runbooks/native-projects.md) for usage and exact boundaries.

The complete retained legacy browser suite still needs contract reconciliation before a full CI acceptance claim. The native UI slice verifies selected legacy navigation, recovery and draft-focus flows; its runbook records older individual-skills expectations found outside that selection. Keep that cleanup in the core quality gate alongside PostgreSQL 17 validation.

Next implementation work: scoped agent-principal and native team contracts, followed by AI-assisted intake/planning against those authorized identities. Retain PostgreSQL 17 validation as a release check, and continue the intake-to-review UX, workflow/model/storage decisions and quality evaluation set alongside those slices. The **Legacy work** UI and execution services retain their v1 authority until their replacements pass a tested cutover; native project IDs cannot invoke those legacy workers. The private Stdout task register supplies candidate business work; this roadmap owns platform engineering priority.

## Current implementation boundaries

- Simon owns conversations, goals, project execution, team configuration, permissions,
  findings, deliverables, and the review experience. Standalone work and personal projects
  must remain usable without company or home configuration.
- Connected project boards own their business task fields and human collaboration.
  The implemented ClickUp bridge maintains a bounded local execution mirror. Avoid
  running two simultaneous task authorities for an existing binding. New v2 projects use native Simon records; their tasks are not mirrored into the legacy binding or actor-specific work state.
- Managed files and artifacts remain local by default. New worker context, responses,
  evidence and drafts are journaled automatically for retained history. The Files view
  and Drive replication expose actual deliverables with descriptive filenames; answers
  are not automatically exported as documents.
  Connected storage supplies authorized inputs and replication/export destinations;
  each artifact needs one canonical identity and retained versions.
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

The native project API is the first replacement slice, documented in the [native project runbook](runbooks/native-projects.md). The table below describes the earlier application capabilities that remain active pending their corresponding cutovers. Their presence does not imply that they support native v2 projects.

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

- Managed files and agent state were migrated to `%LOCALAPPDATA%/Simon/data` on October 2;
  source/destination hashes and owner-scoped historical downloads passed. Original roots
  remain available for rollback and historical leases. Select a separate permanent home
  for operator configuration when packaging the installation independently of this checkout.
- Select encrypted off-machine backup storage. The full-backup workflow now schedules
  complete managed-data bundles and isolated restore checks in idle maintenance windows.
  The local task is installed and its first 476-file/42-table restore check passed;
  preserve credential recovery material separately.
- Configure the final private HTTPS origin and provider authentication. Test phone login,
  private uploads/previews/downloads, streaming, voice, Google callbacks, and account isolation.
- Verify cold-start and recovery behavior on the deployed host. Windows logon tasks require
  a signed-in user and Docker Desktop; they do not provide availability before sign-in.
- Provision only the selected workflows' model endpoints, token prices, worker images,
  provider accounts, and scoped grants. A catalog entry or configuration check is not a
  successful live integration test.

## Existing gaps to map into core and individual tool work

The sections below preserve the earlier implementation gap inventory. The delivery order above owns priority. Core behavior is tested with reference tools; specialized production validators and provider workflows are accepted later with their individual tool.

### 1 Automatic project persistence and reviewed outputs

The owner's October 2 requirement is that project context, drafts, evidence and outputs
are always saved and discoverable. The first implementation wave now journals candidates
before review and retains partial responses and evidence. Remaining work is unified
authorized discovery, complete artifact/continuation records and stronger file inspection;
optional editable copies are not the initial persistence step.

- Preserve candidate-before-review journaling and add any missing continuation records.
- Unify discovery of drafts, outputs and evidence through an authorized project inventory with stable
  links, meaningful names, source references and immutable versions.
- Preserve automatic history ownership and explicit export/editable-copy actions.
  If old data is retained, backfill its inventory without rewriting source bytes or turning every
  saved response into a deliverable file.
- Keep local commitment independent of cloud availability; connect the unified registry
  to replication and complete backups as those operations are implemented.

Acceptance: create an output without a save tool, find it in the project, retrieve it in
a later task, and recover its exact draft after terminating the worker during review.
Failed review must leave a visible saved draft, with an accurate completion status.

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

## Pilot and measures

The selected pilot is **stdout**. Its archive and eventual identity, product, design and operating work provide realistic inputs for evaluating Simon. The [pilot charter](architecture/clothing-brand-pilot-charter.md) maps these inputs to reusable platform capabilities and acceptance evidence. The immediate review concerns the platform's intake, team creation, boards, visibility, model controls, output quality and recovery. Brand reviews occur within the pilot as the corresponding platform capabilities become ready. Source-art rules remain explicitly **OPEN**; unresolved brand choices should appear as human tasks and persisted waits, without blocking independent platform work.

Use sanitized representative fixtures and generic reviewed artifacts to test the core. Intake must surface conflicting palette guidance and duplicate style identifiers without silently overwriting source evidence. Reviews must preserve exact artifact versions and unresolved decisions. Project schemas, staffing and approval mechanisms must also work for other company and task types; apparel details belong in project configuration and later tools. After core acceptance, choose one real tool at a time and validate it on the brand's actual work. Research, editable documents, costing spreadsheets, creative generation, apparel design, Git/code delivery and website operations are separate work packages.

Git and local generator development are concrete pilot requirements after the core gate. Generator acceptance must reproduce approved files from the same versioned code, assets, parameters and seed, distinguish seed uniqueness from visual uniqueness, and retain curation and production evidence. These are future tool checks, not results established by intake or core fixtures. Website development and management likewise require reviewed repository changes and separate deployment/commerce acceptance.

Measure first-pass acceptance, owner correction time, cost per accepted deliverable, artifact retrieval, recovery, tenant isolation and interactive latency under background load. Establish hardware/workload baselines before claiming capacity or setting subscription prices.

## Expansion conditions

Tenant/admin, model/local-worker and entitlement contracts now belong to the core. The table concerns additional production integrations, dedicated infrastructure and scale beyond that tested foundation.

| Work                                                                  | Revisit when                                                                                                                                          |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Additional external board providers and business administration tools | A real organization cannot use the existing board boundary or needs a self-hosted provider.                                                           |
| Broad authenticated browser and desktop automation                    | A selected workflow lacks a suitable API or scriptable adapter and has a defined validation contract.                                                 |
| More CAD/PCB applications and video generation                        | The pilot requires a concrete missing output or editing operation.                                                                                    |
| Remote Git publishing and richer coding runtimes                      | The stdout generator and website now supply concrete use cases; accept one Git/code package after the core, then expand from measured workflow needs. |
| Larger distributed runner fleets, desktop seats, and storage backends | Measured resource pressure, placement, or artifact volume justifies the operational cost.                                                             |
| Dedicated local GPU purchases and inference optimization              | Representative evaluations demonstrate a quality, privacy, availability, or total-cost benefit.                                                       |
| General checkout and interactive phone agents                         | A specific provider/workflow and exact authorization/reconciliation contract are selected.                                                            |
| Broader sharing, retention, and semantic memory extraction            | Explicit scope, deletion/retention semantics, and retrieval-quality acceptance tests are defined.                                                     |

## Document ownership and maintenance

This roadmap owns priority and status. Runbooks own setup, operations, and current limits.
The [October 7 platform proposal](architecture/autonomous-work-platform-plan.md) is the
current proposed target and decision register. The
[earlier platform design](architecture/multi-agent-company-platform-plan.md) supplies
compatible background; its original delivery table is design history, not a second backlog.
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
