# Simon platform pilot charter and core acceptance plan

PLAN-01. Updated October 7, 2026. Status: platform requirements and evaluation contracts grounded in the stdout pilot; architecture choices, test thresholds and experiment budgets remain open. This is a planning artifact, not an implementation or deployment record.

Build **Simon as an autonomous project and company workflow platform**, using stdout's existing clothing-brand context as representative input. The platform must ingest a large, rough project, identify missing or conflicting information, assemble and grow a team, manage all work on boards, produce independently checked outputs, and keep the user informed and in control. Stdout tests these reusable capabilities; it is not a parallel brand consulting project or a requirement to finalize the brand before building Simon.

**Complete and test the initial platform core before implementing business tools one at a time**, as requested by the owner. Brand refinement and creative production become in-platform pilot work as the relevant tools pass acceptance. PLAN-01 defines the platform requirements, evaluation contracts and implementation backlog needed to reach that point.

Read with the [platform plan](autonomous-work-platform-plan.md), [hosting and capacity comparison](hosting-and-capacity-plan.md), [integration work packages](integration-delivery-plan.md) and [delivery roadmap](../next-phases.md).

## Confirmed owner decisions

| Topic            | Decision                                                                            | Design consequence                                                                                                                                                |
| ---------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Product audience | Eventual public SaaS                                                                | Design tenant separation, metering, configurable entitlements, account lifecycle and administration into the core. Public launch remains a later acceptance gate. |
| Administrator    | Owner has configurable full administrative access and control                       | Separate platform administration from tenant membership; provide privileged management, explicit quota overrides, support access and audit history.               |
| Deployment       | Start development locally to keep cost low; primarily hosted later                  | Use reproducible local deployment and portable service/storage interfaces. A cloud-only account must be fully usable.                                             |
| Local workers    | Supported but optional                                                              | Enroll scoped local workers without requiring every customer to own suitable hardware.                                                                            |
| Subscriptions    | Explore tiers for compute and storage with dynamic allocation                       | Use versioned entitlements, reservations, usage records and fair scheduling; do not allocate a dedicated server to every subscriber by default.                   |
| Boards           | Agents and the user both have full work-management control                          | Expose the same project-scoped board operations to humans and agents. Board provider remains a design choice to compare.                                          |
| Pilot            | Use stdout's existing archive and intended work to evaluate Simon                   | Derive reusable intake, planning, team, board, artifact and review requirements; conduct brand development through the platform when its tools are accepted.      |
| Creative work    | Project-specific approval settings and back-and-forth development                   | Persist briefs, concepts, comments, alternatives, decisions and revisions; changes can return work to an earlier stage.                                           |
| Models           | Future SaaS keeps a free default; paid OpenAI is allowed for this pilot             | Enroll the key securely and establish a project cap before paid calls; retain provider independence and no silent paid fallback for other users.                  |
| Existing state   | No meaningful Simon projects require migration; valuable external brand files exist | A justified Simon rebuild is allowed. Preserve the external archive, source references, design history and owner decisions through intake.                        |
| Delivery order   | Complete/test core, then implement one tool at a time                               | Demonstrate framework behavior with reference tools and fixtures first; accept each real tool before starting the next rollout.                                   |

These decisions authorize planning and design changes here. No code, database, credentials, scheduled tasks or deployed services are removed by this document.

## Pilot evidence and remaining platform inputs

The owner supplied an existing brand archive containing identity, strategy, product/design planning and creative material. Its rough drafts, overlapping files, past collaborator contributions, conflicting choices and future code/design needs provide realistic intake and evaluation cases. Preserve the originals and source references independently of any Simon rebuild.

The intended domain work includes brand direction, Essentials and first-drop planning, file organization, task tracking, design iteration, an algorithmic graphics tool and website management. Use these to specify what Simon must support. Brand identity, palette, assortment and the HUMAN ERROR source-art policy remain **OPEN**; they are future project decisions, not blockers for architecture or core implementation.

