# Simon roadmap

Updated October 10, 2026. This is the authoritative delivery order for Simon. Detailed
architecture documents explain design choices; runbooks describe implemented behavior.
Use the core-first sequence below for implementation and acceptance.

The [October 9 competitive research](research/2026-10-09/README.md) maps comparable
products, challenges proposed advantages and defines bounded evaluation experiments.
Its market findings remain dated research. The owner's October 10 decision makes change
impact/repair, usable delivery packets, lower review effort, reliable completion and
authorized continuity mandatory base-platform requirements, as specified in the
[baseline quality implementation plan](architecture/platform-baseline-quality-plan.md).
Other innovation candidates remain research hypotheses; this roadmap governs delivery.

The next implementation slice now provides **immutable delivery snapshots** with exact
multi-file ZIP exports, safe source previews, captured checks/decisions/review states,
durable receipts and current applicability/disclosure checks. Artifact acceptance now
tracks human-only authority and all source revisions beyond the bounded review excerpts;
stale accepted work can be repaired and known revoked evidence is withheld from downstream
execution. See [the artifact runbook](runbooks/native-artifacts.md#delivery-packets).
This is partial baseline work. Typed fact dependencies and selective/transitive repair,
required-file package acceptance, native-format validation, resource metering and the
comparative core gate remain required and unaccepted.

Simon is being designed for public SaaS, starting with local development and later hosted operation with optional local workers. The next milestone is **an accepted platform core**: create and steer a project/team, manage human and agent work, repair changed inputs across affected outputs, deliver usable versioned packets, minimize review effort, enforce resource/model limits, recover interruptions and support authorized takeover. The goal is parity or better with the strongest relevant platforms serving the same supported workflows. Real business tools follow individually after that gate and inherit the same baseline contracts.

**The AI platform is the primary deliverable. Stdout is its pilot and evaluation project.** Use the supplied brand archive to specify and test reusable platform behavior. Brand strategy, collection development and company operations become work performed through accepted platform capabilities; resolving those business decisions is not a prerequisite for architecture or core development. The prepared private review package is input evidence, not proof that Simon can produce it autonomously.

## Confirmed direction and current delivery order

The owner selected eventual public SaaS with configurable full administration; local development first and primarily hosted operation later; optional local workers; compute/storage subscription tiers; full project-board operations for both agents and humans; the stdout clothing-brand pilot; and interactive creative reviews configured per project. Future SaaS keeps a free default model profile with project paid API keys and cost limits. Paid OpenAI is allowed for stdout after secure key enrollment and an explicit cap.

No meaningful Simon projects require migration, so a clean rebuild is permitted where justified. The separate stdout brand archive is valuable existing work: 218 source files remain preserved unchanged in `Downloads/stdout_context`. Private intake inventories, product details and review drafts stay outside committed platform documentation.

**Complete and test the initial core, then implement and accept one business tool at a time.** Model transport, generic files and reference/test tools are necessary core infrastructure. They do not imply implementing all business adapters during the foundation build.

The five baseline capabilities are required core work, not optional enhancements or a
proposal to narrow Simon to one buyer/workflow. Representative workflows measure the
general platform. Core acceptance includes fair configured comparisons and retained
quality/review/recovery/cost evidence; feature presence and synthetic regression tests
alone do not establish parity.

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

Estimate remaining work from this core backlog. The confirmed direction above governs
sequencing across the architecture and integration plans.

## Mandatory base-platform quality

All six work packages below are dependencies of CORE-ACCEPT. Existing slices implement
parts of these contracts; none is declared complete by adding it to the plan.

| Core package                     | Required behavior                                                                                                                                                                | Acceptance                                                                                                           |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| BASE-01 Change impact and repair | Track typed fact/requirement dependencies, detect affected work, preserve unaffected outputs and route bounded corrections; historical approval does not certify changed inputs. | CORE-19: material/irrelevant edits, missing dependencies and revoked evidence.                                       |
| BASE-02 Usable delivery packets  | Keep editable originals, previews, evidence, checks, exact decisions and unresolved limits in one versioned package; verify supported reopen/edit and manifest identity.         | CORE-20: actual multi-file fixture and missing/stale/mismatched components.                                          |
| BASE-03 Lower review effort      | Present meaningful differences, material findings and necessary decisions with visible uncertainty; measure owner effort without weakening quality checks.                       | CORE-21: blind defect checks, correction loop and comparative review burden.                                         |
| BASE-04 Reliable completion      | Recover interrupted/uncertain work without blind duplicate effects, lost state, hidden charges or indefinite starvation; reconcile before retry.                                 | CORE-22: dispatch/lease/restore failures, costs and progress under uncertainty.                                      |
| BASE-05 Continuity               | Another authorized human/runtime resumes with current sources, files, decisions and pending obligations; transfer neither secrets nor revoked disclosure/action rights.          | CORE-23: takeover, model/runtime replacement and permission-safe handoff.                                            |
| BASE-06 Comparative quality      | Match or exceed the strongest eligible same-goal baseline on registered supported-workflow measures; include a strong configured existing stack and retain all attempts.         | CORE-24: per-workflow quality/usability/effort/recovery/cost evidence, no aggregate score hiding a material deficit. |

The [baseline plan](architecture/platform-baseline-quality-plan.md) defines deliverables,
dependencies and implementation order; the [pilot charter](architecture/clothing-brand-pilot-charter.md#core-acceptance-matrix)
defines CORE-01 through CORE-24. Generic package/dependency/validator/recovery/handoff
infrastructure and supported-file proofs belong in the core. Native authoring/rendering
and external writes for each real application remain individually accepted tools. Each
tool must satisfy the baseline for its declared format/provider scope before enablement.

Register comparator configurations, privacy/model/resource constraints, metrics and
materiality thresholds before trials. Resolve material parity deficits and evidence gaps
before accepting the affected capability. The former 30% review-effort improvement idea
is an optional stretch target; parity or better is the required floor. The October 9
[evaluation protocol](research/2026-10-09/evaluation-plan.md) supplies measurement methods.

## Implemented core slices

The [native project foundation](runbooks/native-projects.md) begins DATA-01, DATA-02,
AGENT-01 and UI-01. Shared projects, human membership, versioned board edits, human/agent/pool
assignment and atomic claims are implemented. The browser provides brief/task editors,
conflict review, uncertain-write retries, search/filtering and archive/restore.

Scoped agent identities now have immutable project-local role keys, role instructions,
success criteria, staffing rationale and active/paused/retired states. Owners can manage
the team and configure its active-role limit. Explicitly authorized manager agents can
create and revise their own subordinate roles within that policy; they cannot delegate
management authority, edit themselves, enlarge policy, issue credentials or administer
human membership. Pausing/retiring releases unfinished work and fences task versions.

Temporary bearer credentials are hashed at rest, bound to an agent revision and project,
and checked against current role, project and issuing-owner authority. The native API
supports actual agent authorship and board operations. The browser displays credentials
once and provides revocation. Creation consumes no inference or compute. These are the
authority contracts used by the model-driven staffing procedure.

[AI-assisted intake and automatic staffing](runbooks/project-intake.md) now extend
PLAN-01, AGENT-01 and UI-01. Projects can retain versioned original sources and extracted
text, save context/answers, use qualified project model routes and request
a bounded next-milestone plan. A separate model call reviews the structured proposal.
Deterministic validation checks cited passages, active role reuse, duplicate work, role
purpose, staffing capacity and current context before any role/task application.

Each project controls hosted/paid consent and automatic application. Generation plus
review is reserved before dispatch against shared project/workspace limits; unknown
usage retains its money and call slot. Approved plans create roles, work and separate
review tasks atomically. Blocking questions create human decision tasks without staffing.
At most three new roles are admitted per attempt, within existing team policy; new roles
receive no management privilege or tool/execution authority. The UI exposes evidence,
findings, questions, reuse rationale, review issues, current routes and project model costs.

[Project models and resource limits](runbooks/project-models.md) now extend MODEL-01,
COST-01 and UI-01. Administrator-approved templates support local/open-weight and hosted
text transports; project owners enroll scoped encrypted keys through a write-only UI.
A deliberate synthetic text/JSON check qualifies each current configuration. Planning
and review can use distinct pinned or automatic qualified routes, with no provider-key
fallback or dispatched-call fallback. Default policy denies hosted and paid processing;
configured and qualified zero-fee local models can run under zero monetary ceilings.

Workspace and project lifetime/daily/monthly/per-operation caps and concurrent-call limits
share a durable per-call ledger. Known calls settle separately; unresolved dispatches
retain liabilities through cancellation/restart and have evidenced workspace-owner
reconciliation. Late additional provider charges remain recorded. The UI supports model
rotation/disablement, qualification, routes, limits, usage history and recovery of unknown
key saves without retaining plaintext credentials.

Intake remains bounded to at most 12 source excerpts of 3,000 characters each,
text/DOCX extraction and no OCR/visual analysis. Qualification
proves a small text/JSON exchange, not broad model quality. Native qualification, intake
and specialist execution currently use the ledger; independent chat, business-tool execution, subscriptions,
compute/storage meters and full hosted administration remain open. Migration 0032 adds
intake evidence/history; 0033 adds project models, encrypted credential revisions, resource
policies and usage, importing any earlier intake liabilities once. Recovery includes the
catalog and originals; encrypted project keys require the matching database and master key.

[Native execution and durable workflows](runbooks/native-execution.md) now extend DATA-02,
FLOW-01, AGENT-01 and RUNNER-01. Owners issue a bounded project grant; tasks queue under
versioned role/input/model authority and advance through leased server-side steps. The
PostgreSQL controller records its engine/definition version, dependencies, immutable step
inputs, checkpoints, events, correlated human/timer/child waits and bounded schedules.
Children inherit their root's shared cost/model-call/delegation limits. Every specialist
call uses the existing model ledger; unknown dispatches retain their liabilities.

The Execution panel supports policy and dependency edits, asynchronous admission,
candidate output, questions, activity/history, cancellation, safe retries, schedules and
one-time runner credentials. Optional outbound runners hold separate project-scoped
authority and keep provider keys on the server. Stale leases and changed grants cannot
apply results; completed checkpoints can advance without redispatch. Deterministic
reference operations exercise receipt recovery without exposing real business tools.
Migration 0034 persists this slice. Completed candidates move tasks to **In review**, not
accepted/done; manual board status does not prove output quality.

This controller is the bounded PostgreSQL alternative in the architecture plan. Temporal,
isolated local shell/CAD/GPU work, actual resource meters and broad source retrieval
remain open. The runner coordinates server-side execution; it does
not relocate inference to its own machine. Scheduling progresses while workers poll,
and repeating schedules create separate occurrence tasks with coalesced missed intervals.

[Native artifacts and review](runbooks/native-artifacts.md) now extend ART-01, REVIEW-01
and UI-01. Completed execution drafts automatically create immutable exact-byte revisions
with hashes, mechanical checks and producing-run attribution. Owners can upload generic
files, inspect safe text previews and download authorized originals. One logical deliverable
per task retains up to 100 revisions; files are limited to 1 MiB.

A separate reviewer role receives a fresh bounded context and one reserved model call on
the qualified project review route. Review is recorded board work, independent of its maker.
Current artifact, task, source, role, model and policy authority fence stale results while
known charges remain settled and uncertain dispatches retain their liabilities. Owner
decisions bind exact revision/hash and policy; configurable automatic acceptance requires
a passing independent review. Feedback creates a bounded linked repair task whose output
revises the same artifact and requires a new review. Manual board status creates no acceptance.

The browser provides review policy, checks, costs, findings, owner decisions and revision
history. Independent model review/preview currently supports UTF-8 text up to 48,000 bytes
within the full request admission bound, with limited current source excerpts. Visual,
application and broad source-quality checks, live pilot calibration, streaming/object-store
uploads and physical storage quotas remain open. Migration 0035 persists the artifact slice.

The legacy Work interface, manifest profiles/teams, dispatcher/scheduler, v1 project and
assistant-task boards, project storage/replication services, migrations of old project
files, compatibility launchers and their obsolete tests/examples have been removed.
Migration 0031 adds agent/policy/credential records and drops the three retired project-file
tables. Historical SQL files remain an immutable schema ledger; no legacy-project backfill or compatibility
reader is maintained. No operator database is changed by the development test runs.

Independent chat/accounts/voice and reusable model, execution, file, transport and connector
primitives remain. The chat worker only serves queued conversations; native workflows use
the separate execution runner and controller. No existing provider adapter is automatically a native
project capability.

## Next implementation slices

1. **Model evaluation and remaining resource authority.** Accept capped live qualification
   and pilot quality after explicit key/cap enrollment; no paid call has been made during
   implementation. Broaden capability and task-quality evaluations beyond basic text/JSON.
   Extend context retrieval and role capability/model requirements as their enforcing
   services become available. Complete isolated local tool runners, measured compute/storage
   admission, entitlements and administrator
   catalog UI before claiming unified all-tool billing or commercial readiness. Pending
   live-model evaluation does not block synthetic development of execution and review.
2. **Finish the mandatory baseline quality contracts.** Implement BASE-01 through BASE-05
   across project/source dependencies, package manifests and supported editable originals,
   the review inbox, complete recovery/accounting and authorized takeover. Preserve current
   review/lease/cost authority while closing selective change impact, multi-file delivery,
   measured owner effort, economic liveness and derived-content revocation gaps. Exercise
   actual supported files and reference operations; a declaration or schema alone cannot
   complete a package.
3. **Core acceptance, comparative quality and SaaS controls.** Complete tenant/platform admin, entitlements,
   security/concurrency/recovery/restore scenarios, usability, model quality and capacity
   measurements. Validate PostgreSQL 17 (the CI/container target); current local database
   acceptance uses PostgreSQL 16.15 with pgvector 0.8.6. Pass CORE-01 through CORE-24 and
   BASE-06's registered same-goal comparisons. Record limits and resolve material quality
   deficits before declaring the core accepted or claiming beta readiness.
4. **One real business tool at a time.** Select and accept the next
   [integration work package](architecture/integration-delivery-plan.md), using the clothing
   pilot's actual work. Creative generation, documents, spreadsheets, apparel design,
   Git/code and website operations are separate deliverables after the core gate. Every
   tool inherits BASE-01 through BASE-05 and validates the relevant comparative quality
   for its actual outputs and operation scope.

## Verification and remaining limits

Memory and PostgreSQL contracts exercise persistence, composite foreign keys, version
checks, receipts, revocation, task release, simultaneous claims and staffing admission.
Service/API tests cover current authority before receipt replay, hash-only credentials,
credential expiration/revocation, human/agent distinction and transactional rollback.
Browser tests use authenticated isolated services for role editing, policy, assignment,
conflicts, unknown writes, one-time credentials, pagination and mobile layouts. The
[native project runbook](runbooks/native-projects.md) owns reproducible commands and limits.

October 9 verification covered **2,955 passing tests**, including PostgreSQL and
authenticated Edge browser scenarios, with **91.29% fresh coverage** (branches enabled).
The unchanged repository gate is 90%. The full non-live run initially passed 2,951 tests,
with three failures and one fixture error. A historical migration expectation omitted
0035, and the identity browser test evaluated a deferred Markdown helper before it loaded;
both test issues were corrected. A database connection timeout and browser connection
reset passed on retry without product changes. All four cases then passed together in a
fresh disposable cluster. Sixteen Windows symlink/POSIX FIFO cases were skipped and eight
live-model cases excluded.

Production source stayed fixed across the full run and retries. Coverage began fresh for
the full run and appended only those four current retries; no historical measurements
were merged. Both disposable PostgreSQL 16.15/pgvector 0.8.6 clusters stopped.

The full run includes all **11 artifact and 11 execution browser scenarios**, worker/API authority,
atomic delegation, shared graph limits, paid synthetic accounting, unknown outcomes,
reference receipts, database restart, simultaneous claims, stale leases and checkpoint
recovery without redispatch. Formatting, lint, typing across 157 source files and all ten
connector checks pass. The [artifact runbook](runbooks/native-artifacts.md#verification-and-remaining-scope)
records reproducible commands and limits.

The October 9 focused artifact command/service/API suite plus the existing paid dependency
race regression passed **88 tests in 29.19 seconds**. It covers candidate persistence,
exact revision/hash checks, independent reviewer board work and model routing, owner
acceptance/repair, interrupted review recovery, late charges and current corrected
dependency content and rejected review-context admission without work or charges. This
focused synthetic run did not measure repository coverage; the full run and retries above
provide broader evidence, also recorded in the [artifact runbook](runbooks/native-artifacts.md).

Provider responses in these tests are synthetic. PostgreSQL 17, paid live qualification
and pilot output-quality acceptance remain unverified; no paid model call or operator
database/deployment change was made during implementation.

A scoped board credential grants no shell, model, provider, publishing, spending or
platform-admin rights. Team-size limits are persistent active-role limits, not compute
quotas. Coarse identity/workspace transactions remain the conservative write boundary;
large-tenant throughput is unmeasured. Per-run activity and exact artifact acceptance are
available; full board revision browsing, visual/application review and live artifact-quality
calibration remain open. Model review of a staffing
proposal is not acceptance of future deliverables. Passing these foundation tests does
not complete the [core acceptance matrix](architecture/clothing-brand-pilot-charter.md#core-acceptance-matrix).

## Pilot and document ownership

Stdout's source archive remains untouched. Its conflicting inputs and unresolved source-art
rule are realistic intake/review fixtures, not hard-coded platform policy. Measure accepted
output quality, owner correction time, cost, retrieval, recovery and tenant isolation.

This roadmap owns order and status. The [platform plan](architecture/autonomous-work-platform-plan.md)
owns target architecture and backlog contracts; runbooks describe current behavior.
Removed implementation history remains in Git. Update current runbooks and this roadmap
with each accepted slice; distinguish implementation, test evidence and actual deployment.
