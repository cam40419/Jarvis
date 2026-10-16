# Core baseline contracts and registered fixtures

Registered October 12, 2026, before comparative trials. This is the next implementation
contract for BASE-01/02/05, linked to the [baseline plan](platform-baseline-quality-plan.md).
The selective-repair fixtures now execute against the artifact, review and dependency services
in `tests/unit/test_native_dependencies.py`; the [runbook](../runbooks/native-dependencies.md)
states their supported bounds. [Package acceptance and native continuity](../runbooks/delivery-review-continuity.md)
now have enforcing implementations; implementation tests do not establish competitive
acceptance. The [input manifest](../../examples/core-baseline/fixtures.json) is versioned
alongside this protocol. Business-tool integration remains after CORE-ACCEPT.

## Dependency and change contract — BASE-01 / CORE-19

A dependency identifies workspace/project, an immutable fact or requirement ID and
revision/hash, an output revision, a typed selector, and coverage (`complete`, `partial`,
`unknown`). Facts carry original source authority and visibility. Requirements carry their
acceptance-rule revision. Supported initial selectors are JSON Pointer for JSON and named
whole-file dependencies for text. Unknown selectors never imply complete coverage.

Change assessment traverses fact-to-output and output-to-output edges to a fixed point,
including cycles without infinite traversal. It records the old/new inputs, affected
selectors, reason, coverage gaps, impacted outputs and retained unaffected outputs. Absence
of declared coverage is explicit uncertainty, not evidence of independence. Revoked or
newly inaccessible source authority still fences disclosure, including historical packets.

A repair proposal has an immutable input assessment, exact expected output heads, bounded
edits, required checks and approval implications. It commits all intended head changes
atomically only if those heads and authority are still current. Concurrent human edits
produce a conflict with preserved work. Unrelated bytes and approval identities must be
identical before and after. Every affected dependent either receives an accepted repair or
remains visibly stale; a repaired upstream file cannot silently approve a downstream file.

The apparel fixture changes a unit price; the non-apparel fixture changes an equipment
inspection interval. Each has a direct output, a transitive output, an unrelated accepted
asset, a partial-coverage output and a cycle. Run both semantic changes and same-value
source-revision changes; conservatively invalidating all artifacts does not pass selective
repair. Inject concurrent edits, revocation during repair and a disconnected dependency.

## Package acceptance contract — BASE-02 / CORE-20

A supported profile is identified by immutable name/version, required roles and cardinality,
allowed media types, dependency rules, validator versions and reproducible reopen/edit/render
steps. An acceptance record binds the profile hash, exact manifest and component hashes,
preview derivations, validator results and versions, reviewer decisions, current authority
and unresolved limitations. Capturing a ZIP or accepting its individual files does not
create this record.

The initial fixture profile is a generic editable text/JSON bundle: one JSON specification,
one Markdown rationale, a JSON schema/check report, and derived safe previews bound to the
exact source hashes. Edit a declared JSON field, reopen it, regenerate its preview and
verify the intended change plus unchanged unrelated data. This establishes only that
declared profile; native CAD, office and creative-application support require their own
accepted tools and validators.

Reject missing/duplicate required roles, absent originals, invalid JSON, a failed validator,
unknown validator versions, preview-source mismatch, stale decisions and revoked evidence.
Changing a profile or validator invalidates applicability until the required revalidation
finishes. Keep prior packages immutable and label why they no longer apply.

## Authorized continuity contract — BASE-05 / CORE-23

A handoff describes exact accepted/stale/candidate revisions, dependency assessment,
decisions, active tasks, unresolved effects, known costs, held liabilities, current grants
and supported continuation operations. It references authorized content and durable effect
receipts; credentials, session tokens and inaccessible source bodies are never embedded.
Current recipient membership, purpose/project grants and revocation are checked both at
export/read and at resume. A historical manifest is not a capability grant.

Run the same project through two human operators and two runtime identities. The successor
must locate accepted work, explain remaining decisions and resume a completed checkpoint
without redispatching it. Include a revoked source, an altered file, an expired credential,
an unresolved provider effect and a downgraded storage allowance. Unauthorized content is
withheld, missing context is named, held liabilities remain charged, and unsupported runtime
transfer remains blocked rather than inventing a successful continuation. Grant changes
during handoff must be tested, not just access at bundle creation.

## Comparison criteria fixed before trials — BASE-06 / CORE-24

Use the [market map and evaluation protocol](../research/2026-10-09/evaluation-plan.md)
to select eligible alternatives for each genuinely supported workflow, including a competent
practitioner using an existing stack. Record current edition, configuration, model, hardware,
account access, setup, operator experience and native strengths before screening. An
unavailable account is missing evidence, not a competitor failure. Freeze the eligible
comparison register and task inputs before confirmatory trials; this document does not
claim current competitive results or complete that register.

Mandatory correctness thresholds are zero unauthorized disclosures, silent losses,
duplicate effects, concealed liabilities or false acceptance events; all required package
steps and seeded critical-defect detection must pass. Unrelated accepted bytes/decisions
must be preserved exactly. No aggregate score can compensate for failure of these gates.

For supported workflows, measure quality defects, successful completion, active review/setup/
takeover seconds, wall-clock completion, intervention count and full attributable cost.
Retain attempts, failures and uncertainty. Against the strongest eligible baseline on each
measure, pre-register a maximum 5% regression margin for human effort and latency and 10%
for total cost; quality and successful mandatory cases permit no regression. These margins
define material regressions rather than a claim that matching them is innovation.

Use 30 paired confirmatory cases per workflow, balanced across apparel and non-apparel
inputs, with randomized order and blinded output scoring. Screening cases cannot be reused
as held-out evidence. Report paired bootstrap one-sided 95% upper bounds (10,000 resamples,
fixed seed 20261012) on continuous measure ratios; a zero baseline requires an absolute
margin registered before trials. A bound above its margin means inconclusive or worse,
not parity. Do not interpret zero failures in 30 trials as proof of zero population risk.
Register scoring rubrics, task timeouts and absolute margins before executing their cases.
Correctness stress tests supplement, rather than replace, these workflow trials.

Re-register material protocol changes before collecting new confirmatory results, retain
the prior protocol/results, and rerun comparisons affected by a model, validator, format,
authority or recovery change. CORE-ACCEPT remains open until this evidence exists.