Private intake records, product details and review drafts belong in the separate private project package. The private stdout task board is a domain backlog that can later test Simon's import and work-management behavior; it is not the platform development roadmap. Manually prepared inventories, briefs and task boards supply evidence and fixtures. Their existence does not prove that Simon can ingest, plan, assign, execute or review them.

Separate missing platform decisions from missing business decisions. Assign each an owner and record which experiment or future task depends on it. Independent architecture and core work should continue while brand choices remain unresolved.

| Input                        | Useful answer                                                                                             | Why it matters                                                                                                          |
| ---------------------------- | --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Representative input         | Supplied: stdout archive and intended creative, operational and code work                                 | Derive generic requirements and sanitized fixtures while preserving private sources.                                    |
| Evaluation quality           | Proposed rubrics, seeded failures and examples of accepted/rejected behavior                              | Makes output quality, independent review and correction handling measurable before final brand choices exist.           |
| First platform milestone     | Reviewed requirements mapping, core contracts, UX flows and dependency-ordered implementation backlog     | Defines PLAN-01 completion without requiring an approved brand direction or assortment.                                 |
| Board and artifact authority | Compare native project boards and artifact storage with optional external collaboration                   | Establish shared ownership and conflict rules; account access can be tested when its connector is selected.             |
| Development hardware         | OS, CPU, RAM, GPU/VRAM, available disk and whether the machine can remain on                              | Determines local worker/model feasibility and test capacity.                                                            |
| Budget ceilings              | Paid OpenAI use is allowed; exact model/tool cap, hosting allowance and brand spending limits remain open | Secure key enrollment and enforceable caps precede paid execution; model allowance does not authorize inventory or ads. |
| Free-model delivery          | Remains a future SaaS onboarding decision; stdout can use its approved paid provider profile              | Preserve free/local tests and no paid fallback while avoiding a free-only restriction on the pilot.                     |

There is enough context to complete PLAN-01's platform design. Hardware and spending limits gate only the experiments that require them. Do not buy services or run paid experiments on an assumed budget.

## Reusable requirements derived from the pilot

| Representative source or intended work                   | Reusable platform behavior                                                                                           | Evaluation contract                                                                              |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Many rough files and overlapping versions                | Durable bulk intake, source inventory, deduplication candidates, provenance and resumable indexing                   | CORE-01/02; every selected fixture remains traceable and recoverable.                            |
| Conflicting identity or product notes                    | Distinguish historical claims, proposals, approved decisions and open questions; route contradictions to human tasks | CORE-01/02/10; no silent promotion of a draft into policy.                                       |
| Past collaborator contributions and new specialties      | Create role responsibilities, preserve contribution history, assign human/agent/pool work and propose team growth    | CORE-03/04; delegation cannot enlarge permissions or budgets.                                    |
| Brand, design, operations and code dependencies          | Produce milestones and a shared board; expose dependencies, blockers, reassignment and progress                      | CORE-04/16; humans and agents operate on the same durable state.                                 |
| Editable graphics, documents and visual revisions        | Versioned artifacts, suitable previews, maker/reviewer separation, annotation and approval bound to the exact output | CORE-10/16; seeded defects fail review and feedback produces a linked revision.                  |
| Local procedural artwork and website code                | Scoped local workers, reproducible tool inputs and code/artifact lineage                                             | CORE-13/17; core fixtures prove execution contracts, later Git/design tools prove actual output. |
| Paid pilot models and free future SaaS use               | Configurable providers, capability routing, project secrets, cost reservations and visible limits                    | CORE-07/08/09/17; no secret leakage, cross-project key use or silent paid fallback.              |
| Long-running work, missing replies and interrupted tools | Correlated waits, cancellation, recovery, operation reconciliation and visible uncertainty                           | CORE-11/12/13/15/18; restart never invents completion or repeats an unconfirmed effect blindly.  |
| Large projects and eventual public SaaS                  | Tenant isolation, fair queues, metering, pagination, optional workers and configurable admin access                  | CORE-05/06/14/16; measure capacity on the declared deployment profile.                           |

Use sanitized fixture equivalents in committed tests; keep the private source-to-fixture mapping outside Git. Drafts can supply contradictory values without choosing the correct brand palette or assortment. Include a synthetic second tenant to test reusable behavior and prevent the design from becoming specific to one clothing company.

