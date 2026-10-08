# Simon roadmap

Updated October 8, 2026. This is the authoritative delivery order for Simon. Detailed
architecture documents explain design choices; runbooks describe implemented behavior.
Use the core-first sequence below for implementation and acceptance.

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

Estimate remaining work from this core backlog. The confirmed direction above governs
sequencing across the architecture and integration plans.

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
text, save context/answers, choose an administrator-approved workspace model and request
a bounded next-milestone plan. A separate model call reviews the structured proposal.
Deterministic validation checks cited passages, active role reuse, duplicate work, role
purpose, staffing capacity and current context before any role/task application.

Each project controls cloud consent, automatic application and a lifetime planning
allowance. Generation plus review is reserved before dispatch; missing usage remains
reserved and retries reuse a durable attempt. Approved plans create roles, work and
separate review tasks atomically. Blocking questions create human decision tasks without
staffing. At most three new roles are admitted per attempt, within existing team policy;
new roles receive no management privilege or tool/execution authority. Existing roles and
tasks are preserved when reused. The UI exposes evidence coverage, findings, questions,
reuse/new-role rationale, review issues, status and planning costs.

This is a bounded planning slice: at most 12 source excerpts of 3,000 characters each,
text/DOCX extraction, no OCR or visual analysis, and no native task execution. Model
configuration currently uses an administrator file with workspace bindings and
environment-key references. Full project BYOK enrollment, automatic free-tier selection,
account-wide billing/resource enforcement and live-model quality acceptance remain open.
Migration 0032 adds intake, source revisions and planning attempts. Recovery bundles now
include the configured model catalog and managed source originals.

The legacy Work interface, manifest profiles/teams, dispatcher/scheduler, v1 project and
assistant-task boards, project storage/replication services, migrations of old project
files, compatibility launchers and their obsolete tests/examples have been removed.
Migration 0031 adds agent/policy/credential records and drops the three retired project-file
tables. Historical SQL files remain an immutable schema ledger; no backfill or compatibility
reader is maintained. No operator database is changed by the development test runs.

Independent chat/accounts/voice and reusable model, execution, file, transport and connector
primitives remain. The chat worker only serves queued conversations. Native board tasks
have no execution dispatcher yet. No existing provider adapter is automatically a native
project capability.

## Next implementation slices

1. **Model and resource authority.** Build beyond the intake-only catalog/allowance:
   project provider enrollment, encrypted keys, qualified free/local
   defaults, configurable routing, concurrent budget reservations and usage settlement.
   Account for planning, specialists, reviews and retries under the same ceiling. Paid
   stdout evaluation needs an enrolled/configured key and an explicit cap; no paid call
   has been made by this implementation. Add unknown-charge reconciliation and visible
   endpoint quality/capability qualification. Extend intake context retrieval and role
   capability/model requirements as their enforcing services become available.
2. **Native execution and durable workflows.** Versioned dependencies, execution leases,
   bounded delegation, cancellation, checkpoints, stale-worker rejection, event-driven
   waits and scheduling. Keep persistent role identity separate from a worker process.
   Enroll optional local runners under task-specific authority and measured resource limits.
3. **Artifact and review loop.** Save candidates automatically, immutable content revisions,
   exact-version validation, independent agent review, configurable human approval and
   repair cycles. Do not treat a manual board status as proof of quality or permission
   to publish. Provide a coherent user view of progress, cost, evidence and pending decisions.
4. **Core acceptance and SaaS controls.** Complete tenant/platform admin, entitlements,
   security/concurrency/recovery/restore scenarios, usability, model quality and capacity
   measurements. Validate PostgreSQL 17 (the CI/container target); current local database
   acceptance uses PostgreSQL 16.15 with pgvector 0.8.6. Record limits before claiming beta readiness.
5. **One real business tool at a time.** Select and accept the next
   [integration work package](architecture/integration-delivery-plan.md), using the clothing
   pilot's actual work. Creative generation, documents, spreadsheets, apparel design,
   Git/code and website operations are separate deliverables after the core gate.

## Verification and remaining limits

Memory and PostgreSQL contracts exercise persistence, composite foreign keys, version
checks, receipts, revocation, task release, simultaneous claims and staffing admission.
Service/API tests cover current authority before receipt replay, hash-only credentials,
credential expiration/revocation, human/agent distinction and transactional rollback.
Browser tests use authenticated isolated services for role editing, policy, assignment,
conflicts, unknown writes, one-time credentials, pagination and mobile layouts. The
[native project runbook](runbooks/native-projects.md) owns reproducible commands and limits.

The October 8 non-live regression run passed 2,263 tests, including 59 browser cases.
A final focused intake run passed 194 tests, including PostgreSQL restart/replay and
13 intake browser cases; these runs overlap. Fresh combined branch coverage is 90.52%,
above the unchanged 90% gate. Measurements for the three intake services changed during
review were replaced with their final focused results before combining coverage.
The broader run skipped 15 host-dependent symlink/FIFO cases and excluded eight live-model
cases; the focused run skipped one symlink case. Both disposable PostgreSQL 16.15/pgvector
0.8.6 clusters stopped. PostgreSQL 17 and capped real-model quality remain unverified.
The [intake verification boundary](runbooks/project-intake.md#verification-boundary) records
reproducible commands and limits. Formatting, lint, typing and all ten connector checks pass.

A scoped board credential grants no shell, model, provider, publishing, spending or
platform-admin rights. Team-size limits are persistent active-role limits, not compute
quotas. Coarse identity/workspace transactions remain the conservative write boundary;
large-tenant throughput is unmeasured. Full activity/revision browsing, validated task
acceptance and automatic artifact-quality review remain open. Model review of a staffing
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
