# Simon roadmap

Updated October 7, 2026. This is the authoritative delivery order for Simon. Detailed
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
authority contracts for intelligent staffing; the model-driven staffing procedure is not
yet implemented.

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

1. **AI-assisted intake and staffing (PLAN-01, AGENT-01, UI-01).** Ingest files into a
   versioned source inventory; conduct a bounded interview; extract goals, constraints and
   unresolved decisions; propose a reusable role/team plan with an explicit rationale and
   affected-work diff. Apply proposals through the existing scoped authority. Add role
   capability/model requirements and independent review obligations as their enforcing
   services become available. Test reuse, task suitability and unnecessary-team-growth
   rejection rather than relying on fixed executive templates.
2. **Model and resource authority.** Project provider enrollment, encrypted keys, free/local
   defaults, configurable routing, concurrent budget reservations and usage settlement.
   Account for planning, specialists, reviews and retries under the same ceiling. Paid
   stdout inference becomes available only after key enrollment and a configured cap.
3. **Native execution and durable workflows.** Versioned dependencies, execution leases,
   bounded delegation, cancellation, checkpoints, stale-worker rejection, event-driven
   waits and scheduling. Keep persistent role identity separate from a worker process.
   Enroll optional local runners under task-specific authority and measured resource limits.
4. **Artifact and review loop.** Save candidates automatically, immutable content revisions,
   exact-version validation, independent agent review, configurable human approval and
   repair cycles. Do not treat a manual board status as proof of quality or permission
   to publish. Provide a coherent user view of progress, cost, evidence and pending decisions.
5. **Core acceptance and SaaS controls.** Complete tenant/platform admin, entitlements,
   security/concurrency/recovery/restore scenarios, usability, model quality and capacity
   measurements. Validate PostgreSQL 17 (the CI/container target); current local database
   acceptance uses PostgreSQL 16.15 with pgvector 0.8.6. Record limits before claiming beta readiness.
6. **One real business tool at a time.** Select and accept the next
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

The October 7 consolidated non-live run passed 2,074 tests, including 46 browser cases;
18 further authority/recovery boundary cases also pass. Fresh combined branch coverage
is 90.08%, above the unchanged 90% gate. Host-dependent skips and the untested PostgreSQL
17 target are recorded in the runbook; this evidence accepts the implemented slices only.

A scoped board credential grants no shell, model, provider, publishing, spending or
platform-admin rights. Team-size limits are persistent active-role limits, not compute
quotas. Coarse identity/workspace transactions remain the conservative write boundary;
large-tenant throughput is unmeasured. Full activity/revision browsing, validated task
acceptance and automatic quality review remain open. Passing these foundation tests does
not complete the [core acceptance matrix](architecture/clothing-brand-pilot-charter.md#core-acceptance-matrix).

## Pilot and document ownership

Stdout's source archive remains untouched. Its conflicting inputs and unresolved source-art
rule are realistic intake/review fixtures, not hard-coded platform policy. Measure accepted
output quality, owner correction time, cost, retrieval, recovery and tenant isolation.

This roadmap owns order and status. The [platform plan](architecture/autonomous-work-platform-plan.md)
owns target architecture and backlog contracts; runbooks describe current behavior.
Removed implementation history remains in Git. Update current runbooks and this roadmap
with each accepted slice; distinguish implementation, test evidence and actual deployment.
