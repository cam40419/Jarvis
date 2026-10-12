# Systems and economics challenge

Independent second iteration, observed **October 9, 2026**. This challenges the [100-candidate opportunity map](opportunity-map.md), [workforce comparison](workforce-platforms.md) and [enterprise/infrastructure comparison](enterprise-and-infrastructure.md). It is a design red team, not a change to the roadmap or evidence of comparative performance. Fresh browsing was limited to external revision retention, activity retry semantics and judge limitations; existing research supplies vendor counterexamples.

## Position and disagreements

The proposed bundle—result contracts, accepted work, change impact, a decision inbox and editable production—is coherent enough to test. It is too broad to be the first release promise. A useful initial claim is **one supported job reaches a usable accepted result with less owner effort, including one meaningful change and one interrupted operation**.

I disagree with three stronger interpretations. First, an accepted-work graph does not automatically establish current business truth. Second, a portable acceptance layer does not carry current authority or execution guarantees between systems. Third, editable production should not become a universal Office/design/desktop adapter project before a repeated pilot job wins. These are scope and architecture disagreements, not objections to retaining those options.

The most severe counterexample is a configured existing stack: a competent person, native file history, a proofing workflow, a strong assistant and automation. Workato already offers an [Action Board](https://www.workato.com/the-connector/action-board-go/); Dify supports [human edit/regenerate/pause/resume](https://dify.ai/blog/the-human-input-node-bringing-human-judgment-into-automated-workflows) and an [Asqav signed-action plugin](https://dify.ai/blog/asqav-joins-dify-marketplace-to-make-every-agent-action-verifiable). [AgentCore policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html) checks tool authority, prior approvals and cumulative thresholds. Individual gates, receipts and inboxes are weak novelty claims. Simon must win the joined workflow and its operating economics.

## What exists and what the bundle would add

| Area                 | Current documented Simon                                                                                                          | Proposed behavior requiring additional acceptance                                                                       |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Deliverable identity | One logical artifact/task; immutable revisions, SHA-256, maker/run identity; 1 MiB/file and 100 revisions                         | Multi-file native source/render/manifest packages, larger storage and external-object identities                        |
| Review               | Fresh reviewer role, exact revision/hash, bounded supplied evidence; UTF-8 up to 48,000 bytes; human-only binary review available | Visual/application validators, empirical reviewer calibration, domain production checks                                 |
| Changing work        | Current source/task/role/model/policy authority fences obsolete results; linked bounded repairs                                   | Claim-level dependency coverage, selective semantic invalidation, cross-tool change detection                           |
| Execution and money  | PostgreSQL checkpoints, scoped admission, model reservations/settlement, unknown holds and reconciliation                         | Accepted business adapters, verified external effect recovery, complete compute/storage/tool economics                  |
| Deployment           | Administrator-declared local/hosted model catalog; encrypted project credentials; optional execution coordination                 | Desktop production boundary, isolated application workers, measured entitlements/capacity and hosted release acceptance |

These distinctions follow the [artifact runbook](../../runbooks/native-artifacts.md), [model runbook](../../runbooks/project-models.md) and [roadmap](../../next-phases.md). Passing synthetic regressions is evidence for contracts, not live output quality or a finished public service.

## Stressing the 100 candidates

All twenty opportunity themes encounter seven shared constraints. The table groups the entire catalog; it does not rank one hundred features as independent bets.

| Catalog themes                                                    | Constraint that can defeat the idea                                       | Smallest useful discriminator                                                 |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Intake; planning; evidence/change                                 | The contract and dependency graph cost more to maintain than the work     | Brief time, missed material changes, false stale alerts and repair locality   |
| Formats; creative production; review                              | Reviewers agree on attractive but unusable output                         | Reopen/edit/render supported files; blind expert defect assessment            |
| Tools; reliability; collaboration                                 | History survives but external state, authority or handoff does not        | Lost reply, duplicate request, revoked reader and second-person takeover      |
| Privacy; learning                                                 | Derived memories leak information or silently harden an error into policy | Revoke a source, delete a client and challenge a learned preference           |
| Resources; commercial operations                                  | Cheap generation creates expensive review, hosting and support            | Complete cost and human minutes per usable outcome, including failed attempts |
| Interaction; packages; distribution                               | A polished new workspace adds another place to check                      | Repeated use against the buyer's existing stack without concierge prompting   |
| Ecosystem; evaluation; long-term research; alternative strategies | A reusable platform appears before transferable evidence exists           | A second held-out customer/workflow succeeds without custom engineering       |

## Semantic change is the hard part

Hash changes establish identity differences, not material impact. A spelling correction might be irrelevant; changing “must” to “may,” a currency unit or the authoritative source can invalidate an entire plan. An unchanged source can become unusable when its permission, freshness or reliability changes. A missing dependency edge is more dangerous than an unnecessary review because the interface can display false assurance.

Start with explicit typed dependencies: input revision, requirement/policy revision, source passage, calculation parameter and validator version. Separate **historically accepted**, **current applicability**, **current disclosure permission** and **external release status**. Avoid one green status that implies all four. A model may propose impact edges and repair scope; it should not silently suppress mandatory authority invalidation. Unsupported relationships need a visible coverage gap and conservative re-review.

Selectivity should be earned on a fixed defect corpus: include irrelevant edits, paraphrases, contradictory updates, expired facts, permission changes, source replacement and hidden dependencies. Compare against full re-review. If selective repair regularly misses material consequences or saves little total attention, keep coarse invalidation rather than adding a complex semantic graph. Acceptance must name the policy/validator versions actually used, not imply all future checks passed.

## Historical decisions and current permissions

A decision can remain historically valid while the original source is no longer available to a new reader. Conversely, retaining a decision and its explanation can disclose the revoked source. [ClickUp explicitly documents](https://help.clickup.com/hc/en-us/articles/36926065055127-Super-Agent-privacy-security-and-permissions) a memory-sharing boundary that differs from original source access. Simon should not claim permission-aware evidence merely because it checks project membership.

Treat source snapshots, extracts, derived statements, previews, embeddings and logs as separate disclosure surfaces with inherited classification. Export cannot bypass that classification. An explicit owner-authorized publication/declassification event can create a separately shareable deliverable; a past model approval cannot. Deletion policy must explain whether historical metadata can remain, which content is inaccessible and which downstream assertions require reassessment. This is an unresolved product contract, not something a provenance graph solves automatically.

## External files and the rollback illusion

An external URL is not immutable accepted bytes. [Drive's revision API](https://developers.google.com/workspace/drive/api/guides/manage-revisions) documents auto-purged blob history, limited keep-forever revisions and potentially incomplete revision listings for heavily edited Workspace files. Revision access also requires stronger roles than ordinary reading. A connector therefore needs an operation-specific snapshot/export policy, content digest, source revision identity and permission check; retaining an ID alone is insufficient.

At action time, verify the accepted payload and destination object/account against current authority and supported concurrency preconditions. If another person edits the external object, import a new candidate or mark the relationship unresolved. Do not silently rewrite Simon's immutable revision to match it. Some formats have unstable rendering, external fonts, formulas, linked assets or application-specific objects: a reopened editable file needs more than a screenshot comparison.

Undo is a new effect. A restored file does not retract an email, downstream copy, supplier decision or charge. Every accepted operation needs its own duplicate-prevention boundary, observable receipt and compensation limits. Even [Temporal's activity documentation](https://docs.temporal.io/activities) recommends idempotent code because retrying an activity can duplicate side effects. Durable control flow cannot confer transactional behavior on another service.

## Reviewer correlation and queue growth

Fresh context and a separate role are useful authority separation. They do not establish statistically independent errors. Two models can share training, assumptions and source omissions. The [MT-Bench judge study](https://arxiv.org/abs/2306.05685) identifies position, verbosity and self-enhancement bias and limited reasoning; its preference-agreement result is not a certification of Simon's task correctness.

Use deterministic application checks for machine-verifiable properties, bounded evidence checks for consequential claims and human expertise for creative direction or unsupported formats. Measure false acceptance and unnecessary repair separately; model agreement alone is a poor outcome. Review cost includes the owner's time to inspect findings, reconcile disagreement and approve new revisions. The inbox must show omitted/truncated evidence and unsupported checks, not bury them under an approval summary.

Three repairs can still be three expensive non-converging attempts. Add a no-progress/escalation policy and an attention forecast. Repeated owner corrections may reveal an inadequate brief or tool, not a missing extra agent. Stop when the next review cannot plausibly change the decision; retain mandatory authority gates. If the review queue grows faster than the owner clears it, admit less new work rather than celebrating throughput.

## Honest money can still create deadlock

Current unknown calls retain money **and concurrency**, including zero-fee calls. Intake reserves generation and review together, occupying two slots even before review dispatch; default concurrency is two. This can make one uncertain operation block a workspace's otherwise free work. The current behavior is conservative and documented in [model accounting](../../runbooks/project-models.md); changing it requires proof that possible running work remains bounded.

Separate monetary liability, possible live execution and queued future capacity conceptually. A future change could retain unknown monetary debt while releasing a runtime slot only after trusted termination/lease evidence, or stage later execution-slot admission while reserving the entire monetary budget. Neither a timeout nor a user pressing retry proves the old call ended. Do not resolve the problem by automatically refunding unknown holds or sending a second call.

Measure workspace starvation, reconciliation effort, retry cost and forecast calibration under several concurrent projects. “Cost per accepted result” needs a denominator including abandoned outcomes and human work; show median and tail costs, not only a successful trace. A zero token tariff still has compute, storage, electricity and support cost. Customer-key usage does not remove Simon's hosting or operational costs.

## Hosting, licensing and resource assumptions

The current `local` model flag is an administrator assertion. Loopback means the Simon server's host, not a browser user's desktop; enrolled runners do not move inference to their host. Private production requires a verified data-flow boundary plus supported worker/app/hardware profiles. Desktop automation must test unattended execution, application licensing, crash recovery, secrets and rendering consistency for each supported environment; this pass establishes no universal entitlement to automate licensed applications.

Reuse is also a commercial decision. [n8n's October 6 license explanation](https://support.n8n.io/article/can-i-use-your-license-for-my-use-case) distinguishes internal use from external customers configuring workflows through UI/API/MCP/agents. [Dify's source license](https://github.com/langgenius/dify/blob/main/LICENSE) requires written authorization for covered multi-tenant service operation. Integrating a customer's instance and embedding a builder into Simon are different deployment models. Obtain the applicable commercial terms before adopting either as a hosted foundation.

Storage admission must account for originals, revisions, renders, extracts, backups, temporary files and reservations; rendered previews can expand tiny source files substantially. Compute admission needs real worker time, memory/GPU capacity and cancellation evidence. Current file bounds and model ceilings are not complete subscription metering. A low-cost SaaS tier cannot be priced from token cost alone.

## Missing avenues and minimum sequence

Four additions sharpen the catalog: a **revocation/retention state machine** for derived decisions; an **external-object synchronization contract** with conflict quarantine; an **economic liveness drill** across zero-fee/unknown/concurrent calls; and **format-specific capability profiles** specifying supported apps, renderer versions, validators and compensation limits. They are acceptance requirements before broad promises, not four new dashboards.

1. Finish the authoritative core gate: resource/entitlement enforcement, administration, security/concurrency/recovery/restore, PostgreSQL 17 acceptance, usability and bounded live model quality. Benchmark current text review during this work; do not claim new tools are available.
2. Select one pilot job and acceptance rubric. Produce a complete current text packet and one seeded change; measure owner minutes and defects against a strong configured baseline. Prototype the decision inbox using actual recorded decisions.
3. After the core gate, accept one narrow tool operation that materially helps that job. A bounded source-file/generator or Git operation is a candidate, not permission for general shell/desktop execution. Specify exact inputs, resulting editable files, validation, authority, costs and lost-response recovery.
4. Repeat the same workflow with interruption and human takeover. Add another operation only when the first repeatedly produces accepted useful work within its stated limits. Cross-customer reuse, portability and workflow packs come after evidence of transfer.

Reuse existing durable runtimes when a measured orchestration gap justifies them; keep Simon's authority and acceptance contracts independent. Integrate customer-owned boards/file stores through one authority per object. Reuse native application validators and narrow open libraries before building a renderer or universal desktop agent. Avoid a Flowise cloud dependency given its documented [2026 sunset](https://flowiseai.com/sunset), a connector-count race, or a runtime migration undertaken solely to advertise durability.

## Kill criteria and architecture falsifiers

Pre-register thresholds against the buyer's workflow rather than choosing them after results. The bundle should be narrowed or rejected if repeated blind tests show no reduction in owner effort or defect rate at comparable total cost; if selective change handling misses consequential dependencies; or if the real editable output requires expert cleanup that erases the gain. Stop the hosted offer if tail support/recovery cost defeats the planned margin.

Hard failures include accepting bytes that differ from the reviewed target, applying revoked authority, disclosing a revoked source through derived content, duplicate external effects without reconciliation, quota overspend from concurrent reservations, unrecoverable state after restore and indefinite workspace starvation after a bounded interruption. These cannot be averaged away by good generation scores. Keep public-documentation uncertainty separate from a failed test. The aim is a supportable repeated result; the architecture remains a hypothesis until these cases pass.