## First platform milestone

The first milestone is **a reviewable Simon implementation plan and evaluation contract**, grounded in the supplied pilot and reusable across large projects. It should let the owner assess the proposed project-creation experience, automatic team decisions, manual controls, board authority, quality and review process, model settings, cost visibility and recovery behavior before substantial implementation begins.

An end-to-end scenario starts with representative rough files, builds a project charter with explicit unknowns, proposes a team and task graph, executes bounded reference work, independently reviews its artifacts, takes user feedback, and recovers from an interruption. The UI must expose decisions, task ownership, tool activity, artifact versions, costs, blockers and uncertainty throughout. The user can edit the plan or team, reassign work, change allowed models or budgets, and stop execution.

After the core gate and individual tool acceptance, stdout becomes a real in-platform evaluation: Simon can help refine identity, develop Essentials and the first drop, manage creative revisions and operate accepted code/design workflows. Their final brand decisions are outputs of future project work. They are not prerequisites for completing PLAN-01.

Keep platform backlog items and domain pilot tasks separate and traceable. A platform item is complete when its required behavior passes evaluation; producing a brand document manually cannot satisfy that criterion.

## Platform creative collaboration contract

Use `brief -> concepts -> owner discussion -> revised direction -> production draft -> agent checks -> owner revision or acceptance -> publication proposal`. Project settings select which checkpoints require the owner; a task can revisit concepts when feedback changes the direction.

Keep the original request, annotated comments, accepted decisions, rejected alternatives and the exact referenced versions. A revision request should carry the conversation and visual references forward. Ask a focused question when feedback conflicts with an earlier accepted constraint; otherwise apply the correction and continue independent work.

Support distinct deliverable types: garment artwork, garment construction/technical material, advertisement, social post, still image and video. Each needs its own dimensions, editable source, target channel/application, quality rubric and proof. Batch a coherent campaign for review while keeping every asset individually inspectable.

An approved creative concept does not automatically authorize ad spend, an order, a public post or a change to the storefront. Those are separate effects governed by the project's configured rules. Changing the asset or material destination after approval requires the applicable new review.

## Definition of the initial core system

The core is a usable, locally deployable SaaS foundation with explicit contracts for future tools. It includes:

- Accounts, isolated tenants/workspaces, membership, configurable platform admin, tenant owner roles and authenticated access to files and history.
- Projects, charter/intake, milestones, native board contracts, human/agent/pool assignment, dependencies, comments, task claiming and concurrent edits.
- Persistent agent roles, AI team composition, bounded delegation, manual overrides, schedules/waits and cancellation.
- A unified model registry and essential model transport, free/local profile, project paid-key setup, capability checks, routing, reservations and enforceable spend policies.
- Generic uploads and file artifacts, immutable versions, previews supported by the core, maker/reviewer tasks, owner comments and acceptance bound to exact versions.
- Durable execution, event correlation, tool-operation ledger, leases, recovery, unknown-outcome handling and local/remote worker enrollment contracts.
- Shared queues, per-tenant entitlements, storage limits, metering and admin overrides. Payment-provider integration is a later individual integration.
- A connector SDK/reference implementation, deterministic fake provider and test fixtures that exercise reads, delayed jobs, writes, failures and reconciliation.
- Working user interfaces for project creation, team/board operation, reviews, connections/models, spending, health, and recovery; repeatable local installation and backup/restore.

One minimal real model transport is essential infrastructure, as are generic local file operations and test/reference tools. Specialized research, Office authoring, image/video generation, interactive web automation, Git/code operations, email/calendar, Shopify and social publishing are later tool packages. Local Git and isolated generator/storefront execution are concrete early post-core requirements; they need not wait for remote GitHub integration. Existing implementations are reuse candidates and must pass their own acceptance before being exposed as supported production tools.

“Core complete” proves orchestration and product contracts. It does not certify the quality or reliability of an unimplemented image generator, spreadsheet writer or commerce adapter. Final public SaaS readiness also requires hosted load/security/operations acceptance and the commercial account lifecycle.

