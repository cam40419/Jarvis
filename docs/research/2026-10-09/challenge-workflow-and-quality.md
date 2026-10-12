# Second-pass challenge: joined workflows and actual quality

Research/design date: **October 9, 2026**. This pass reads the [opportunity catalog](opportunity-map.md), [workforce comparison](workforce-platforms.md), [enterprise/infrastructure comparison](enterprise-and-infrastructure.md) and [execution/creative comparison](execution-and-creative-platforms.md). It challenges the proposed small-creative-team entry market. The experiments below are designs, not completed trials, roadmap changes or measured competitive scores.

## What changes after the challenge

A new review portal is a weak starting proposition. Existing creative suites plus a specialist proofing product and a capable coding/general agent already cover much of the proposed experience. Filestage has version-specific review reports; Ziflow has AI-assisted checklists and campaign proofing; eligible Workfront/Frame.io customers already join work management with approvals. Simon should initially help users **maintain the same commercial truth across their existing applications when work changes**, and prove lower correction effort. It should not require them to replace their creative workspace. [Filestage](https://help.filestage.io/en/articles/1482207-export-review-report-to-print-it-or-save-it-locally), [Ziflow](https://www.ziflow.com/), [Workfront/Frame.io](https://help.frame.io/en/articles/12242607-adobe-workfront-integration-overview).

Even evidence-bound files have prior art. Hebbia's June disclosure documents cited PowerPoint outputs and a Tick-and-Tie agent checking presentation figures against source files. StagedWorkspace is an under-review preprint binding parsed/native/review views to file hashes and studying visible diffs; its results do not establish Simon performance. Conversely, Gemini Notebook's current help says slide revisions do not consult sources, a concrete workflow gap worth testing rather than generalizing across all Google products. [Hebbia](https://www.hebbia.com/blog/whats-new-june-disclosure-2026), [StagedWorkspace](https://arxiv.org/abs/2608.18050), [Gemini Notebook revision scope](https://support.google.com/gemininotebook/answer/16757456).

The narrow buyer hypothesis becomes: a person who repeatedly coordinates product facts, editable assets, approvals and external delivery across applications, and presently pays for correction or handoff errors. A casual creator needing one image, a team wholly satisfied inside Canva, or a regulated enterprise seeking a control plane is a poor first target. Interview and concierge evidence must establish frequency and willingness to pay before building broad connectors.

## Six representative jobs where joining work might matter

These are proposed test jobs, with explicit authorities and bounded deliverables. They are not claims that Simon currently executes them.

| Job                       | Cross-application result                                                                                       | Where value could change                                                   | Critical failure                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Product launch            | Approved product record → editable campaign master, SKU sheet, product page and publication proposal           | One material/price change reaches all dependent outputs                    | Correct copy accompanies imagery of the wrong product variant         |
| Supplier handoff          | Existing expert-approved specifications → structured tech-pack summary, cost sheet and staged supplier package | Supplier corrections become traceable decisions and bounded updates        | A plausible rendering is presented as verified physical feasibility   |
| Agency rebrand            | Client guidelines/assets → native design, presentation and website change package                              | Client approval applies to the intended revisions and audiences            | Private client assets or obsolete licensed material cross projects    |
| Evidence-to-pitch         | Approved commercial facts/quotes → workbook, cited deck and editable proposal                                  | Numbers remain consistent after source changes and corrections             | Rendered figures differ from spreadsheet formulas or evidence         |
| Digital product launch    | Brief → working app, editable visuals, help text and release packet                                            | A product change repairs code and collateral together                      | Tests pass while checkout, mobile layout or claimed behavior is wrong |
| Recurring campaign report | Metric definitions/data → calculations, deck and proposed next campaign                                        | Reports stay aligned with changing definitions, not just refreshed numbers | A denominator/date-window change silently reverses the conclusion     |

The supplier job needs human technical expertise and existing approved inputs. Manufacturing readiness, rights, material availability and expert sign-off remain separate obligations. The launch job is a better first experiment than a whole clothing-company operator: it has several artifacts, changing truth and visible handoffs without requiring autonomous procurement.

## Interoperability and wrong-artifact risks

A screenshot is insufficient. The file can flatten editable layers, substitute fonts, break formulas, lose master slides, omit hidden content or render differently in the intended application. The review packet can describe revision A while export or publication uses B. A valid product image can be wrong for its SKU. A native file can remain editable yet embed obsolete facts.

Test the actual native file, its rendered view and a simple intended edit. Preserve the application/version, linked assets/fonts, input snapshot and export settings. Where a vendor offers only rendered output, label the limitation. A human approver should see which bytes, destination and business facts the decision covers.

An API/MCP is an integration route, not automatic authority over every vendor agent. Current Cursor self-hosted workers still use cloud orchestration/inference and upload some output; Workspace Agents accept asynchronous runs rather than synchronous finished deliverables; Runway workflow updates require republishing; Frontify accessed data reaches the external AI. These boundaries affect latency, custody, account setup and confirmation. [Cursor](https://cursor.com/docs/cloud-agent/self-hosted), [Workspace run API](https://learn.chatgpt.com/workspace-agents/trigger-runs), [Runway](https://dev.runwayml.com/workflows), [Frontify](https://www.frontify.com/en/guide/dam-mcp).

Before accepting an operation, establish supported authentication, edition, object scopes, cancel/retry semantics, output export, stable external IDs, usage visibility and terms for the intended commercial use. Unknowns stay unknown. A user subscription does not establish an embeddable API entitlement. Closed native editing may make a human-assisted handoff more reliable than browser automation.

## Twelve complementary ideas and small falsifiers

These specify mechanisms beyond the broad catalog; all remain hypotheses.

1. **Field-level authority bindings.** Bind price, material, dimensions and approved wording to typed source fields and responsible owners. Prototype twenty fields across three outputs. Reject if owners cannot maintain the bindings or ordinary source edits cause excessive alerts.
2. **An intended-edit challenge.** Open the delivered native file, change one designated field and export/reopen it. Prototype one workbook or design template. Reject if this adds more work than the failures it prevents.
3. **Business objects inside creative assets.** Associate product identifiers and claims with editable objects, crops and alt text. Prototype a tagged product card and its mobile variant. Reject if tagging costs exceed cross-asset repair benefits.
4. **User-defined change tests.** Let an owner specify “a price change must alter these outputs and leave these others alone.” Prototype a recorded before/after example. Reject if the tests are too fragile to survive template updates.
5. **Three-view review.** Combine visual rendering, extracted content and native structure instead of trusting any one view. Prototype a deck containing a subtle number and overflow defect. Reject if combined review increases false alarms without reducing accepted defects.
6. **Supplier response reconciliation.** Import a reply as proposed field changes, preserving sender/units and requiring the correct owner decision. Prototype a mocked quote correction. Reject if manual reading and editing is faster or correspondence cannot be mapped reliably.
7. **Vendor-agent checkout packets.** Give a specialist only a bounded input copy, scope and return contract; reconcile returned native files and receipts before promotion. Prototype a coding-agent or creative-API task. Reject if adapter effort and lost native context exceed direct usage.
8. **Release cutover awareness.** On an accepted source change, identify staged/scheduled deliveries that still reference old work. Prototype a local publication queue. Reject if destinations lack sufficient visibility or stale deliveries remain undetectable.
9. **Review-debt accounting.** Show unchecked source changes and expired assumptions alongside money spent. Prototype one accepted packet with three later changes. Reject if the measure is arbitrary or does not affect owner decisions.
10. **Semantic preservation receipts.** Record which facts/objects must survive export and verify them in the exported destination. Prototype workbook-to-deck figures. Reject if practical native exports cannot be inspected without excessive special cases.
11. **Bounded alternative auditions.** Compare a few variants against explicit constraints, then preserve the human's chosen direction and rejected reasons. Prototype three campaign directions with a fixed budget. Reject if the system increases indecision or overfits one preference.
12. **A handoff completion check.** Ask a second authorized person to resume using only the delivery packet. Prototype a ten-minute colleague handoff. Reject if missing context requires the maker or if the packet becomes an unreadable audit archive.

Prioritize field bindings, intended-edit checks and a real cross-output source-change demonstration. They expose value before a universal connector catalog or new visual editor.

## Three moonshots with minimum research prototypes

**A compiler for change-safe commercial work.** A typed brief compiles into tool-specific production steps, invariant checks and bounded repairs. The ambitious result is propagation across native creative/code/business assets. Minimum prototype: one product record, one editable document and one test website, changing a price and a material independently. Compare with a competent person using templates and an agent. Stop if dependencies are mostly unobservable or repairs damage unrelated work.

**A business release rehearsal.** Model a small launch's facts, approvals, destinations and possible effects, then rehearse source changes and interrupted writes before release. Minimum prototype: three local mock destinations with a lost-response scenario and a revoked grant. The simulation must expose assumptions and unknown external state. Stop if rehearsal findings do not predict actual constrained connector behavior; do not promise universal rollback.

**A portable production memory.** Preserve decisions, native assets, evidence, responsibilities and failed alternatives so another human/runtime can continue with equivalent understanding. Minimum prototype: transfer one project between two people and two execution engines with one source revoked. Measure successful continuation and disclosure errors. Stop if portability loses native semantics, leaks inaccessible context or requires costly reconstruction.

These research tracks should earn implementation priority through experiments after the existing core gate. They are not a reason to expand the current backlog immediately.

## Fair pilot: six workflows × five variants

Use the six jobs above and five independently reset variants: **30 defined cases per comparison arm**. Keep variant histories separate so earlier tests do not teach later ones their hidden answers.

| Variant          | Controlled intervention                                                           | Required behavior                                                                         |
| ---------------- | --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Clean task       | Consistent evidence, clear objective, valid native inputs                         | Produce the bounded usable result without unnecessary questions                           |
| Seeded defect    | Insert one held-out material defect: wrong SKU, unit, claim, formula or layout    | Find and explain the defect before acceptance; avoid invented faults                      |
| Owner correction | After candidate creation, request one authorized local correction                 | Repair the intended parts and preserve unrelated accepted material                        |
| Source change    | After acceptance, change one authoritative input; also include an irrelevant edit | Find material dependent outputs, reopen applicable review, ignore irrelevant change       |
| Interrupted run  | Stop execution around a staged delivery or lose a simulated write response        | Avoid duplicate effects, preserve authority/cost uncertainty, reconcile before continuing |

Test three arms: Simon's supported workflow; a competent practitioner using their normal PM/file store plus the best relevant suite/proofing product and a strong agent; and the strongest convenient integrated product for that job. A different native tool may be best for each job. Do not deliberately cripple competitors by withholding their approval/memory/automation features. Account eligibility and unavailable operations are disclosed, not scored as universal absences.

Give every arm the same business evidence, criteria, permitted effects, budgets and access boundaries. Allow an experienced operator to configure each, recording one-time and recurring setup. Blind domain reviewers see the actual deliverables, not vendor labels or persuasive traces. Hold defects out from makers; evaluate first drafts and final outputs separately. Live or paid runs require their own authorized plan; initial fault cases can use local mock effects.

Record correctness as discrete findings: material defects accepted, correct findings, false rejections, missed source impacts, unnecessary invalidations, native edit success, delivery identity and unauthorized/duplicate effects. Creative preference is a separate blinded rating with stated criteria; do not trade away hard correctness with an aesthetic average.

Measure active human setup/review/repair/recovery minutes, interruption count, completion/correction cycles and wall time. Log all attempts, discarded variants, model/tool usage, provider charges, GPU/runtime/storage/export fees and unresolved charges. Report marginal execution cost and allocated subscription/setup cost separately; human effort uses an explicit sensitivity range. Missing billing data stays missing, not zero.

The 30-case screen is diagnostic, not a robust market ranking. Repeat promising cases with independent briefs/seeds and several runs, reporting distributions and paired differences rather than fabricated precision. Pre-register the criteria that matter to pilot owners and the tolerable defect rate. Continue only if lower supervision or better change correctness survives the strongest conventional stack and willingness-to-pay interviews.

## What to prune

Prune a second creative editor, another Kanban/chat workspace, a universal agent marketplace, broad provider routing and an approval button sold as the moat. Treat those as supporting components only where a proved job needs them. Keep a new portal optional until it reduces review time. Defer autonomous physical-production claims, outcome guarantees and unlimited creative exploration.

The second pass narrows the recommendation from “an integrated creative team” to **one recurring change-and-handoff job inside an existing creative stack**. Native-file fidelity, authoritative field propagation and observed delivery become the first proofs. Review/acceptance infrastructure remains useful engineering, but its customer advantage must be earned against established proofing and agent tools.