## Core acceptance matrix

These are proposed tests, not results. Use deterministic fixtures for repeatable correctness, two independent tenants for isolation, and a small real model evaluation for end-to-end behavior. The pilot may use OpenAI after secure enrollment and cap approval; retain free/local profile tests for SaaS requirements. A mock test alone does not establish model output quality.

| ID      | Scenario                                                                                                   | Required evidence                                                                                                           |
| ------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| CORE-01 | Start a project from representative rough archive files, leave, return and refine its charter              | Historical claims, current decisions and open questions remain distinct; corrections preserve accepted history.             |
| CORE-02 | Import representative files with conflicting palettes, duplicate SKU identifiers or hostile instructions   | Preserve originals/provenance; raise conflicts without silent merging; source text cannot change permissions.               |
| CORE-03 | AI creates the initial team and proposes a needed specialist later                                         | Roles have clear purpose; no new privilege or budget is acquired through role creation.                                     |
| CORE-04 | Human and agent create/edit/reassign board work concurrently                                               | Version conflicts are visible; no lost edits; pooled work has one successful claim.                                         |
| CORE-05 | One tenant tries another tenant's tasks, files, model keys and runner                                      | Requests fail without leaking private state; admin support access uses explicit privileged paths and audit.                 |
| CORE-06 | Admin changes a plan limit or pauses a tenant                                                              | Versioned override takes effect; ordinary users cannot grant themselves the override.                                       |
| CORE-07 | Concurrent parent, child and reviewer runs approach a model spending limit                                 | Admission reserves once against all applicable caps; unknown charges remain reserved.                                       |
| CORE-08 | A free-provider quota runs out or a local model becomes unavailable                                        | Visible wait/retry or permitted free alternative; no silent paid/cloud fallback.                                            |
| CORE-09 | A paid key is added/revoked for one project                                                                | Other projects cannot use it; budgets apply; raw keys never enter prompts, logs or client API responses.                    |
| CORE-10 | A generic design record has mismatched seed/version metadata or a palette conflict, then receives feedback | Independent review catches the fixture defect; revision preserves history; stale acceptance cannot approve changed bytes.   |
| CORE-11 | Restart while planning, reviewing and waiting on a correlated reply                                        | Recoverable work resumes with known inputs; unrelated tasks can continue.                                                   |
| CORE-12 | Lose the response to a simulated external write                                                            | Operation becomes unknown and is reconciled; no blind duplicate submission.                                                 |
| CORE-13 | Disconnect a local runner, expire its lease, and deliver an old result                                     | Replacement follows policy; stale worker cannot publish; no duplicated unconfirmed action.                                  |
| CORE-14 | Saturate one tenant's queue or storage entitlement                                                         | Other tenants remain usable; storage/retry/priority behavior is explicit and no data is silently lost.                      |
| CORE-15 | Restore database/files/keys to an earlier point                                                            | Dispatch stays disabled until workflow and simulated provider state are reconciled; measured recovery evidence is retained. |
| CORE-16 | Owner navigates intake, board, team, review, models and costs                                              | No JSON/config edits needed for product use; keyboard/responsive/error flows work; status reflects actual durable state.    |
| CORE-17 | Perform equivalent bounded tasks through two supported model configurations                                | Capability failures are explicit; actual chosen model/config and usage are recorded; required privacy policy holds.         |
| CORE-18 | Stop a recurring workflow or revoke its authority                                                          | New work stops, already-started effects remain visible and reconciled, and restart cannot re-enable it silently.            |

For each case record exact code/image/config versions, fixture or input, environment, assertions, artifacts, failures and recovery steps. Maintain regression coverage for authorization and reconciliation while replacing old code. Define throughput targets after the hardware/workload baseline, then exercise concurrent tenants and large paginated boards against those targets.

Core acceptance requires all mandatory scenarios passing, no unresolved release-blocking access/spending/data-loss defects, a verified restoration, and owner acceptance of the actual workflow. Live provider tests must state the endpoint/model, policy and cap used, and which cases remain simulated. Production garment generation, visual uniqueness and manufacturing quality are later tool acceptance, not claims established by metadata fixtures. Do not carry a historical coverage number forward as acceptance evidence.

## Tool implementation after the core gate

Select and finish one work package at a time. Each package has an output contract, connector operations, resource grants, billing behavior, tests, a real pilot task, owner review, recovery evidence and a documented support boundary. Enable it only after that gate; then choose the next package based on the pilot's current needs.

Candidate tool packages, selected for platform learning and demonstrated pilot need, to enable one at a time:

1. Source discovery/research or richer artifact import, selected against the first intake gap after generic core uploads work.
2. Local Git/code execution for the generator and website: reviewed changes, reproducible builds, tests, artifact handoff and recovery.
3. Image generation/editing or a selected design application, including the full owner revision loop and project-configured source-art policy.
4. A procedural graphics generator with versioned inputs, editable output and independent visual/technical checks.
5. Document production and spreadsheet authoring as separate tools with rendered inspection and calculation checks.
6. Optional shared storage or visual-board synchronization when collaboration requires it.
7. Video/media, website deployment and commerce operations as separate packages when their workflows are selected.
8. Garment CAD, email/calendar, social publishing and other operations according to demonstrated need.

This is a proposed dependency order, not a requirement to build every item or a storefront-provider decision. Each listed alternative or grouped capability still needs an individual implementation gate. Git/code delivery is distinct from ordinary Git use by Simon's developers. Build the generator and website through accepted tools; do not count briefs as implemented systems or require simultaneous connector rollouts. A procedural graphics compositor and garment pattern CAD are different capabilities.

Generator acceptance must show that the same versioned code, assets, parameters and seed reproduce the same approved design file under a recorded environment. Different seeds alone do not prove different visual output. Review collision/similarity detection, curation, print/placement validation, origin records and issuance/replacement policy before making public uniqueness claims.

## Proposed evaluation thresholds

These are proposed acceptance targets for owner and engineering review, not measured results or brand approval requirements:

- Every mandatory core scenario has a fixture, observable assertions, expected failure behavior and retained evidence; all must pass before the core gate.
- Scripted isolation, permission, stale-approval and budget tests allow no unauthorized read, write or paid dispatch. Any observed violation blocks release.
- Every seeded must-fail review defect is detected, and a revision cannot inherit approval for changed content. Open-ended model quality uses a reviewed rubric and a recorded evaluation set.
- The owner completes intake, team/board changes, review/revision, model settings and stop/recovery flows through the UI without editing configuration files.
- Every evaluated run exposes its state, task owner, selected configuration, costs or unresolved reservations, output versions and recovery status. Unknown provider outcomes remain visible.
- Recovery tests preserve accepted artifacts and reconcile pending actions. Throughput, responsiveness, queue fairness and restore-time targets are set after a measured hardware/workload baseline, before the corresponding performance gate.

## PLAN-01 completion evidence

Complete PLAN-01 when the following platform deliverables are concrete and reviewable:

1. A reusable requirements mapping from pilot evidence to platform behavior, with private material separated from sanitized fixtures.
2. Explicit contracts for project intake, team creation/growth, board ownership, artifacts, independent review, model configuration, budgets, execution and recovery.
3. A representative evaluation set and the mandatory core matrix, with proposed quality and performance thresholds, measurement methods and remaining decisions identified.
4. Draft end-to-end user flows showing automatic operation, manual steering and visibility across normal, blocked, revision and recovery states.
5. Architecture decisions or bounded comparison tasks for board/storage authority, workflow ownership, provider abstraction, local/hosted execution and SaaS isolation.
6. A dependency-ordered platform backlog with acceptance evidence, plus the proposed first post-core tool and its separate evaluation scope.

Final brand identity, palette, assortment, source-art policy and creative approval are future domain decisions. They do not gate this planning stage. Secure credentials and spending caps are required before the experiments that consume them, not before independent architecture work. Manual intake packages, proposed boards and this charter must remain labeled as planning evidence until Simon itself demonstrates the corresponding behavior.
